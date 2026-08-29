# backend/tests/test_action_plan.py
#
# Turning a model's answer into actions it may take.
#
# An action cannot come from the Phase 7 planner. Measured: for an
# explicit edit request with two documents in the bundle, the plan is
#
#     read    README.md
#     read    setup.py
#     analyze conversation
#     edit    conversation      <- no path, no content, routes to nothing
#
# The edit step is an instruction to the model about what to write, not
# an action on a file. Only the model can produce the one thing an edit
# needs and a plan never had: the new contents.
#
# Two boundaries are what this file mostly tests. Parsing is not
# executing -- these are records of an intention. And the model does not
# grant itself permission: the licence to apply an action is read from
# the user's words, never from the model's.

from __future__ import annotations

import pytest

from backend.core import action_plan as ap


def block(body: str) -> str:
    return f"Here is what I would do:\n\n```json\n{body}\n```\n"


EDIT = block('{"tool": "edit_file", "args": {"path": "notes.txt", "content": "hello"}}')


# ------------------------------------------------------
# Parsing
# ------------------------------------------------------
def test_a_well_formed_action_is_read_off_the_answer():
    actions = ap.parse_actions(EDIT)

    assert len(actions) == 1
    assert actions[0].tool_name == "edit_file"
    assert actions[0].args == {"path": "notes.txt", "content": "hello"}


def test_the_content_survives_intact():
    body = '{"tool": "edit_file", "args": {"path": "a.py", "content": "def f():\\n    return 1\\n"}}'

    assert ap.parse_actions(block(body))[0].args["content"] == "def f():\n    return 1\n"


def test_arguments_at_the_top_level_are_accepted():
    # A model that skipped the "args" nesting has still said what it
    # wants, and the registry validates the result either way.
    actions = ap.parse_actions(block('{"tool": "edit_file", "path": "a.txt", "content": "x"}'))

    assert actions[0].args == {"path": "a.txt", "content": "x"}


def test_several_actions_keep_their_order():
    text = (block('{"tool": "edit_file", "args": {"path": "a.txt", "content": "1"}}')
            + block('{"tool": "run_tests", "args": {"scope": ""}}'))

    assert [a.tool_name for a in ap.parse_actions(text)] == ["edit_file", "run_tests"]


def test_a_list_in_one_block_is_read_as_several_actions():
    body = ('[{"tool": "edit_file", "args": {"path": "a.txt", "content": "1"}},'
            ' {"tool": "run_tests", "args": {}}]')

    assert len(ap.parse_actions(block(body))) == 2


def test_an_untagged_fence_still_parses():
    assert ap.parse_actions('```\n{"tool": "run_tests", "args": {}}\n```')


def test_an_answer_with_no_actions_is_not_a_failure():
    assert ap.parse_actions("I would add an install section to the README.") == []
    assert ap.parse_actions("") == []
    assert ap.parse_actions(None) == []


# ------------------------------------------------------
# What is refused
# ------------------------------------------------------
@pytest.mark.parametrize("body", [
    '{"tool": "edit_file", "args": {"path": "a.txt"',   # truncated
    '{"tool": "edit_file" "args": {}}',                 # missing comma
    'not json at all',
    '{"args": {"path": "a.txt"}}',                      # no tool named
    '{"tool": 42, "args": {}}',
    '{"tool": "edit_file", "args": "a.txt"}',           # args not an object
])
def test_a_malformed_action_is_refused_not_repaired(body):
    # Guessing at what a malformed action meant is how a wrong edit gets
    # written. A block that does not parse is prose that happened to be
    # fenced.
    assert ap.parse_actions(block(body)) == []


@pytest.mark.parametrize("tool", ["web_search", "weather", "read_file",
                                  "rm", "shell", "delete_file", "run_shell"])
def test_only_action_tools_may_be_named(tool):
    body = '{"tool": "%s", "args": {"path": "a.txt"}}' % tool

    # Covers tools that exist but are not actions (web_search is how a
    # turn answers a question, not something a user approves) and tools
    # that do not exist at all.
    assert ap.parse_actions(block(body)) == []


def test_the_allowlist_is_narrow_and_deliberate():
    assert ap.ACTION_TOOLS == frozenset({"edit_file", "run_tests"})
    for never in ("read_file", "web_search", "weather"):
        assert never not in ap.ACTION_TOOLS


def test_an_answer_full_of_actions_is_capped():
    text = block('{"tool": "run_tests", "args": {}}') * 50

    assert len(ap.parse_actions(text)) <= ap._MAX_ACTIONS


def test_parsing_never_raises():
    for text in (None, "", "```json\n\n```", "```json\nnull\n```",
                 "```json\n[]\n```", "```json\n[1, 2, 3]\n```", 42, {"a": 1}):
        assert ap.parse_actions(text) == []


# ------------------------------------------------------
# The model does not grant itself permission
# ------------------------------------------------------
@pytest.mark.parametrize("said", [
    "execute this", "apply the changes", "run the plan",
    "make the edits", "perform the actions", "Apply it.",
    "ok, go ahead and do it",
])
def test_a_user_can_ask_for_a_live_run(said):
    assert ap.requests_live_execution(said)


