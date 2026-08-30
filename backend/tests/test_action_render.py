# backend/tests/test_action_render.py
#
# What the user reads when the model proposes an action.
#
# A fenced json block is how the model tells the machine what it wants,
# and it is not how ARIA should tell a person. Asked to create a file,
# mistral-7b answered with the block and nothing else, so the whole reply
# on screen was:
#
#     ```json
#     {"tool": "create_file", "path": "hello_world.py"}
#     ```
#
# Everything behind it worked -- parsed, staged, waiting for consent. The
# user was shown machine syntax and no sentence.
#
# The one assertion that matters most here is the last section: the
# description is built from the PARSED invocation, so it cannot describe
# something other than what would run.

from __future__ import annotations

import pytest

from backend.core.action_plan import parse_actions
from backend.core.action_render import render_actions_for_reading as render


def block(payload: str) -> str:
    return f"```json\n{payload}\n```"


# ======================================================
# The block is replaced
# ======================================================
def test_a_bare_action_block_becomes_a_sentence():
    out = render(block('{"tool": "create_file", "path": "hello_world.py"}'))

    assert "```json" not in out
    assert "hello_world.py" in out
    assert "yes, do it" in out


def test_prose_the_model_wrote_is_kept():
    out = render("I will add the script.\n\n"
                 + block('{"tool": "edit_file", "path": "a.py", "content": "print(1)"}'))

    # An answer that already explains itself keeps its own words and
    # gains the confirmation line.
    assert "I will add the script." in out
    assert "```json" not in out
    assert "yes, do it" in out


def test_several_actions_are_listed():
    out = render(block('{"tool": "delete_file", "path": "notes.md"}')
                 + "\n" + block('{"tool": "move_file", "path": "a.py", "dest": "src/a.py"}'))

    assert "delete `notes.md`" in out
    assert "move `a.py` to `src/a.py`" in out


def test_an_answer_with_no_actions_is_untouched():
    # Almost every turn.
    for text in ("Just talking.", "", "Here is a thought.\n\nAnd another."):
        assert render(text) == text


def test_a_code_block_is_not_scaffolding():
    text = "Here is python:\n\n```python\nprint(1)\n```"

    # It is the code being discussed, not the action syntax.
    assert render(text) == text


def test_a_code_block_survives_beside_an_action():
    text = ("Here is the script:\n\n```python\nprint(1)\n```\n\n"
            + block('{"tool": "edit_file", "path": "a.py", "content": "print(1)"}'))

    out = render(text)

    assert "```python\nprint(1)\n```" in out
    assert "```json" not in out


# ======================================================
# What each action reads as
# ======================================================
@pytest.mark.parametrize("payload,expected", [
    ('{"tool": "delete_file", "path": "notes.md"}', "delete `notes.md`"),
    ('{"tool": "create_folder", "path": "docs"}', "create the folder `docs`"),
    ('{"tool": "move_file", "path": "a.py", "dest": "src/a.py"}', "move `a.py` to `src/a.py`"),
    ('{"tool": "rename_file", "path": "a.py", "new_name": "b.py"}', "rename `a.py` to `b.py`"),
    ('{"tool": "copy_file", "path": "a.py", "dest": "b.py"}', "copy `a.py` to `b.py`"),
    ('{"tool": "run_tests"}', "run the tests"),
])
def test_each_action_reads_as_what_it_does(payload, expected):
    assert expected in render(block(payload))


def test_a_file_with_contents_says_how_much():
    import json

    # Built with json.dumps rather than written out: a literal newline
    # inside a JSON string is invalid JSON, the block parses to nothing,
    # and the test then asserts against an unrendered answer.
    payload = json.dumps({"tool": "edit_file", "path": "a.py",
                          "content": "print(1)\nprint(2)"})

    out = render(block(payload))

    assert "write `a.py` (2 lines)" in out


def test_an_empty_file_is_named_as_empty():
    # A file proposed without contents is real, committable and useless,
    # and the only cheap moment to say "no, put the script in it" is
    # before agreeing to it.
    out = render(block('{"tool": "create_file", "path": "a.py"}'))

    assert "empty" in out
    assert "no contents were provided" in out


