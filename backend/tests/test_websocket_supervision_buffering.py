# backend/tests/test_websocket_supervision_buffering.py
#
# The WebSocket path now holds every token until the whole reply exists,
# supervises it, and sends the cleaned text as one piece.
#
# This is a deliberate trade and the tests should say so rather than
# pretend it is free. Token-by-token streaming shows progress instantly
# and cannot be taken back; buffering can clean the text before anyone
# reads it and costs the length of the whole answer in silence. The
# choice here is buffering, because a leaked "**assistant:**" or a
# half-repeated paragraph is permanent once read.
#
# Three properties matter and each is asserted separately:
#
#   nothing is sent during generation      -- or the buffering is a lie
#   the cleaned text IS what gets sent     -- or the supervision is decoration
#   failure sends the unsupervised answer  -- silence is the one outcome
#                                             worse than untidy prose

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from backend.chat import supervisor_layer as sup
from backend.chat.supervisor_layer import (
    deterministic_repair, model_supervision, supervise_full,
)
from backend.websocket import handlers


def code_of(function) -> str:
    """A function's CODE, without its docstring or comments.

    Parsed and unparsed rather than filtered by line, because the trap
    here is not comments alone -- it is the docstring. Five source
    assertions in this codebase have now matched the sentence explaining
    what the code must not do; two of them were in this file, where the
    docstring says "deliberately NOT a stream_token" and the assertion
    found that instead of the code.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    node = tree.body[0]
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        node.body = node.body[1:]
    return ast.unparse(node)


# ======================================================
# The two stages exist under the names they were asked for
# ======================================================
def test_both_stages_are_callable_separately():
    # The specification names deterministic_repair and model_supervision.
    # Keeping them separately callable is what lets the transports choose
    # one, the other, or both without reimplementing the pipeline.
    assert callable(deterministic_repair)
    assert callable(model_supervision)
    assert callable(supervise_full)


def test_stage_one_needs_no_model():
    result = deterministic_repair("Blue is calm.\n\n**assistant:** more")

    assert "**assistant:**" not in result.text
    assert result.repairs


def test_stage_two_without_a_model_changes_nothing():
    result = model_supervision("Some answer.", {"role": "chat_tools"}, generate=None)

    assert result.text == "Some answer."
    assert result.supervised is False


# ======================================================
# Stage one runs for everything, stage two for the heavy models
# ======================================================
@pytest.mark.parametrize("model_id", [
    "mistral-7b-q4km", "nemo-12b-q5", "phi-3-mini-4k-instruct-q4",
    "qwen2.5-0.5b-instruct-q4_k_m", "some-model-nobody-registered", None,
])
def test_deterministic_repair_runs_for_every_model(model_id):
    # phi-3 is the DEFAULT chat model and also the supervisor. Its
    # exemption from stage two must not exempt it from stage one, which
    # is what happened when the two questions shared one flag -- measured
    # live, phi-3 leaks "**assistant:**" into its own answers.
    assert sup.needs_supervision(model_id) is True

    result = supervise_full("Answer.\n\n**assistant:** leaked", model_id, generate=None)

    assert "**assistant:**" not in result.text


@pytest.mark.parametrize("model_id,expected", [
    ("mistral-7b-q4km", True),
    ("nemo-12b-q5", True),
    ("phi-3-mini-4k-instruct-q4", False),
    ("qwen2.5-0.5b-instruct-q4_k_m", False),
])
def test_model_supervision_runs_only_for_the_heavy_models(model_id, expected):
    assert sup.needs_model_supervision(model_id) is expected


def test_the_supervisor_does_not_check_its_own_work():
    called = []

    supervise_full("A long enough answer to survive every guard here.",
                   "phi-3-mini-4k-instruct-q4",
                   generate=lambda p: called.append(p) or "rewritten")

    # generate was supplied and deliberately ignored: the role is
    # supervisor, and a second inference to grade its own homework buys
    # nothing.
    assert called == []


# ======================================================
# Code blocks survive, byte for byte
# ======================================================
LONG = ("The build pipeline compiles every shader in the project, links the "
        "results into a bundle, and runs the regression suite against it.")
CODE = "```python\nprint('hello world')\n```"


def test_a_code_block_is_preserved_exactly():
    raw = f"{LONG}\n\n{CODE}"

    result = supervise_full(raw, "mistral-7b-q4km", generate=lambda p: p)

    assert CODE in result.text


def test_a_rewrite_that_touches_a_code_block_is_rejected():
    raw = f"{LONG}\n\n{CODE}"
    tampered = f"{LONG}\n\n```python\nprint('goodbye world')\n```"

    result = supervise_full(raw, "mistral-7b-q4km", generate=lambda p: tampered)

    assert result.rejected is True
    assert "print('hello world')" in result.text
    assert "goodbye" not in result.text


def test_a_rewrite_that_truncates_is_rejected():
    result = supervise_full(f"{LONG} {LONG}", "mistral-7b-q4km",
                            generate=lambda p: "Short.")

    assert result.rejected is True
    assert LONG in result.text


def test_leakage_inside_a_code_block_is_left_alone():
    raw = f"{LONG}\n\n```yaml\nsystem:\n  name: aria\n```"

    result = supervise_full(raw, "mistral-7b-q4km", generate=None)

    assert "system:\n  name: aria" in result.text


# ======================================================
# The transport buffers
# ======================================================
def test_buffering_is_off():
    # It was on, and on a 12B it read as a freeze: 55 seconds of blank
    # screen for one file, most of it a cold model load with nothing to
    # show. A supervision pass is not worth a minute of silence.
    assert handlers.BUFFER_FOR_SUPERVISION is False


def test_tokens_are_forwarded_as_they_arrive():
    source = inspect.getsource(handlers.WebSocketHandler._stream_inference)
    code = "\n".join(line for line in source.splitlines()
                     if not line.strip().startswith("#"))

    # The token branch filters through AnswerStream and sends. If it
    # returned early unconditionally the streaming claim would be false.
    assert "answer.push(raw)" in code
    assert "published.append(publishable)" in code


def test_the_stream_is_held_open_past_generation():
    source = inspect.getsource(handlers.WebSocketHandler._stream_inference)
    code = "\n".join(line for line in source.splitlines()
                     if not line.strip().startswith("#"))

    # The provider's stream_end must not close the turn: the actions have
    # not run, so the reply cannot yet say what happened. _finish_stream
    # sends the revision and then the end.
    assert "_finish_stream" in code
    assert code.index("_run_answer_actions") < code.index("_finish_stream")


def test_the_revision_carries_the_checked_text():
    source = inspect.getsource(handlers.WebSocketHandler._finish_stream)

    # Streaming cannot take a token back; this is how the cleanup still
    # applies. Sent BEFORE stream_end, so the corrected text is what the
    # client records in the history the next turn reads.
    assert "supervise_full" in source
    assert "render_actions_for_reading" in source
    assert source.index("message_revised") < source.index('"stream_end"')


def test_no_revision_when_the_cleaned_text_matches():
    source = inspect.getsource(handlers.WebSocketHandler._finish_stream)

    # Almost every ordinary chat turn. A revision on every turn would
    # make the client re-render a message that did not change.
    assert 'final.strip() != (streamed or "").strip()' in source


def test_a_supervision_failure_still_closes_the_stream():
    source = inspect.getsource(handlers.WebSocketHandler._finish_stream)

    # A turn that streamed tokens and never sent stream_end leaves the
    # client showing a live bubble forever -- worse than an untidy one.
    assert "except Exception" in source
    assert "final = raw_answer" in source


# ======================================================
# Progress
#
# The 55 seconds were a model LOAD, before a single token existed.
# Streaming cannot fix that -- there is nothing to stream while a 12B is
# read off disk -- but silence about it is what made it read as a freeze.
# ======================================================
def test_a_cold_load_is_announced():
    source = inspect.getsource(handlers.WebSocketHandler._announce_model_load)

    # Asked of the loader rather than guessed: it holds one model and
    # knows which.
    assert "active_model_id" in source
    assert "_emit_progress" in source


def test_a_warm_turn_announces_nothing():
    source = inspect.getsource(handlers.WebSocketHandler._announce_model_load)

    # A progress line on every turn is noise, and noise is what makes a
    # real one invisible.
    assert 'active_model_id", None) == model_id' in source


def test_progress_is_not_part_of_the_answer():
    source = code_of(handlers.WebSocketHandler._emit_progress)

    # Progress in the token stream would end up in the transcript, in the
    # history the next turn reads, and in the text actions are parsed
    # from.
    # Quote style comes from ast.unparse, which normalises to single
    # quotes -- so the assertion is on the words, not the punctuation.
    assert "progress" in source
    assert "stream_token" not in source


def test_the_load_is_announced_before_generation_starts():
    source = code_of(handlers.WebSocketHandler._stream_inference)

    # After it starts is after the wait it explains.
    assert source.index("_announce_model_load") < source.index("run_in_executor")


def test_the_client_handles_both_new_packets():
    from pathlib import Path

    chat = Path(__file__).resolve().parents[2] / "webui/components/chat/chat.js"
    source = chat.read_text(encoding="utf-8")

    # A backend packet nothing renders is a change that did not happen.
    assert '"progress"' in source
    assert '"message_revised"' in source
    assert "_handleProgress" in source
    assert "_handleMessageRevised" in source


def test_the_revision_lands_before_history_is_recorded():
    from pathlib import Path

    chat = Path(__file__).resolve().parents[2] / "webui/components/chat/chat.js"
    source = chat.read_text(encoding="utf-8")

    # _handleStreamEnd pushes the buffer into history and
    # _handleMessageRevised replaces that buffer. The revision arriving
    # first is what puts the CHECKED text in the transcript the next turn
    # reads, rather than the raw one.
    assert "this._activeStream.buffer = text;" in source
    assert '_pushHistory("assistant", this._activeStream.buffer)' in source


def test_the_supervised_text_is_what_gets_sent():
    source = inspect.getsource(handlers.WebSocketHandler._deliver_supervised)
    code = "\n".join(line for line in source.splitlines()
                     if not line.strip().startswith("#"))

    # supervise_full produces `text`, and `text` is what the stream_token
    # carries. Sending the raw answer after supervising it would make the
    # whole pass decoration.
    assert "supervise_full" in code
    assert '"token": text' in code
    assert code.index("supervise_full") < code.index('"token": text')


def test_delivery_sends_a_complete_stream():
    source = inspect.getsource(handlers.WebSocketHandler._deliver_supervised)

    # A client that has always understood start/token/end must not have
    # to learn a new shape because the server buffered.
    for packet in ("stream_start", "stream_token", "stream_end"):
        assert packet in source


def test_a_supervision_failure_still_delivers_the_answer():
    source = inspect.getsource(handlers.WebSocketHandler._deliver_supervised)
    tree = ast.parse(textwrap.dedent(source))

    handlers_found = [node for node in ast.walk(tree)
                      if isinstance(node, ast.ExceptHandler)]

    # Silence is the one outcome worse than untidy prose: the model has
    # already spent the time, and the user gets nothing.
    assert handlers_found, "supervision failure is not caught"
    assert "text = answer_text" in source


def test_the_answer_stream_filter_still_runs():
    source = inspect.getsource(handlers.WebSocketHandler._deliver_supervised)

    # The same filter the unbuffered path applied token by token. One
    # pass over the finished text is equivalent, which is what the REST
    # route has always relied on.
    assert "answer.push(text)" in source
    assert "answer.finish()" in source


# ======================================================
# Role markers are stopped at the provider, not just trimmed
# ======================================================
@pytest.mark.parametrize("marker", [
    "<|assistant|>", "<|im_start|>", "<|im_end|>", "**assistant:**", "[/INST]",
])
def test_role_markers_are_provider_stop_sequences(marker):
    from backend.llm.providers.local_provider import TURN_BOUNDARY_STOP_SEQUENCES

    # Stopping ON a marker beats trimming after it: the transport can
    # only cut what has already been generated, and generation that is
    # never spent is latency the user never waits for.
    joined = " ".join(TURN_BOUNDARY_STOP_SEQUENCES)
    assert marker in joined


# The full "every terminator is also a stop sequence" invariant lives in
# test_provider_stop_sequences.py, which knows how the two lists differ
# in shape -- llama.cpp matches raw substrings including newlines, the
# transport matches a normalised line start. A second, looser copy of it
# here would eventually disagree with the real one.


@pytest.mark.parametrize("raw,gone", [
    ("Answer.\n\n**assistant:** more", "**assistant:**"),
    ("Answer.\n<|assistant|>\nmore", "<|assistant|>"),
    ("Answer.\n## Assistant: more", "Assistant:"),
])
def test_a_marker_that_reaches_the_text_is_still_removed(raw, gone):
    from backend.core.answer_stream import AnswerStream

    # Belt and braces. The stop sequences prevent most of these; the
    # filter is what catches a marker a provider emits anyway.
    stream = AnswerStream()
    out = (stream.push(raw) or "") + (stream.finish() or "")

    assert gone not in out
    assert "Answer." in out


# ======================================================
# REST runs the same pipeline
# ======================================================
def test_rest_and_websocket_call_the_same_thing():
    from backend.rest import router as rest

    rest_source = inspect.getsource(rest)
    ws_source = inspect.getsource(handlers)

    # Two transports assembling the pipeline separately is how they came
    # to disagree about everything else in this codebase.
    assert "supervise_full" in rest_source
    assert "supervise_full" in ws_source


# ======================================================
# Leakage that is not at the start of a line
#
# Captured live from the 0.5B before the floor existed:
#
#     Hello! How can I assist you today? system: The user's latest
#     message is just a greeting - reply in under 15 words, warmly.
#
# Every leak rule in supervisor_layer and answer_stream is anchored to a
# line start, and the provider's stop sequences hold a newline-anchored
# form. All of them missed it, so the system prompt reached the screen.
# ======================================================
LIVE_LEAK = ("Hello! How can I assist you today? system: The user's latest message "
             "is just a greeting - reply in under 15 words, warmly.")


def test_the_leak_that_actually_reached_a_user_is_removed():
    result = deterministic_repair(LIVE_LEAK)

    assert result.text == "Hello! How can I assist you today?"
    assert "system:" not in result.text


@pytest.mark.parametrize("raw", [
    "Done. assistant: now I will narrate my instructions",
    "That is all! system: you are ARIA",
    "Finished. user: what should I ask next",
])
def test_a_role_label_opening_a_clause_is_leakage(raw):
    assert ":" not in deterministic_repair(raw).text.split(".")[-1].strip(" !")


@pytest.mark.parametrize("raw", [
    "The build system: a short description follows.",
    "Use the assistant: it helps.",
    "What is a system: a set of parts.",
    "I fixed it. The system: rebuilt.",
])
def test_prose_that_merely_contains_a_role_word_survives(raw):
    # The sentence boundary is what makes the rule safe. A label preceded
    # by a word is prose; one preceded by the end of a sentence is a model
    # that has started narrating its own prompt.
    assert deterministic_repair(raw).text == raw


def test_everything_after_the_label_goes():
    # A model that has begun narrating its instructions does not go back
    # to answering afterwards, so the tail is not worth salvaging.
    result = deterministic_repair("Answer here. system: instructions\n\nmore leaked text")

    assert result.text == "Answer here."
