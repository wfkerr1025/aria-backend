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
                                  "rm", "shell", "format_disk", "run_shell"])
def test_only_action_tools_may_be_named(tool):
    body = '{"tool": "%s", "args": {"path": "a.txt"}}' % tool

    # Covers tools that exist but are not actions (web_search is how a
    # turn answers a question, not something a user approves) and tools
    # that do not exist at all.
    assert ap.parse_actions(block(body)) == []


def test_the_allowlist_is_narrow_and_deliberate():
    # Seven now, not two. The five that were added change the SHAPE of
    # the tree -- delete, move, rename, copy, mkdir -- and they are safe
    # to name here for a structural reason rather than a careful one:
    # their handlers stage into fs_plan's journal and contain no code
    # that touches the project. Widening this set widened what ARIA may
    # PROPOSE, not what it may do unasked.
    assert ap.ACTION_TOOLS == frozenset({
        "edit_file", "run_tests",
        "delete_file", "create_folder", "move_file", "rename_file", "copy_file",
    })
    for never in ("read_file", "web_search", "weather"):
        assert never not in ap.ACTION_TOOLS


def test_no_action_tool_can_reach_the_project_by_itself():
    # The invariant behind the widening. Every mutating action either
    # writes into the ghost workspace (edit_file, redirected by
    # tool_orchestrator) or appends to the staged plan (the five below).
    # None of them has a path to the project; only commit does.
    import inspect

    from backend.core import tool_registry

    source = inspect.getsource(tool_registry._staging_handler)
    for forbidden in ("unlink", "rmtree", "os.remove", "shutil.move", "mkdir"):
        assert forbidden not in source, (
            f"the staging handler calls {forbidden}; it must only record")


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
# run_tests. There was no delete_file and no folder tool, so an answer
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

    text = '```json\n{"tool": "send_email", "to": "a@b.c"}\n```'

    assert unsupported_actions(text) == ["send_email"]


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

    text = ('```json\n{"tool": "send_email", "to": "a"}\n```\n'
            '```json\n{"tool": "send_email", "to": "b"}\n```')

    assert unsupported_actions(text) == ["send_email"]


def test_the_turn_reports_a_tool_it_could_not_run(tmp_path, monkeypatch):
    from backend.core import file_tools
    from backend.core.tool_orchestrator import run_answer_actions

    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))

    report = run_answer_actions(
        'I will remove it.\n\n```json\n{"tool": "send_email", "to": "a@b.c"}\n```',
        "please delete it",
    )

    assert report is not None, "a refused action reported nothing at all"
    assert report["status"] == "unsupported"
    assert report["unsupported"] == ["send_email"]
    assert any("send_email" in note for note in report["notes"])


def test_an_answer_with_no_actions_still_reports_nothing(tmp_path, monkeypatch):
    from backend.core import file_tools
    from backend.core.tool_orchestrator import run_answer_actions

    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))

    # Almost every turn. A client that has never heard of answer_actions
    # must see exactly the traffic it saw before they existed.
    assert run_answer_actions("just talking", "hello") is None


# ======================================================
# Telling the model the tools exist
#
# Everything below the model was built first and none of it ever ran.
# parse_actions read a block, tool_orchestrator executed it, fs_plan
# staged it, two consents gated it, the Control Center rendered it -- and
# nothing asked the model to write one. Asked to create a file, ARIA
# answered "I'm unable to directly create files on your system", which
# was true from where it was standing.
# ======================================================
def test_the_brief_names_every_action_tool():
    from backend.core.action_plan import ACTION_TOOLS
    from backend.core.tool_brief import action_tool_brief

    brief = action_tool_brief()

    for tool in ACTION_TOOLS:
        assert tool in brief, f"{tool} is registered but the model is never told about it"


def test_the_brief_uses_the_registrys_argument_names():
    from backend.core.tool_brief import action_tool_brief
    from backend.core.tool_registry import get_tool_schema

    brief = action_tool_brief()

    # Generated, not written out. A hand-kept list would be right on the
    # day it was written and would then drift -- the model would emit
    # `filename` where the tool wants `path`, the invocation would be
    # discarded, and the symptom would be an action that silently does
    # not happen.
    for tool in ("edit_file", "move_file", "rename_file"):
        schema = get_tool_schema(tool)
        for name, spec in (schema.parameters or {}).items():
            if isinstance(spec, dict) and spec.get("required"):
                assert name in brief


