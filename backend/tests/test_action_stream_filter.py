# backend/tests/test_action_stream_filter.py
#
# Keeping the action block off the screen while it streams.
#
# Unbuffering the stream brought back a problem buffering had hidden.
# Measured live: the user watched
#
#     ```json
#     {"tool": "edit_file", "path": "hello_world.py", "conte
#
# arrive one fragment at a time and then get replaced a second later by
# "Done: created hello_world.py". Machine syntax scrolling past is what
# action_render exists to prevent, and streaming had put it back.
#
# The fragments matter as much as the text: a provider splits tokens
# wherever it likes, so every case here is fed in small chunks and the
# markers land across boundaries. A filter that only works on whole lines
# would pass every test written the easy way and fail on the wire.

from __future__ import annotations

import pytest

from backend.core.action_stream_filter import ActionBlockFilter


def stream(text: str, chunk: int) -> tuple[str, bool]:
    """Feed the text in fixed-size chunks, as a provider would."""
    filter_ = ActionBlockFilter()
    out = "".join(filter_.push(text[i:i + chunk]) for i in range(0, len(text), chunk))
    return out + filter_.finish(), filter_.suppressed_a_block


ACTION = 'Adding it now.\n\n```json\n{"tool": "edit_file", "path": "a.py"}\n```\n'
CODE = "Here is the code:\n\n```python\nprint(1)\n```\n"


# ======================================================
# What is hidden
# ======================================================
@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 7, 13, 1000])
def test_an_action_block_never_reaches_the_screen(chunk):
    # Every chunk size, because "```json" split across two packets is the
    # normal case, not the edge case.
    out, suppressed = stream(ACTION, chunk)

    assert "tool" not in out
    assert "```json" not in out
    assert suppressed is True


@pytest.mark.parametrize("chunk", [1, 3, 7, 1000])
def test_the_prose_around_it_still_streams(chunk):
    out, _ = stream(ACTION, chunk)

    assert "Adding it now." in out


def test_an_unlabelled_fence_holding_json_is_also_hidden():
    # A model that writes ``` instead of ```json has still written an
    # action -- parse_actions reads those, so the display must hide them.
    out, suppressed = stream('```\n{"tool": "run_tests"}\n```\n', 4)

    assert "tool" not in out
    assert suppressed is True


def test_an_unterminated_action_shows_nothing():
    # A stream that ends mid-fence has nothing publishable left: half an
    # action helps nobody.
    out, _ = stream('```json\n{"tool": "edit_file", "path": "a', 5)

    assert out.strip() == ""


# ======================================================
# What is not
# ======================================================
@pytest.mark.parametrize("chunk", [1, 2, 4, 9, 1000])
def test_a_code_block_streams_exactly_as_written(chunk):
    # It is the code the user asked about, not scaffolding.
    out, suppressed = stream(CODE, chunk)

    assert "```python\nprint(1)\n```" in out
    assert suppressed is False


@pytest.mark.parametrize("chunk", [1, 3, 8])
def test_inline_backticks_survive(chunk):
    text = "Use `print()` for that, or ``double`` if you must."

    out, _ = stream(text, chunk)

    assert out == text


@pytest.mark.parametrize("chunk", [1, 4, 11])
def test_ordinary_prose_passes_through_unchanged(chunk):
    text = "An ordinary answer, with punctuation: commas, colons; and dashes -- fine."

    out, _ = stream(text, chunk)

    assert out == text


def test_prose_after_a_hidden_block_still_arrives():
    text = 'Adding it.\n\n```json\n{"tool": "edit_file"}\n```\nAll done.'

    out, _ = stream(text, 6)

    assert "Adding it." in out
    assert "All done." in out
    assert "tool" not in out


def test_a_code_block_and_an_action_block_together():
    text = (CODE + "\n" + '```json\n{"tool": "edit_file", "path": "a.py"}\n```\n')

    out, suppressed = stream(text, 5)

    assert "print(1)" in out
    assert "tool" not in out
    assert suppressed is True


# ======================================================
# It is a display filter, not a parser
# ======================================================
def test_the_raw_answer_is_untouched_by_this():
    import inspect

    from backend.websocket import handlers

    source = inspect.getsource(handlers.WebSocketHandler._stream_inference)

    # accumulated_tokens holds the RAW fragments and drives the actions.
    # If the filter reached those, hiding the block from the screen would
    # also hide it from the executor -- the proposal would vanish and
    # nothing would be staged.
    assert "accumulated_tokens.append(raw)" in source
    assert "action_blocks.push(answer.push(raw))" in source


def test_filtering_never_raises():
    for text in ("", "`", "```", "```json", "```json\n{", "x" * 5000):
        filter_ = ActionBlockFilter()
        assert isinstance(filter_.push(text), str)
        assert isinstance(filter_.finish(), str)
