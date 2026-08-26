# backend/tests/test_answer_stream.py
#
# The answer, separated from the scaffolding, before anything is sent.
#
# The three inputs below are not invented: they are what the models on
# this machine actually returned for "how much is MSFT trading at" with a
# stubbed web_search, captured through the real WebSocket path.
#
#   gpt-4       a clean sentence
#   phi-3-mini  the right answer, wrapped in echoed prompt sections, then
#               "Instruction:" and a fabricated new turn
#   qwen-0.5b   "not found" forever
#
# The filter has to leave the first alone, rescue the second, and stop the
# third. The rule that makes that safe is that a scaffolding prefix is
# stripped and the text after it kept -- no rule in the module can delete
# an answer, only a label or a fabricated turn.

from __future__ import annotations

import pathlib

import pytest

from backend.core.answer_stream import AnswerStream


def stream(raw: str, chunk: int = 3) -> tuple[str, dict]:
    """Push `raw` through in small chunks, as a provider would."""
    return stream_with(raw, chunk=chunk)


def stream_with(raw: str, chunk: int = 3, **options) -> tuple[str, dict]:
    """As stream(), with AnswerStream options."""
    filt = AnswerStream(**options)
    out = "".join(filt.push(raw[i:i + chunk]) for i in range(0, len(raw), chunk))
    return out + filt.finish(), filt.stats


# Captured verbatim from phi-3-mini-4k-instruct-q4.
PHI3_RAW = (
    ' "MSFT is currently trading at $412.30, up 1.2% (source: example.com)"\n'
    "\n(Note 12)\n\n"
    "- Response: MSFT is currently trading at $412.30, up 1.2% (source: example.com)\n"
    "\n\nSolution:\n\n"
    "The Microsoft (MSFT) stock is currently trading at $412.30, which represents "
    "a 1.2% increase. This information was obtained from the web search results "
    "sourced at example.com.\n\n\nInstruction:\n"
)

GPT4_RAW = "Microsoft (MSFT) is currently trading at $412.30, which is up 1.2%."


# ======================================================
# A good answer is never touched
# ======================================================
def test_a_clean_answer_passes_through_byte_for_byte():
    out, stats = stream(GPT4_RAW)
    assert out == GPT4_RAW
    assert not any(stats.values())


@pytest.mark.parametrize("chunk", [1, 2, 5, 13, 500])
def test_the_result_does_not_depend_on_how_tokens_are_split(chunk):
    """Chunking is the provider's business and must not change the answer.

    Worth pinning because the filter holds a partial line: a marker
    arriving split across two tokens has to be recognised just the same.
    """
    assert stream(PHI3_RAW, chunk=chunk)[0] == stream(PHI3_RAW, chunk=1)[0]


def test_markdown_survives_intact():
    md = "Here are the steps:\n\n1. First **thing**\n2. Second\n\n```py\nx = 1\n```\n\nDone."
    assert stream(md)[0] == md


# ======================================================
# The reasoning preamble is not streamed
# ======================================================
def test_the_echoed_scaffolding_is_stripped_but_the_answer_is_kept():
    out, stats = stream(PHI3_RAW)

    # The labels are gone.
    assert "- Response:" not in out
    assert "Solution:" not in out

    # Everything they were labelling is still here.
    assert "$412.30" in out
    assert "1.2%" in out
    assert "example.com" in out
    assert stats["stripped_prefixes"] == 2


def test_generation_past_the_answer_is_cut_off():
    """"Instruction:" is the model starting a turn it invented."""
    out, stats = stream(PHI3_RAW)
    assert "Instruction:" not in out
    assert stats["terminated_at"] == "instruction:"


@pytest.mark.parametrize("terminator", ["User:", "System:", "Instruction:", "User Query:"])
def test_a_fabricated_turn_boundary_ends_the_answer(terminator):
    out, _ = stream(f"The real answer is 42.\n{terminator} what about 43?\n")
    assert "The real answer is 42." in out
    assert "43" not in out


def test_a_repetition_loop_collapses_to_one_line():
    """qwen-0.5b's failure mode: the same line until max_tokens runs out."""
    out, stats = stream("not found\n" * 40)
    assert out.strip() == "not found"
    assert stats["dropped_repeats"] == 39


def test_reveal_reasoning_turns_the_whole_thing_off():
    """The scaffolding is suppressed by default, not made unreachable."""
    filt = AnswerStream(reveal_reasoning=True)
    assert filt.push(PHI3_RAW) + filt.finish() == PHI3_RAW


# ======================================================
# Tool results reach the user; tool scaffolding does not
# ======================================================
def test_the_tool_result_survives_and_its_scaffolding_does_not():
    """Requirement: a concrete result and its source, with no raw payload.

    The price and the source are the answer. "Tool Results:" and the
    step id around them are how the prompt was built, and the user has no
    use for either.
    """
    raw = (
        "Tool Results:\n"
        "- web_search (step1): Microsoft (MSFT) is trading at $412.30 "
        "(source: example.com)\n"
        "\nMicrosoft is trading at $412.30, according to example.com.\n"
    )
    out, _ = stream(raw)

    assert "$412.30" in out
    assert "example.com" in out
    assert "Tool Results:" not in out


