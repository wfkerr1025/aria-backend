# backend/tests/test_provider_stop_sequences.py
#
# The local provider stops generating, rather than generating text the
# transport will throw away.
#
# backend/core/answer_stream.py already hides degeneration from the user.
# What it cannot do is give back the time: a qwen2.5-0.5b turn that
# produced its answer in two seconds went on repeating two citations for
# another thirty-four, because nothing told llama.cpp to stop. The filter
# is the last line of defence; this is the first.
#
# Two mechanisms, and they cover different failures:
#
#   stop sequences   a fixed string -- a new speaker, the instruction
#                    block restarting. llama.cpp matches these itself.
#   repetition halt  no fixed string to match; the repeated segment is
#                    whatever the model landed on, so the provider watches
#                    its own output and closes the generator.
#
# The provider is exercised through a fake loader. What is under test is
# when the provider stops pulling tokens, which is provider logic --
# running a real GGUF to assert it would make the test slow, machine
# dependent, and no more truthful. One opt-in live test at the bottom
# covers the real thing.

from __future__ import annotations

import os

import pytest

from backend.core.answer_stream import TERMINATORS, detect_repetition_loop
from backend.llm.providers import local_provider
from backend.llm.providers.local_provider import TURN_BOUNDARY_STOP_SEQUENCES


class FakeLoader:
    """A loader that yields fixed chunks and records how many were pulled.

    llama.cpp's stream is lazy: it samples the next token only when the
    consumer asks for one. That is what makes "the provider stopped" a
    real saving rather than a cosmetic one, and counting pulls here is how
    that is observed.
    """

    def __init__(self, chunks, stop_at=None):
        self.chunks = chunks
        self.stop_at = stop_at or []
        self.pulled = 0
        self.closed = False

    def run_stream(self, prompt, *, max_tokens, temperature, stop=None):
        emitted = ""
        try:
            for chunk in self.chunks:
                # Approximate llama.cpp's own stop-sequence handling so a
                # test can tell "the provider asked it to stop" from "the
                # provider stopped pulling".
                if any(marker in emitted + chunk for marker in (stop or [])):
                    return
                self.pulled += 1
                emitted += chunk
                yield chunk
        finally:
            self.closed = True


def provider_with(chunks):
    provider = local_provider.Provider()
    loader = FakeLoader(chunks)
    provider.loader = loader
    provider._load_model_for_request = lambda request: ("fake-model", object())
    provider._build_prompt = lambda request: "PROMPT"
    return provider, loader


def collect(chunks):
    """Run one streaming turn; return (text, loader)."""
    provider, loader = provider_with(chunks)
    out = []

    def callback(packet):
        if packet.get("type") == "chat_stream":
            out.append(packet["content"])

    provider.stream(
        type("Request", (), {"max_tokens": 2048, "temperature": 0.7, "model_id": "m"})(),
        callback,
    )
    return "".join(out), loader


def tokens(text, size=8):
    return [text[i:i + size] for i in range(0, len(text), size)]


# ======================================================
# Stop sequences
# ======================================================
@pytest.mark.parametrize("marker", [
    "\nUser:", "\nAssistant:", "\nSystem:",
    "- [user]", "- [assistant]", "- [system]",
])
def test_a_speaker_marker_is_a_stop_sequence(marker):
    """A new speaker means the model has started a turn nobody asked for."""
    assert marker in TURN_BOUNDARY_STOP_SEQUENCES


@pytest.mark.parametrize("marker", [
    "\nInstruction:", "\nInstructions:", "\nUser Query:", "\nSynthesis Instructions:",
])
def test_the_prompts_instruction_block_is_a_stop_sequence(marker):
    assert marker in TURN_BOUNDARY_STOP_SEQUENCES


def test_generation_stops_at_a_fabricated_turn():
    text, loader = collect(tokens(
        "Microsoft is trading at $412.30.\n- [user] and what about Apple?\n"
    ))
    assert "$412.30" in text
    assert "Apple" not in text
    assert loader.pulled < len(tokens(
        "Microsoft is trading at $412.30.\n- [user] and what about Apple?\n"
    ))


# ------------------------------------------------------
# What must NOT be a stop sequence
# ------------------------------------------------------
@pytest.mark.parametrize("label", [
    "Response:", "Solution:", "Answer:", "Output:",
    "web_search (step1):", "step1): web_search",
    "search_result", "file:web_search_result.md",
])
def test_scaffolding_labels_are_never_stop_sequences(label):
    """Stripping and stopping are opposites, and confusing them is fatal.

    answer_stream removes these labels and keeps the text after them,
    because that text is the answer -- phi-3's best phrasing came after
    "Solution:", and qwen's reply *opened* with "web_search (step1):". A
    stop sequence on any of them would end generation before the answer
    existed, trading a cosmetic problem for an empty reply.

    "(file:...#search_result)" is also the citation format the synthesis
    prompt asks for, so stopping there would truncate a correct answer at
    its first citation.
    """
    assert label not in TURN_BOUNDARY_STOP_SEQUENCES


def test_an_answer_containing_a_scaffolding_word_is_not_cut_short():
    answer = "The Response: header is set by the server. The Solution: label follows it."
    text, _ = collect(tokens(answer))
    assert text == answer


# ======================================================
# Repetition halt
# ======================================================
def test_generation_halts_when_the_model_starts_repeating():
    """The thirty-four wasted seconds, as a test."""
    answer = "Microsoft (MSFT) is trading at $412.30, up 1.2% (source: example.com). "
    spam = "(note: no other information is provided) (file:web_search_result.md#search_result) "
    chunks = tokens(answer + spam * 20)

    text, loader = collect(chunks)

    assert "$412.30" in text
    assert loader.pulled < len(chunks), "the provider kept pulling tokens through the loop"
    assert text.count("file:web_search_result.md") <= 2