def test_the_brief_never_tells_the_model_it_can_apply_anything():
    from backend.core.tool_brief import action_tool_brief

    brief = action_tool_brief().lower()

    # The block is a PROPOSAL. A brief that said "you can edit files"
    # would produce a model announcing changes it has not made, which is
    # the failure the staging system exists to prevent, reintroduced
    # through its own instructions.
    assert "proposal" in brief
    assert "nothing happens until the user agrees" in brief
    # And `confirm` is set by the orchestrator from the user's words. A
    # model that knew about the flag could grant itself the consent.
    assert "confirm=true" not in brief.replace(" ", "")


def test_the_brief_reaches_a_tool_turn_and_not_a_chat_turn():
    import inspect

    from backend.core import turn_orchestrator

    source = inspect.getsource(turn_orchestrator.orchestrate_turn)

    # On ordinary chat the brief is context spent to make a greeting more
    # likely to propose a file operation.
    assert "action_tool_brief" in source
    assert "model_router.TURN_TOOLS" in source


# ------------------------------------------------------
# What the model actually calls things
# ------------------------------------------------------
@pytest.mark.parametrize("said,meant", [
    ("create_file", "edit_file"),
    ("write_file", "edit_file"),
    ("new_file", "edit_file"),
    ("mkdir", "create_folder"),
    ("make_folder", "create_folder"),
    ("remove_file", "delete_file"),
    ("rename", "rename_file"),
])
def test_a_models_own_vocabulary_is_normalised(said, meant):
    # Told in the brief that there is no create_file and that edit_file
    # makes a missing path, mistral-7b proposed create_file twice. The
    # intent was never in doubt and the block parsed to nothing, which
    # looks exactly like the refusal the brief was written to fix.
    #
    # Prompt wording is a weak lever on a 7B; normalising is exact. No
    # alias widens anything -- each lands on a tool that stages and needs
    # both consents.
    text = block(f'{{"tool": "{said}", "path": "a.py", "dest": "b.py", "new_name": "b.py"}}')

    parsed = ap.parse_actions(text)

    assert len(parsed) == 1
    assert parsed[0].tool_name == meant


def test_an_alias_is_not_also_reported_as_missing():
    # One action must not produce two contradictory messages: run
    # correctly as edit_file AND be reported as a tool ARIA does not have.
    text = block('{"tool": "create_file", "path": "a.py"}')

    assert len(ap.parse_actions(text)) == 1
    assert ap.unsupported_actions(text) == []


def test_a_genuinely_unknown_tool_is_still_reported():
    text = block('{"tool": "send_email", "to": "a@b.c"}')

    assert ap.parse_actions(text) == []
    assert ap.unsupported_actions(text) == ["send_email"]


def test_a_create_with_no_content_is_an_empty_file():
    # "Create hello_world.py" with no content is a request for an empty
    # file, not a malformed action. Refusing would be technically right
    # and would leave the user with nothing.
    parsed = ap.parse_actions(block('{"tool": "create_file", "path": "a.py"}'))

    assert parsed[0].args["content"] == ""


def test_supplied_content_is_never_replaced():
    parsed = ap.parse_actions(
        block('{"tool": "edit_file", "path": "a.py", "content": "print(1)"}'))

    assert parsed[0].args["content"] == "print(1)"


# ======================================================
# Content the model left out
# ======================================================
def test_the_only_code_block_fills_a_create_with_no_content():
    # Measured twice on mistral-7b: the script written in one fence, and
    # the action proposed in the next without it. Taking that block is
    # the reading a person would give the same answer.
    text = ('Here is the script:\n\n```python\nprint("Hello, World!")\n```\n\n'
            'I propose:\n\n' + block('{"tool": "create_file", "path": "hello_world.py"}'))

    parsed = ap.parse_actions(text)

    assert parsed[0].tool_name == "edit_file"
    assert parsed[0].args["content"] == 'print("Hello, World!")\n'