# ======================================================
# The description cannot disagree with what would run
# ======================================================
@pytest.mark.parametrize("payload", [
    '{"tool": "create_file", "path": "hello_world.py"}',
    '{"tool": "delete_file", "path": "notes.md"}',
    '{"tool": "move_file", "path": "a.py", "dest": "src/a.py"}',
])
def test_every_path_shown_is_a_path_that_would_be_acted_on(payload):
    text = block(payload)

    rendered = render(text)
    for invocation in parse_actions(text):
        # Built from the same invocation the executor receives, so the
        # sentence and the effect cannot drift apart.
        assert invocation.args["path"] in rendered


def test_rendering_never_raises():
    for text in (None, "", "```json\n{not json}\n```", "```json\n[]\n```", "x" * 5000):
        assert isinstance(render(text), str)


# ======================================================
# The raw answer still drives the actions
# ======================================================
def test_the_transport_parses_actions_from_the_raw_answer():
    import inspect

    from backend.websocket import handlers

    source = inspect.getsource(handlers.WebSocketHandler._stream_inference)

    # If display and actions shared one string, rendering would take the
    # block away from the executor: the proposal would vanish from the
    # screen AND nothing would be staged.
    assert "raw_answer" in source
    assert "_run_answer_actions(self._supervise(raw_answer" in source


def test_rendering_a_rendered_answer_changes_nothing():
    once = render(block('{"tool": "create_file", "path": "a.py"}'))

    # It runs on the display path only, but a double application must not
    # compound -- there are no blocks left to describe.
    assert render(once) == once


def test_an_unfenced_action_is_described_not_printed():
    import json

    # Stripping only fences left the raw JSON on screen beside a sentence
    # describing it.
    out = render(json.dumps({"tool": "edit_file", "path": "hello_world.py",
                             "content": 'print("hi")\n'}))

    assert '"tool"' not in out
    assert "write `hello_world.py`" in out


def test_prose_around_an_unfenced_action_survives():
    import json

    out = render("I will remove it. "
                 + json.dumps({"tool": "delete_file", "path": "a.py"})
                 + " Shall I?")

    assert "I will remove it." in out
    assert "Shall I?" in out
    assert "delete `a.py`" in out
    assert '"tool"' not in out


# ======================================================
# Saying what happened, not what might
# ======================================================
def test_an_already_staged_action_is_not_asked_about_again():
    out = render(block('{"tool": "edit_file", "path": "a.py", "content": "x"}'),
                 staged=True)

    # Passing staged=False while the executor had already staged produced
    # a reply telling the user to say "yes, do it" about work that was
    # already done.
    assert "yes, do it" not in out
    assert "Staged" in out
    assert "commit it in Settings" in out


def test_an_unconfirmed_action_asks_for_confirmation():
    out = render(block('{"tool": "edit_file", "path": "a.py", "content": "x"}'),
                 staged=False)

    assert "yes, do it" in out


def test_the_transport_tells_the_renderer_what_happened():
    import inspect

    from backend.websocket import handlers

    source = inspect.getsource(handlers.WebSocketHandler._deliver_supervised)

    # From the same function the executor consults, so the sentence and
    # the outcome cannot disagree.
    assert "requests_live_execution" in source
    assert "staged=" in source


# ======================================================
# ARIA's own text, out of the model's transcript
# ======================================================
def test_scaffolding_is_removed_from_a_previous_turn():
    from backend.core.action_render import strip_scaffolding

    rendered = render(block('{"tool": "edit_file", "path": "a.py", "content": "x"}'))

    cleaned = strip_scaffolding(rendered)

    # The model imitates what it sees itself having said: with the
    # rendering in the transcript it produced three nested "Here is what
    # I would do:" headings in one reply, one of them empty.
    assert "Here is what I would do:" not in cleaned
    assert "yes, do it" not in cleaned
    assert "write `a.py`" in cleaned


def test_stripping_leaves_the_models_own_words():
    from backend.core.action_render import strip_scaffolding

    text = "I will add the script.\n\nHere is what I would do:\n\n- write `a.py` (1 line)"

    cleaned = strip_scaffolding(text)

    assert "I will add the script." in cleaned
    assert "- write `a.py` (1 line)" in cleaned


def test_the_orchestrator_cleans_the_history_it_sends():
    import inspect

    from backend.core import turn_orchestrator

    source = inspect.getsource(turn_orchestrator.orchestrate_turn)

    assert "strip_scaffolding" in source
    # On a copy: request.messages is the caller's list and the
    # orchestrator mutates nothing it was handed.
    assert "replace(request, messages=" in source
