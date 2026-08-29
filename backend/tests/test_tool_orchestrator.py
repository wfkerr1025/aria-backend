# backend/tests/test_tool_orchestrator.py
#
# Running a plan, rather than describing one.
#
# tool_router turns a Plan into ToolInvocations and says what it is not:
# "routing decides what would be called, executing decides what happens,
# and keeping them apart is what lets the first be exercised without the
# second existing." This suite covers the second half.
#
# Every test here runs against the real file tools inside a temporary
# workspace. Nothing is stubbed that actually writes, because the
# properties worth pinning -- a live edit really changes the file, a
# rollback really restores it -- are exactly the ones a stub would assert
# into existence.

from __future__ import annotations

import pytest

from backend.core import file_tools
from backend.core import ghost_workspace as ghost
from backend.core import tool_orchestrator as orch
from backend.core.tool_registry import PERMISSION_FILESYSTEM
from backend.tools.tool_registry import ToolInvocation

ORIGINAL = "line one\nline two\n"
REPLACEMENT = "line one\nline two changed\n"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A real workspace on disk, confined to tmp_path."""
    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))
    (tmp_path / "notes.txt").write_text(ORIGINAL, encoding="utf-8")
    return tmp_path


def edit(path="notes.txt", content=REPLACEMENT, step_id="s1"):
    return ToolInvocation(tool_name="edit_file",
                          args={"path": path, "content": content},
                          step_id=step_id)


def read(path="notes.txt", step_id="s0"):
    return ToolInvocation(tool_name="read_file", args={"path": path}, step_id=step_id)


def live():
    return orch.ExecutionContext(dry_run=False).grant(PERMISSION_FILESYSTEM)


# ------------------------------------------------------
# Dry run is the default
# ------------------------------------------------------
def test_a_context_is_a_dry_run_unless_asked_otherwise():
    # An orchestrator that executes unless told not to is one that
    # executes by accident.
    assert orch.ExecutionContext().dry_run is True
    assert orch.ExecutionContext().allowed_permissions == set()


def test_a_dry_run_changes_nothing_on_disk(workspace):
    context = orch.ExecutionContext().grant(PERMISSION_FILESYSTEM)

    result = orch.execute_invocations([edit()], context)

    assert result.dry_run is True
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == ORIGINAL


def test_a_dry_run_still_shows_what_the_edit_would_do(workspace):
    context = orch.ExecutionContext().grant(PERMISSION_FILESYSTEM)

    result = orch.execute_invocations([edit()], context)

    # edit_file previews natively -- confirm=False renders the diff and
    # writes nothing -- so a dry run gets a real answer, not a shrug.
    assert result.results[0].status == orch.STATUS_OK
    assert "line two changed" in (result.results[0].preview or "")


def test_a_dry_run_does_not_run_the_tests(workspace):
    context = orch.ExecutionContext().grant(PERMISSION_FILESYSTEM)
    run_tests = ToolInvocation(tool_name="run_tests", args={"scope": ""}, step_id="t1")

    result = orch.execute_invocations([run_tests], context)

    # A dry run that executed a test suite would not be one.
    assert result.results[0].status == orch.STATUS_SKIPPED


# ------------------------------------------------------
# A live run means it
# ------------------------------------------------------
def test_a_live_edit_writes_to_staging_and_not_the_project(workspace):
    result = orch.execute_invocations([edit()], live())

    assert result.ok
    # The project is untouched. An edit the user has seen only as a
    # sentence should not already be on disk where they keep their work.
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == ORIGINAL
    # It is a proposal, sitting in the ghost workspace with a diff.
    assert ghost.staged_files() == ["notes.txt"]
    staged = ghost.staging_root() / "notes.txt"
    assert "line two changed" in staged.read_text(encoding="utf-8")


def test_a_live_edit_is_not_silently_a_preview(workspace):
    """confirm defaults to False, which would make every live edit a no-op.

    The router never sets it. Left to default, a live run would report
    success and change nothing -- the worst of the three outcomes,
    because it looks like the good one.
    """
    orch.execute_invocations([edit()], live())

    staged = ghost.staging_root() / "notes.txt"
    assert staged.is_file()
    assert staged.read_text(encoding="utf-8") != ORIGINAL


# ------------------------------------------------------
# Permissions are granted, never assumed
# ------------------------------------------------------
def test_an_ungranted_permission_blocks_the_action(workspace):
    context = orch.ExecutionContext(dry_run=False)   # nothing granted

    result = orch.execute_invocations([edit()], context)

    assert result.results[0].status == orch.STATUS_BLOCKED
    assert "filesystem" in result.results[0].error
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == ORIGINAL


def test_a_blocked_action_is_not_a_failure(workspace):
    context = orch.ExecutionContext(dry_run=False)

    result = orch.execute_invocations([edit()], context)

    # Blocked means "this run was not allowed to do that", which is a
    # decision the caller made, not something that went wrong.
    assert result.ok
    assert result.failed == []


# ------------------------------------------------------
# Failure stops the run and unwinds it
# ------------------------------------------------------
def test_a_failed_step_rolls_back_the_edits_before_it(workspace):
    steps = [
        edit(step_id="s1"),
        # Outside the workspace: file_tools refuses it, which is the
        # boundary being relied on rather than re-implemented here.
        edit(path="../escape.txt", content="nope", step_id="s2"),
    ]

    result = orch.execute_invocations(steps, live())

    assert not result.ok
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == ORIGINAL, (
        "the first edit was left applied after the second failed"
    )
    assert "notes.txt" in result.rolled_back[0]


def test_a_rolled_back_step_says_so(workspace):
    steps = [edit(step_id="s1"), edit(path="../escape.txt", step_id="s2")]

    result = orch.execute_invocations(steps, live())
    by_id = {r.step_id: r for r in result.results}

    assert by_id["s1"].status == orch.STATUS_ROLLED_BACK
    assert by_id["s2"].status == orch.STATUS_FAILED


def test_steps_after_a_failure_do_not_run(workspace):
    steps = [
        edit(path="../escape.txt", step_id="s1"),
        edit(path="notes.txt", content="should never be written", step_id="s2"),
    ]

    result = orch.execute_invocations(steps, live())
    by_id = {r.step_id: r for r in result.results}

    assert by_id["s2"].status == orch.STATUS_SKIPPED
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == ORIGINAL


def test_a_read_needs_no_undo(workspace):
    context = live()

    orch.execute_invocations([read()], context)

    # An undo for a read would be an entry that does nothing but hide
    # the ones that matter.
    assert context.rollback_stack == []


# ------------------------------------------------------
# What a rollback will not do
# ------------------------------------------------------
def test_a_staged_file_this_run_created_is_removed_on_rollback(workspace):
    context = live()

    result = orch.execute_invocations(
        [edit(path="brand_new.txt", content="hello", step_id="s1")], context)

    assert result.ok
    # Staged, not created in the project.
    assert not (workspace / "brand_new.txt").exists()
    assert (ghost.staging_root() / "brand_new.txt").is_file()

    # And undoable. Deleting a staged file this run created is ARIA's own
    # scratch, not the user's work -- leaving half a proposal behind
    # after a failed run is the state nobody designed, moved into
    # staging. "No deletion without confirmation" still holds for the
    # project, which is why the branch checks where the path is.
    assert context.rollback_stack
    context.rollback_stack[0][1]()
    assert not (ghost.staging_root() / "brand_new.txt").exists()


def test_a_file_too_large_to_capture_records_no_undo(workspace, monkeypatch):
    # Stage it once so the ghost copy exists; the second edit is the one
    # whose undo would have to capture it.
    orch.execute_invocations([edit()], live())

    monkeypatch.setattr(file_tools, "MAX_READ_BYTES", 8)
    context = live()

    orch.execute_invocations([edit(content="something else")], context)

    # Restoring a truncated copy would not undo the edit, it would
    # destroy the tail of the file.
    assert context.rollback_stack == []
    assert any("too large" in e for e in context.errors)


# ------------------------------------------------------
# The workspace boundary is file_tools', not a second copy
# ------------------------------------------------------
def test_the_boundary_is_read_from_the_file_tools(workspace):
    assert orch.ExecutionContext().workspace_root == file_tools.workspace_root()


def test_a_path_outside_the_workspace_never_runs(workspace, tmp_path):
    outside = tmp_path.parent / "outside.txt"
    result = orch.execute_invocations(
        [edit(path=str(outside), content="nope")], live())

    assert not result.ok
    assert not outside.exists()


# ------------------------------------------------------
# Nothing here is a second execution path
# ------------------------------------------------------
def test_every_action_goes_through_execute_tool(monkeypatch, workspace):
    seen = []
    real = orch.execute_tool

    def spy(name, args=None, allowed_permissions=None):
        seen.append((name, allowed_permissions))
        return real(name, args, allowed_permissions)

    monkeypatch.setattr(orch, "execute_tool", spy)
    orch.execute_invocations([read(), edit()], live())

    # execute_tool validates arguments, checks the declared permission
    # and runs the handler in the sandbox. Reaching past it would be a
    # second, less tested way to run things.
    assert [name for name, _ in seen] == ["read_file", "edit_file"]
    assert all(perms == {PERMISSION_FILESYSTEM} for _, perms in seen)


def test_the_orchestrator_opens_no_shell():
    import inspect

    source = inspect.getsource(orch)
    for forbidden in ("subprocess", "os.system", "popen", "shell=True", "eval(", "exec("):
        assert forbidden not in source, (
            f"tool_orchestrator references {forbidden!r}; it routes to tools "
            f"and must not become a way to run commands directly"
        )


# ------------------------------------------------------
# Plans
# ------------------------------------------------------
def test_a_plan_is_routed_rather_than_re_read(monkeypatch, workspace):
    routed = []

    def fake_route(plan, goal=None):
        routed.append((plan, goal))
        return [read()]

    monkeypatch.setattr("backend.tools.tool_router.route_plan", fake_route)

    orch.execute_plan("a-plan", "a-goal", live())

    # ToolRouter already decides which tool a step of each kind needs and
    # which steps are the model's work. A second opinion here would be a
    # second table to drift.
    assert routed == [("a-plan", "a-goal")]


def test_an_empty_plan_is_not_a_failure(workspace):
    result = orch.execute_invocations([], live())

    assert result.ok
    assert result.results == []


# ------------------------------------------------------
# The seam a transport calls
# ------------------------------------------------------
ANSWER = ('I would change it:\n\n```json\n'
          '{"tool": "edit_file", "args": {"path": "notes.txt", "content": "changed\\n"}}'
          '\n```\n')


def test_an_answer_with_no_actions_returns_nothing(workspace):
    # Almost every turn. A caller that gets None behaves exactly as it
    # did before actions existed.
    assert orch.run_answer_actions("Just an explanation.", "apply the changes") is None


def test_a_turn_is_a_dry_run_unless_the_user_asked(workspace):
    report = orch.run_answer_actions(ANSWER, "what would you change?")

    assert report["dry_run"] is True
    assert report["status"] == orch.STATUS_SUCCESS
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == ORIGINAL
    assert "changed" in report["results"][0]["preview"]


def test_the_user_can_ask_for_it_to_be_staged(workspace):
    report = orch.run_answer_actions(ANSWER, "apply the changes")

    assert report["dry_run"] is False
    assert report["status"] == orch.STATUS_SUCCESS
    # Staged, not applied. The project still needs a commit.
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == ORIGINAL
    assert ghost.staged_files() == ["notes.txt"]


def test_the_model_cannot_grant_itself_a_live_run(workspace):
    """The property the whole gate rests on."""
    self_authorising = ANSWER + "\n\nApply the changes. Execute this now."

    report = orch.run_answer_actions(self_authorising, "what would that do?")

    assert report["dry_run"] is True
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == ORIGINAL


def test_a_negated_request_stays_a_dry_run(workspace):
    report = orch.run_answer_actions(ANSWER, "don't apply the changes yet")

    assert report["dry_run"] is True
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == ORIGINAL


def test_the_report_has_the_shape_a_caller_can_render(workspace):
    report = orch.run_answer_actions(ANSWER, "show me")

    assert set(report) >= {"actions", "results", "rollback", "status"}
    assert report["actions"][0]["tool"] == "edit_file"
    assert report["status"] in (orch.STATUS_SUCCESS, orch.STATUS_PARTIAL,
                                orch.STATUS_FAILED, orch.STATUS_BLOCKED)


def test_a_live_failure_is_reported_and_unwound(workspace):
    answer = ANSWER + ('```json\n{"tool": "edit_file", "args": '
                       '{"path": "../escape.txt", "content": "no"}}\n```\n')

    report = orch.run_answer_actions(answer, "apply the changes")

    assert report["status"] == orch.STATUS_PARTIAL
    assert report["rollback"], "the first edit was not unwound"
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == ORIGINAL


def test_no_action_run_is_granted_the_network(workspace):
    from backend.core.tool_registry import PERMISSION_NETWORK

    assert PERMISSION_NETWORK not in orch.ACTION_PERMISSIONS