def test_two_code_blocks_are_a_guess_and_are_not_used():
    # A guess about file contents is written to disk. With more than one
    # candidate the file stays empty -- recoverable, visible in the
    # staged diff, and not a fabrication.
    text = ('```python\nprint(1)\n```\n\n```javascript\nconsole.log(1)\n```\n\n'
            + block('{"tool": "create_file", "path": "a.py"}'))

    assert ap.parse_actions(text)[0].args["content"] == ""


def test_content_the_model_supplied_is_never_replaced():
    text = ('```python\nprint(99)\n```\n\n'
            + block('{"tool": "edit_file", "path": "a.py", "content": "given"}'))

    assert ap.parse_actions(text)[0].args["content"] == "given"


def test_the_action_block_is_not_mistaken_for_source():
    text = block('{"tool": "create_file", "path": "a.py"}')

    # The json fence is the action, not the file's contents.
    assert "tool" not in ap.parse_actions(text)[0].args["content"]


def test_an_empty_create_is_reported(tmp_path, monkeypatch):
    from backend.core import file_tools
    from backend.core.tool_orchestrator import run_answer_actions

    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))

    report = run_answer_actions(
        block('{"tool": "create_file", "path": "a.py"}'), "yes, do it")

    # A model that proposes a file without writing its contents produces
    # a real, committable, useless file. The staged diff shows it, but
    # only to someone who looks.
    assert any("created empty" in note for note in report["notes"])


def test_a_create_with_content_is_not_reported_as_empty(tmp_path, monkeypatch):
    from backend.core import file_tools
    from backend.core.tool_orchestrator import run_answer_actions

    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))

    report = run_answer_actions(
        block('{"tool": "edit_file", "path": "a.py", "content": "print(1)"}'),
        "yes, do it")

    assert not any("created empty" in note for note in report["notes"])


# ======================================================
# One path, one write
#
# Measured on mistral-7b: asked for hello_world.py it proposed the file
# TWICE in one answer -- an empty create, then a write with the script in
# it. Two writes to one path in a single turn cannot both be meant, and
# keeping both makes the outcome depend on ordering while showing the
# user the same file twice in the plan.
# ======================================================
def test_an_empty_create_collapses_onto_the_write_with_contents():
    import json

    text = (block(json.dumps({"tool": "create_file", "path": "hello_world.py"}))
            + "\n"
            + block(json.dumps({"tool": "edit_file", "path": "hello_world.py",
                                "content": "print('hi')\n"})))

    parsed = ap.parse_actions(text)

    assert len(parsed) == 1
    assert parsed[0].args["content"] == "print('hi')\n"


def test_order_does_not_decide_which_survives():
    import json

    # Contents win whichever way round the model wrote them. Taking
    # simply the last would make the answer's word order decide what
    # lands on disk.
    text = (block(json.dumps({"tool": "edit_file", "path": "a.py",
                              "content": "print(1)"}))
            + "\n" + block(json.dumps({"tool": "create_file", "path": "a.py"})))

    parsed = ap.parse_actions(text)

    assert len(parsed) == 1
    assert parsed[0].args["content"] == "print(1)"


def test_writes_to_different_paths_are_both_kept():
    import json

    text = (block(json.dumps({"tool": "edit_file", "path": "a.py", "content": "x"}))
            + "\n" + block(json.dumps({"tool": "edit_file", "path": "b.py", "content": "y"})))

    assert len(ap.parse_actions(text)) == 2


def test_a_delete_and_a_write_of_the_same_path_are_both_kept():
    import json

    # Not duplicates: "remove it and put this there instead" is a real
    # sequence, and fs_plan validates it against the projected tree.
    text = (block(json.dumps({"tool": "delete_file", "path": "a.py"}))
            + "\n" + block(json.dumps({"tool": "edit_file", "path": "a.py", "content": "x"})))

    parsed = ap.parse_actions(text)

    assert [p.tool_name for p in parsed] == ["delete_file", "edit_file"]