# ======================================================
# Degeneration after a correct answer
# ======================================================
# Both of these arrive *after* the model has already answered, which is
# what makes cutting them safe. Captured from the two local models on this
# machine once the prompt stopped claiming there was no evidence.

# qwen2.5-0.5b: the right answer, then the same two citations forever, all
# on one line -- so the line-level repetition guard never sees it.
QWEN_LOOP = (
    " web_search (step1): Microsoft (MSFT) is trading at $412.30, up 1.2% "
    "(source: example.com). "
    + "(note: no other information is provided) "
      "(file:web_search_result.md#search_result) " * 12
)

# phi-3-mini: the right answer, then a conversation that never happened,
# written in the user's voice using the prompt's own transcript format.
PHI3_FABRICATED_TURN = (
    " Microsoft (MSFT) is trading at $412.30, according to example.com.\n"
    "\n- [user] how much is MSFT trading at\n"
    "\n- [assistant] Currently, Microsoft (MSFT) is trading at $412.30.\n"
    "\nAlright, here's the updated and refined solution:\n"
)


def test_a_loop_inside_one_line_is_cut_off():
    out, stats = stream(QWEN_LOOP)

    assert "$412.30" in out
    assert stats["stopped_looping"] is True
    # Twelve copies went in. The reader should not see twelve.
    assert out.count("file:web_search_result.md") <= 2


def test_an_echoed_tool_result_line_loses_its_step_id_not_its_finding():
    out, _ = stream(QWEN_LOOP)

    assert "web_search (step1):" not in out
    assert out.lstrip().startswith("Microsoft (MSFT) is trading at $412.30")


def test_a_conversation_the_model_invented_is_cut_off():
    """It is not merely noise -- it puts words in the user's mouth."""
    out, stats = stream(PHI3_FABRICATED_TURN)

    assert "Microsoft (MSFT) is trading at $412.30" in out
    assert "- [user]" not in out
    assert "- [assistant]" not in out
    assert "refined solution" not in out
    assert stats["terminated_at"] == "- [user]"


@pytest.mark.parametrize("speaker", ["[user]", "[assistant]", "[system]"])
def test_every_transcript_speaker_ends_the_answer(speaker):
    out, _ = stream(f"The answer is 42.\n{speaker} and what about 43?\n")
    assert "42" in out
    assert "43" not in out


# ------------------------------------------------------
# The loop guard must not fire on ordinary writing
# ------------------------------------------------------
@pytest.mark.parametrize("text", [
    "The build is very very very slow today.",
    "Scores: 10, 10, 10, 10, 10, 10, 10, 10.",
    "It costs $5.00 $5.00 $5.00",
])
def test_short_natural_repetition_is_left_alone(text):
    """A person repeating themselves is not a model looping."""
    out, stats = stream(text)
    assert out == text
    assert stats["stopped_looping"] is False


@pytest.mark.parametrize("rule", [
    "Section ------------------------------------ end",
    "Header ========================================== done",
    "Dots .................................... done",
    "Zigzag -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-= done",
])
def test_a_horizontal_rule_is_not_a_loop(rule):
    """Perfectly periodic and perfectly legitimate.

    A run built from one or two distinct characters is punctuation, not a
    model that has run out of things to say.
    """
    out, stats = stream(rule)
    assert out == rule
    assert stats["stopped_looping"] is False


def test_a_long_answer_with_a_repeated_phrase_survives():
    """The same phrase twice in a paragraph is normal prose.

    The guard needs the copies to be back to back; separated by other
    words, they carry information and stay.
    """
    text = (
        "The pipeline compiles shaders first. Then it links them. "
        "The pipeline compiles shaders first only when the cache is cold."
    )
    out, stats = stream(text)
    assert out == text
    assert stats["stopped_looping"] is False


def test_nothing_can_delete_the_answer_text():
    """The invariant the whole design rests on.

    Every rule except the terminator strips a label and keeps what follows,
    so a sentence can lose a prefix but never its content.
    """
    for label in ("Answer:", "Response:", "Solution:", "Final answer:", "Output:"):
        out, _ = stream(f"{label} Microsoft is at $412.30.\n")
        assert "Microsoft is at $412.30." in out
        assert label not in out

# ======================================================
# Scaffolding embedded in a sentence
# ======================================================
# The line-anchored rule cannot see an echo that arrives mid-sentence,
# which is how verification caught this one:
#
#     Microsoft is trading at $412.30 (Tool Result: web_search (step1)).
#
# Removing it means un-sending text, so the filter holds a short tail of
# each line back. The trade is a bounded lag, not a line-at-a-time stutter.
def test_an_inline_tool_echo_is_removed_and_the_sentence_survives():
    out, stats = stream(
        "Currently, Microsoft (MSFT) is trading at $412.30, as reported by "
        "example.com (Tool Result: web_search (step1))."
    )
    assert "$412.30" in out
    assert "example.com" in out
    assert "Tool Result" not in out
    assert "step1" not in out
    assert stats["stripped_inline"] >= 1


