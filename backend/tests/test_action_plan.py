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