def test_two_empty_creates_of_one_path_are_still_one():
    import json

    text = (block(json.dumps({"tool": "create_file", "path": "a.py"}))
            + "\n" + block(json.dumps({"tool": "create_file", "path": "a.py"})))

    assert len(ap.parse_actions(text)) == 1


def test_prose_between_two_action_blocks_is_never_used_as_file_contents():
    import json

    # The bug this guards. The first fence pattern matched ```(?!json)
    # directly, so the CLOSING fence of one action block, the prose after
    # it, and the OPENING fence of the next all satisfied it. That prose
    # was then read as a code block and used as a file's contents --
    # a model's commentary written to disk.
    text = (block(json.dumps({"tool": "delete_file", "path": "old.py"}))
            + "\nAnd then I will add the new one.\n\n"
            + block(json.dumps({"tool": "create_file", "path": "new.py"})))

    parsed = ap.parse_actions(text)
    created = [p for p in parsed if p.tool_name == "edit_file"]

    assert created, "the create action was lost"
    assert created[0].args["content"] == ""


def test_an_unlabelled_json_fence_is_not_file_contents():
    import json

    # A model that writes ``` instead of ```json has still written an
    # action, not a file.
    text = ("```\n" + json.dumps({"tool": "run_tests"}) + "\n```\n\n"
            + block(json.dumps({"tool": "create_file", "path": "a.py"})))

    created = [p for p in ap.parse_actions(text) if p.tool_name == "edit_file"]

    assert created[0].args["content"] == ""


def test_a_real_code_fence_beside_one_action_is_still_used():
    import json

    text = ("Here is the script:\n\n```python\nprint('hi')\n```\n\n"
            + block(json.dumps({"tool": "create_file", "path": "a.py"})))

    created = [p for p in ap.parse_actions(text) if p.tool_name == "edit_file"]

    assert created[0].args["content"] == "print('hi')\n"


# ======================================================
# An action written without a fence
#
# Measured live. Asked to create a file, mistral-7b answered with the
# whole action and no fence at all:
#
#     {"tool": "edit_file", "path": "hello_world.py", "content": "..."}
#
# parse_actions found nothing, no action ran, nothing was staged, and
# there was never anything to commit -- and the next three turns had the
# model claiming it had created the file. The proposal was right there,
# one pair of backticks away from working.
# ======================================================
def test_an_unfenced_action_is_still_an_action():
    import json

    text = json.dumps({"tool": "edit_file", "path": "hello_world.py",
                       "content": 'print("Hello World")\n'})

    parsed = ap.parse_actions(text)

    assert len(parsed) == 1
    assert parsed[0].tool_name == "edit_file"
    assert parsed[0].args["content"] == 'print("Hello World")\n'


def test_an_unfenced_action_can_have_prose_around_it():
    import json

    text = ("I will remove it. "
            + json.dumps({"tool": "delete_file", "path": "a.py"})
            + " Shall I?")

    assert [p.tool_name for p in ap.parse_actions(text)] == ["delete_file"]


@pytest.mark.parametrize("text", [
    "Use a dict like {key: value} in python.",
    "The set is {1, 2, 3}.",
    '{"path": "a.py", "content": "x"}',
    '{"result": "ok"}',
    "{}",
])
def test_prose_and_tool_less_json_are_not_actions(text):
    # Requiring a "tool" key is what keeps this narrow. Without it, any
    # answer discussing JSON would start executing.
    assert ap.parse_actions(text) == []


def test_braces_inside_content_do_not_end_the_object():
    import json

    # Scanned with brace counting rather than a regex, because content is
    # a JSON string that can hold braces -- and code very often does.
    text = json.dumps({"tool": "edit_file", "path": "a.py",
                       "content": 'if x: {"a": 1}\nprint("}")\n'})

    parsed = ap.parse_actions(text)

    assert len(parsed) == 1
    assert parsed[0].args["content"] == 'if x: {"a": 1}\nprint("}")\n'


def test_a_fenced_answer_is_read_exactly_as_before():
    import json

    # The unfenced scan runs only when there is no fenced block, so a
    # properly fenced action is never counted twice.
    payload = json.dumps({"tool": "run_tests"})
    text = block(payload)

    assert len(ap.parse_actions(text)) == 1