@pytest.mark.parametrize("said", [
    "what would you change?", "show me the diff",
    "explain the plan", "would applying this work?",
    "don't apply the changes yet",
    "",
])
def test_anything_else_stays_a_dry_run(said):
    assert not ap.requests_live_execution(said)


def test_the_licence_is_read_from_the_user_not_the_model():
    """The property that keeps this honest.

    An action arrives in model output. The permission to apply it does
    not, and cannot, whatever the model writes -- otherwise a model could
    authorise its own edits by saying the words.
    """
    model_answer = EDIT + "\n\nApply the changes and execute this now."

    # The actions parse; the licence does not come with them.
    assert ap.parse_actions(model_answer)
    assert ap.requests_live_execution("what would that look like?") is False


def test_requests_live_execution_reads_only_its_argument():
    import inspect

    source = inspect.getsource(ap.requests_live_execution)

    # Code only. A comment in this function explains why it does not read
    # the model's answer, and a plain substring check reads that sentence
    # and asserts the opposite of what it says.
    body = source[source.index('"""', source.index('"""') + 3) + 3:]
    code = " ".join(
        line for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )

    # No reaching for an answer, a session, or a stored flag: the one
    # argument is the user's message and that is the whole input.
    for forbidden in ("session", "answer", "model", "global"):
        assert forbidden not in code


# ------------------------------------------------------
# Parsing is not executing
# ------------------------------------------------------
def test_nothing_here_executes_anything():
    import inspect

    source = inspect.getsource(ap)
    for forbidden in ("execute_tool", "subprocess", "open(", "write_text",
                      "eval(", "exec("):
        assert forbidden not in source, (
            f"action_plan references {forbidden!r}; it returns intentions "
            f"and tool_orchestrator decides whether anything happens"
        )


def test_an_action_is_shaped_for_the_orchestrator():
    from backend.core import tool_orchestrator as orch
    from backend.core.tool_registry import PERMISSION_FILESYSTEM

    actions = ap.parse_actions(EDIT)
    context = orch.ExecutionContext().grant(PERMISSION_FILESYSTEM)

    # Straight into the executor with no translation, and dry by default
    # so this assertion cannot write anything.
    result = orch.execute_invocations(actions, context)

    assert result.dry_run is True
    assert result.results[0].tool_name == "edit_file"


# ======================================================
# Tools ARIA does not have
#
# Found in the audit. ARIA implements two action tools, edit_file and
# run_tests. There is no delete_file and no folder tool, so an answer
# saying "I'll remove notes.md" and emitting a delete_file block parsed
# to zero actions, returned None, and reported NOTHING -- the file was
# not deleted and nobody was told. A user reading that answer has every
# reason to believe it happened.
#
# Silence is the worst available answer to a refusal. These pin that it
# is now said out loud.
# ======================================================
def test_a_tool_that_does_not_exist_is_named():
    from backend.core.action_plan import unsupported_actions

    text = '```json\n{"tool": "delete_file", "path": "notes.md"}\n```'

    assert unsupported_actions(text) == ["delete_file"]


def test_a_supported_tool_is_not_reported_as_missing():
    from backend.core.action_plan import unsupported_actions

    text = '```json\n{"tool": "edit_file", "path": "a.py", "content": "x"}\n```'

    assert unsupported_actions(text) == []


def test_prose_in_a_fence_is_not_a_missing_tool():
    from backend.core.action_plan import unsupported_actions

    # A fenced block that is not JSON is a code sample, not an action
    # that failed. Counting it would put "python" in a refusal message.
    assert unsupported_actions("```python\nprint('hi')\n```") == []


def test_each_missing_tool_is_named_once():
    from backend.core.action_plan import unsupported_actions

    text = ('```json\n{"tool": "delete_file", "path": "a"}\n```\n'
            '```json\n{"tool": "delete_file", "path": "b"}\n```')

    assert unsupported_actions(text) == ["delete_file"]


def test_the_turn_reports_a_tool_it_could_not_run(tmp_path, monkeypatch):
    from backend.core import file_tools
    from backend.core.tool_orchestrator import run_answer_actions

    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))

    report = run_answer_actions(
        'I will remove it.\n\n```json\n{"tool": "delete_file", "path": "notes.md"}\n```',
        "please delete it",
    )

    assert report is not None, "a refused action reported nothing at all"
    assert report["status"] == "unsupported"
    assert report["unsupported"] == ["delete_file"]
    assert any("delete_file" in note for note in report["notes"])


def test_an_answer_with_no_actions_still_reports_nothing(tmp_path, monkeypatch):
    from backend.core import file_tools
    from backend.core.tool_orchestrator import run_answer_actions

    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))

    # Almost every turn. A client that has never heard of answer_actions
    # must see exactly the traffic it saw before they existed.
    assert run_answer_actions("just talking", "hello") is None