@pytest.mark.parametrize("raw,keep", [
    ("Microsoft is at $412.30 Tool Result: the lookup said so.", "the lookup said so"),
    ("The price web_search (step1) came from a lookup.", "The price came from a lookup."),
    ("Result (Tool Result: web_search (step1)) follows.", "follows"),
])
def test_surrounding_text_is_preserved(raw, keep):
    out, _ = stream(raw)
    assert keep in out


def test_removing_an_echo_does_not_leave_ragged_whitespace():
    """The gap it leaves usually arrives a token after the echo does."""
    out, _ = stream("The price web_search (step1) came from a lookup.")
    assert "  " not in out
    assert out.strip() == "The price came from a lookup."


def test_an_answer_with_no_echo_is_untouched():
    answer = "Microsoft (MSFT) is trading at $412.30 (up 1.2%) per example.com."
    assert stream(answer)[0] == answer


# ------------------------------------------------------
# Prefixes that leaked during verification
# ------------------------------------------------------
@pytest.mark.parametrize("prefix", ["### Response", "## Response", "support:"])
def test_the_leaked_prefixes_are_stripped(prefix):
    out, _ = stream(f"{prefix}\nMicrosoft is trading at $412.30.\n")
    assert "Microsoft is trading at $412.30." in out
    assert prefix not in out


def test_those_words_mid_sentence_are_left_alone():
    """A prefix rule fires at the start of a line, not inside one."""
    for text in ("We discussed the support: ticket system yesterday.",
                 "The server sent a 200 Response after the retry."):
        assert stream(text)[0] == text


# ======================================================
# Near-duplicate sentences
# ======================================================
# Measured on this repo with the module's own similarity function:
#
#     real paraphrase loops        0.742   0.822   1.000
#     legitimately distinct pairs  0.351   0.367   0.643   0.881
#
# The ranges are inverted -- catching the 0.742 loop needs a threshold
# below the 0.881 legitimate pair -- so no lexical threshold separates
# them. The default is set where it cannot cut correct text, and these
# tests pin that choice rather than pretending it is a solved problem.
PARAPHRASE_LOOP = (
    "Microsoft (MSFT) is currently trading at $412.30, which is an increase "
    "of 1.2% (source: example.com). "
    "Based on a recent web search, Microsoft (MSFT) is currently trading at "
    "$412.30, experiencing a 1.2% increase (source: example.com). "
    "And a third restatement follows here."
)

# Two sentences that read almost identically and say opposite things.
NEAR_IDENTICAL_BUT_DISTINCT = (
    "This function returns the active model id. "
    "This function returns the fallback model id. Both are used."
)


def test_a_restated_sentence_is_truncated_at_a_low_enough_threshold():
    out, stats = stream_with(PARAPHRASE_LOOP, similarity_threshold=0.70)
    assert stats["stopped_repeating_sentence"] is True
    assert "third restatement" not in out
    assert "$412.30" in out, "the first, correct sentence must survive"


def test_the_default_threshold_does_not_cut_correct_text():
    """The failure this default exists to avoid.

    At 0.85 the two sentences below -- which say opposite things -- are
    judged duplicates and the answer is truncated. The shipped default is
    above that.
    """
    out, stats = stream(NEAR_IDENTICAL_BUT_DISTINCT)
    assert stats["stopped_repeating_sentence"] is False
    assert "fallback model id" in out

    _, cut = stream_with(NEAR_IDENTICAL_BUT_DISTINCT, similarity_threshold=0.85)
    assert cut["stopped_repeating_sentence"] is True, (
        "if this stops being true the measured overlap has changed and the "
        "default should be re-derived"
    )


def test_a_legitimate_multi_sentence_answer_is_never_truncated():
    answer = (
        "The pipeline compiles shaders first. Then it links them into a single "
        "binary. Failures at the shader stage usually mean a missing include path."
    )
    out, stats = stream(answer)
    assert out.strip() == answer.strip()
    assert stats["stopped_repeating_sentence"] is False


def test_short_sentences_are_not_compared():
    """"Not found." twice is a model being terse, not a loop."""
    out, stats = stream("Not found. Not found. The build log explains why it failed.")
    assert stats["stopped_repeating_sentence"] is False


def test_the_threshold_is_configurable_and_documented():
    from backend.core import answer_stream

    assert 0.0 < answer_stream.SENTENCE_SIMILARITY_THRESHOLD <= 1.0
    assert AnswerStream().similarity_threshold == answer_stream.SENTENCE_SIMILARITY_THRESHOLD
    assert AnswerStream(similarity_threshold=0.5).similarity_threshold == 0.5
    # The rationale lives with the constant, not in a commit message.
    source = pathlib.Path(answer_stream.__file__).read_text(encoding="utf-8")
    assert "0.881" in source, "the measured overlap should stay recorded"