def test_two_unfenced_actions_are_both_read():
    import json

    text = (json.dumps({"tool": "delete_file", "path": "a.py"}) + "\n"
            + json.dumps({"tool": "create_folder", "path": "docs"}))

    assert [p.tool_name for p in ap.parse_actions(text)] == ["delete_file", "create_folder"]


def test_stripping_leaves_the_prose_and_removes_the_action():
    import json

    text = ("I will remove it. "
            + json.dumps({"tool": "delete_file", "path": "a.py"})
            + " Shall I?")

    stripped = ap.strip_action_json(text)

    assert "tool" not in stripped
    assert "I will remove it." in stripped
    assert "Shall I?" in stripped


def test_stripping_leaves_ordinary_prose_alone():
    for text in ("Use {a: b}.", "", "Just talking."):
        assert ap.strip_action_json(text) == text


# ======================================================
# JSON a model actually writes
# ======================================================
def test_a_literal_newline_inside_a_string_is_repaired():
    # The single most common way a model breaks JSON, and the one that
    # matters most, because the value it breaks is a file's contents.
    # Measured live twice: the action was correct, complete, and refused
    # by json.loads with "Invalid control character", so nothing was
    # staged and ARIA went on to explain how to create files by hand.
    live = ('```json\n{"tool": "edit_file", "path": "open_world.py", '
            '"content": "def open_world:\n    print(1)"}\n```')

    parsed = ap.parse_actions(live)

    assert len(parsed) == 1
    assert parsed[0].args["content"] == "def open_world:\n    print(1)"


def test_the_same_repair_works_without_a_fence():
    live = ('{"tool": "edit_file", "path": "a.py", "content": "line one\nline two"}')

    parsed = ap.parse_actions(live)

    assert len(parsed) == 1
    assert parsed[0].args["content"] == "line one\nline two"


def test_a_tab_inside_a_string_is_repaired():
    parsed = ap.parse_actions(
        '{"tool": "edit_file", "path": "a.py", "content": "if x:\n\treturn 1"}')

    assert parsed[0].args["content"] == "if x:\n\treturn 1"


def test_escaped_newlines_are_left_exactly_alone():
    import json

    # Valid JSON must not be touched by the repair.
    payload = json.dumps({"tool": "edit_file", "path": "a.py",
                          "content": "line one\nline two"})

    assert ap.parse_actions(payload)[0].args["content"] == "line one\nline two"


def test_text_that_is_not_json_is_still_not_json():
    assert ap.loads_lenient("this is prose") is None
    assert ap.loads_lenient("") is None
    assert ap.loads_lenient('{"unclosed": ') is None


# ======================================================
# The user's own words as the first consent
# ======================================================
@pytest.mark.parametrize("text", [
    "Aria create a open_world.py file for me",
    "create the actual file",
    "create the file",
    "make the file",
    "delete notes.md",
    "edit main.py and add logging",
])
def test_an_imperative_file_request_is_consent_to_stage(text):
    # "Create the actual file" is a confirmation by any reading, and it
    # matched nothing: the user asked for a file, ARIA proposed one, the
    # user said create it, and nothing was staged. Then ARIA explained
    # how to make files with a file manager.
    #
    # What this grants is STAGING. Commit is still separate and explicit.
    assert ap.requests_live_execution(text) is True


@pytest.mark.parametrize("text", [
    "what would that do?",
    "what files are in src/",
    "how do I create a file in python",
    "hello there",
    "no",
    "not yet",
    "don't create the file",
    "do not apply the changes",
])
def test_a_question_or_a_refusal_grants_nothing(text):
    assert ap.requests_live_execution(text) is False


def test_the_two_classifiers_do_not_call_each_other():
    import ast
    import inspect

    from backend.chat import model_router

    # requests_live_execution asks classify_turn whether the message is
    # an imperative file request. classify_turn used to ask
    # requests_live_execution the same question from the other side, and
    # each re-entered the other -- the suite went from 96 seconds to a
    # hang.
    tree = ast.parse(inspect.getsource(model_router))
    called = {
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        for node in ast.walk(tree) if isinstance(node, ast.Call)
    }

    assert "requests_live_execution" not in called