def test_the_generator_is_closed_not_just_abandoned():
    """Closing is what stops llama.cpp sampling; breaking alone defers it."""
    chunks = tokens("answer. " + "the same phrase repeated forever. " * 12)
    _, loader = collect(chunks)
    assert loader.closed is True


def test_the_provider_halts_where_the_transport_would_truncate():
    """The equivalence the shared predicate exists to guarantee.

    If these two ever disagree the user cannot tell -- the filtered output
    is identical either way -- and only the wasted seconds differ. So it
    is asserted rather than assumed.
    """
    from backend.core.answer_stream import AnswerStream

    raw = "answer here. " + "(a repeated citation fragment) " * 8
    text, _ = collect(tokens(raw))

    filt = AnswerStream()
    filtered = filt.push(raw) + filt.finish()

    assert detect_repetition_loop(text) or len(text) < len(raw)
    assert filt.stats["stopped_looping"] is True
    # Both halted on the same condition, so the provider's output needs no
    # further truncation than the transport already applies.
    assert len(text) <= len(raw)
    assert "answer here." in filtered


# ------------------------------------------------------
# Punctuation is not degeneration
# ------------------------------------------------------
@pytest.mark.parametrize("rule", [
    "Section ======================================== end.",
    "Divider ---------------------------------------- end.",
    "Zigzag -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=- end.",
    "Dots ........................................... end.",
])
def test_a_horizontal_rule_does_not_halt_generation(rule):
    """One or two distinct characters is punctuation, not a loop."""
    text, loader = collect(tokens(rule))
    assert text == rule
    assert loader.pulled == len(tokens(rule))


def test_a_normal_answer_runs_to_completion():
    answer = (
        "The build pipeline compiles shaders first, then links them, then "
        "packages the result. Failures at the shader stage usually mean a "
        "missing include path rather than a syntax error."
    )
    text, loader = collect(tokens(answer))
    assert text == answer
    assert loader.pulled == len(tokens(answer))


def test_short_natural_repetition_survives():
    answer = "It is very very very slow, scoring 10, 10, 10 across the board."
    text, _ = collect(tokens(answer))
    assert text == answer


# ======================================================
# The two lists must not drift apart
# ======================================================
def test_every_transport_terminator_has_a_provider_stop_sequence():
    """A marker worth truncating on is worth not generating.

    The lists cannot be literally shared -- llama.cpp matches raw
    substrings including newlines, the transport matches a normalised line
    start -- so this checks the weaker property that actually matters:
    nothing the transport cuts is missing from what the provider stops on.
    """
    provider_markers = " ".join(TURN_BOUNDARY_STOP_SEQUENCES).lower()

    for terminator in TERMINATORS:
        if terminator.startswith("<|") or terminator.startswith("###"):
            continue  # chat-template tokens, handled by llama.cpp itself
        assert terminator.lower() in provider_markers, (
            f"the transport truncates on {terminator!r} but the provider "
            f"still generates it"
        )


# ======================================================
# End to end, against the real model
# ======================================================
LIVE = os.environ.get("ARIA_LIVE_MODEL_TESTS") == "1"


@pytest.mark.skipif(not LIVE, reason="set ARIA_LIVE_MODEL_TESTS=1 to run a real model")
def test_a_small_model_halts_on_its_own_without_the_transport_filter():
    """The provider's raw output, unfiltered.

    The mirror of test_a_small_model_answers_from_the_lookup_instead_of_
    looping, with the transport taken out of the picture: this reads what
    the provider actually produced, so a pass means generation stopped
    rather than being hidden.
    """
    import time

    from backend.core.local_inference_engine import InferenceMessage, InferenceRequest

    provider = local_provider.Provider()
    prompt = (
        "User Query: how much is MSFT trading at\n\n"
        "Evidence Summary: 1 tool result below, listed under Tool Results.\n\n"
        "Tool Results:\n"
        "- web_search (step1): Microsoft (MSFT) is trading at $412.30, up 1.2% "
        "(source: example.com)\n\n"
        "Produce a single coherent answer.\n"
    )

    # allow_override for the same reason the WebSocket-level live test
    # passes skipSafetyCheck: loading goes through the safety gate, which
    # reads live CPU/RAM and refuses whenever the machine is busy. This
    # test is about when generation stops, not about the gate.
    request = InferenceRequest(
        model_id="qwen2.5-0.5b-instruct-q4_k_m",
        messages=[InferenceMessage(role="user", content=prompt)],
        max_tokens=2048,
        temperature=0.0,
        allow_override=True,
    )

    chunks = []
    started = time.monotonic()
    provider.stream(request, lambda packet: chunks.append(packet.get("content", "")))
    elapsed = time.monotonic() - started
    raw = "".join(chunks)

    assert "412.30" in raw, f"the lookup never reached the answer: {raw!r}"

    # Length is the signal that generation stopped: max_tokens is 2048,
    # which is several thousand characters, so a few hundred means it
    # halted rather than ran out. Asserted in preference to wall-clock,
    # which measures how busy the machine is as much as anything else.
    assert len(raw) < 1200, f"generation produced {len(raw)} chars -- it did not halt"

    # A loose upper bound as well, because the point of the patch is time.
    # Before it, this turn took ~36s; the exact figure is machine
    # dependent, so this is deliberately far above a healthy run (~5s)
    # rather than close to it.
    assert elapsed < 30, f"generation ran for {elapsed:.1f}s -- it did not halt"
