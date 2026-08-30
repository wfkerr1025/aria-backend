# backend/tests/test_file_tools_staging_and_consent.py
#
# The five shape-changing operations, and the two consents between a
# model proposing one and it happening.
#
#     proposed   the model wrote an action        nothing recorded
#     staged     the user said "yes, do it"       journal entry, no change
#     applied    the user committed the workspace the operation runs
#
# Most of this file is about the gap between staged and applied, because
# that is where a staging system earns its keep. A delete that leaks
# through it is not a bug that can be apologised for afterwards.
#
# The strongest assertion here is structural rather than behavioural:
# test_the_handlers_contain_no_filesystem_mutation. A confirm flag can be
# passed by accident; a handler with no unlink in it cannot delete,
# whatever anyone calls it with.

from __future__ import annotations

import pytest

from backend.core import fs_plan, ghost_workspace
from backend.core.fs_plan import PlanError
from backend.core.tool_orchestrator import run_answer_actions


@pytest.fixture
def project(tmp_path, monkeypatch):
    from backend.core import file_tools

    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))
    (tmp_path / "notes.md").write_text("hello\n", encoding="utf-8", newline="")
    (tmp_path / "main.py").write_text("print(1)\n", encoding="utf-8", newline="")
    (tmp_path / "src").mkdir()
    return tmp_path


def block(payload: str) -> str:
    return f"```json\n{payload}\n```"


DELETE = block('{"tool": "delete_file", "path": "notes.md"}')
MOVE = block('{"tool": "move_file", "path": "main.py", "dest": "src/main.py"}')
RENAME = block('{"tool": "rename_file", "path": "notes.md", "new_name": "readme.md"}')
COPY = block('{"tool": "copy_file", "path": "main.py", "dest": "src/copy.py"}')
FOLDER = block('{"tool": "create_folder", "path": "docs"}')

ALL_FIVE = [
    ("delete_file", DELETE), ("move_file", MOVE), ("rename_file", RENAME),
    ("copy_file", COPY), ("create_folder", FOLDER),
]


# ======================================================
# The tools cannot mutate. Structurally.
# ======================================================
def test_the_handlers_contain_no_filesystem_mutation():
    import inspect

    from backend.core import tool_registry

    source = inspect.getsource(tool_registry._staging_handler)

    # The whole safety argument in one assertion. There is no code in the
    # delete handler that deletes, so no argument, flag or permission can
    # make it delete during a turn.
    for forbidden in ("unlink", "rmtree", "remove(", "shutil.move",
                      "copy2", "mkdir", "write_text", "write_bytes"):
        assert forbidden not in source, (
            f"the staging handler calls {forbidden}; it must only record an intent")


def test_only_commit_applies_a_plan():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(fs_plan))
    callers = [
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(getattr(c.func, "attr", getattr(c.func, "id", "")) in
                ("unlink", "rmtree", "move", "copy2", "copytree", "mkdir")
                for c in ast.walk(node) if isinstance(c, ast.Call))
    ]

    # _save and _check touch the journal and the staging directory;
    # apply_operations is the only one that may touch the project.
    assert "apply_operations" in callers
    assert "stage_operation" not in callers
    assert "preview_operation" not in callers


# ======================================================
# Consent one: the chat
# ======================================================
@pytest.mark.parametrize("tool,answer", ALL_FIVE)
def test_without_chat_consent_nothing_is_staged(project, tool, answer):
    report = run_answer_actions(answer, "what would that do?")

    assert report is not None
    assert fs_plan.pending_operations() == []
    # And it still says what it would do -- a dry run whose answer is
    # "nothing ran" tells the user nothing about the decision they are
    # being asked to make.
    assert report["results"][0]["preview"].startswith("Would ")


@pytest.mark.parametrize("tool,answer", ALL_FIVE)
def test_with_chat_consent_it_is_staged_and_nothing_changes(project, tool, answer):
    # ARIA's own staging directory is excluded: it lives inside the
    # project (which is what makes workspaces isolated) and its
    # appearance is not a change to the user's work.
    def user_files():
        return sorted(p.name for p in project.iterdir()
                      if p.name != ghost_workspace.STAGING_DIRNAME)

    before = user_files()

    report = run_answer_actions(answer, "yes, do it")

    assert report["results"][0]["status"] == "ok"
    assert len(fs_plan.pending_operations()) == 1
    # The project is untouched. This is the whole point of the middle
    # state: the user has agreed in words and can still change their mind.
    assert user_files() == before


@pytest.mark.parametrize("phrase", [
    "yes, do it", "yes", "go ahead", "apply the changes", "proceed", "ok",
])
def test_the_phrases_a_user_actually_types_grant_consent(project, phrase):
    # "Yes, do it" is the answer to "Proceed?" and matched nothing until
    # this was probed. A consent step that does not recognise its own
    # standard answer is one that quietly never fires.
    run_answer_actions(DELETE, phrase)

    assert len(fs_plan.pending_operations()) == 1


@pytest.mark.parametrize("phrase", [
    "no", "don't do it", "not yet", "yes, but not the delete",
    "do not apply the changes", "what would that do?",
])
def test_a_refusal_or_a_question_grants_nothing(project, phrase):
    run_answer_actions(DELETE, phrase)

    assert fs_plan.pending_operations() == []


def test_the_model_cannot_consent_on_the_users_behalf(project):
    # The consent is read from the USER's message. A model writing "yes,
    # apply the changes" in its own answer has described an intention.
    answer = "Yes, apply the changes. Doing it now.\n\n" + DELETE

    run_answer_actions(answer, "what do you think?")

    assert fs_plan.pending_operations() == []


# ======================================================
# Consent two: the commit
# ======================================================
def test_a_staged_plan_needs_the_second_consent(project):
    run_answer_actions(DELETE, "yes, do it")

    refused = ghost_workspace.commit_changes("maybe later")

    assert refused["status"] == "refused"
    assert (project / "notes.md").exists()


def test_both_consents_together_apply_the_operation(project):
    run_answer_actions(DELETE, "yes, do it")

    committed = ghost_workspace.commit_changes("commit the changes")

    assert committed["status"] == "committed"
    assert committed["operations"] == ["delete notes.md"]
    assert not (project / "notes.md").exists()
    assert fs_plan.pending_operations() == []


def test_every_operation_runs_end_to_end(project):
    # Copy BEFORE move. A plan is a sequence, and copying main.py after
    # moving it is impossible -- which the staging validation now refuses
    # rather than discovering at commit. Written in an order that works,
    # because what this test is for is that all five operations run.
    run_answer_actions(COPY + "\n" + MOVE + "\n" + FOLDER + "\n" + RENAME,
                       "yes, do it")

    ghost_workspace.commit_changes("commit the changes")

    assert (project / "src" / "copy.py").exists()
    assert (project / "src" / "main.py").exists()
    assert not (project / "main.py").exists()
    assert (project / "docs").is_dir()
    assert (project / "readme.md").exists()


def test_a_plan_that_contradicts_itself_is_refused_while_staging(project):
    # Two individually valid operations and one impossible plan: the copy
    # reads main.py, which the move has already taken away.
    #
    # Both used to stage cleanly and the contradiction surfaced at commit
    # -- in front of somebody who approved it several turns earlier and
    # could no longer see why it was wrong. Operations are validated
    # against the tree the plan WILL produce, not the one on disk now.
    fs_plan.stage_operation("move_file", "main.py", "src/main.py")

    with pytest.raises(PlanError) as raised:
        fs_plan.stage_operation("copy_file", "main.py", "src/copy.py")

    assert "earlier staged step" in str(raised.value)


def test_the_same_step_from_the_new_location_is_accepted(project):
    fs_plan.stage_operation("move_file", "main.py", "src/main.py")

    assert fs_plan.stage_operation(
        "copy_file", "src/main.py", "src/copy.py")["staged"] is True


def test_a_path_the_plan_creates_counts_as_existing(project):
    # The other direction: docs/ does not exist on disk while the second
    # step is being staged, and staging it twice must still be refused.
    fs_plan.stage_operation("create_folder", "docs")

    with pytest.raises(PlanError) as raised:
        fs_plan.stage_operation("move_file", "main.py", "docs")

    assert "already exists" in str(raised.value)


def test_rename_puts_the_file_beside_itself(project):
    run_answer_actions(RENAME, "yes, do it")
    ghost_workspace.commit_changes("commit the changes")

    assert (project / "readme.md").exists()
    assert not (project / "notes.md").exists()


def test_discard_cancels_a_staged_plan(project):
    run_answer_actions(DELETE, "yes, do it")

    ghost_workspace.discard_changes("discard the changes")

    assert fs_plan.pending_operations() == []
    assert (project / "notes.md").exists()


def test_a_discard_with_no_staged_content_still_drops_the_plan(project):
    # The early return that used to fire here left a staged deletion
    # waiting for the next commit -- a delete outliving the discard meant
    # to cancel it, which is the worst thing this system could get wrong.
    run_answer_actions(DELETE, "yes, do it")
    assert ghost_workspace.staged_files() == []

    result = ghost_workspace.discard_changes("discard the changes")

    assert result["status"] == "discarded"
    assert fs_plan.pending_operations() == []


def test_content_is_written_before_the_tree_is_reshaped(project):
    # "Rewrite main.py, then move it into src/" is a sequence a user
    # would say out loud. The reverse order writes to a path that has
    # just stopped existing.
    from backend.core import file_tools

    file_tools.edit_file(ghost_workspace.stage_path("main.py"),
                         "print('new')\n", confirm=True)
    run_answer_actions(MOVE, "yes, do it")

    ghost_workspace.commit_changes("commit the changes")

    assert (project / "src" / "main.py").read_text(encoding="utf-8") == "print('new')\n"


# ======================================================
# Refusals are reported, never swallowed
# ======================================================
@pytest.mark.parametrize("bad", [
    "../outside.txt", "/etc/passwd", "..\\..\\evil.txt", "../../../secrets",
])
def test_a_path_outside_the_workspace_is_refused(project, bad):
    with pytest.raises(PlanError):
        fs_plan.stage_operation("delete_file", bad)

    assert fs_plan.pending_operations() == []


def test_a_rename_cannot_become_a_move_out_of_the_project(project):
    # "../../etc/passwd" is a valid string for new_name, and would turn a
    # rename into an escape. Refused by shape, with a sentence that says
    # what to use instead.
    with pytest.raises(PlanError) as raised:
        fs_plan.stage_operation("rename_file", "notes.md", "../../evil.txt")

    assert "not a path" in str(raised.value)


def test_deleting_something_that_is_not_there_is_refused_now(project):
    # Refused at staging, not at commit. A failure reported ten minutes
    # and five accepted proposals later reaches somebody who has
    # forgotten what they asked for.
    with pytest.raises(PlanError) as raised:
        fs_plan.stage_operation("delete_file", "no_such_file.md")

    assert "does not exist" in str(raised.value)


def test_moving_onto_an_existing_file_is_refused(project):
    (project / "src" / "main.py").write_text("existing\n", encoding="utf-8", newline="")

    with pytest.raises(PlanError) as raised:
        fs_plan.stage_operation("move_file", "main.py", "src/main.py")

    assert "already exists" in str(raised.value)


def test_a_refusal_reaches_the_turn_report(project):
    answer = block('{"tool": "delete_file", "path": "../outside.txt"}')

    report = run_answer_actions(answer, "yes, do it")

    assert report["results"][0]["status"] == "failed"
    assert report["results"][0]["error"]
    assert report["status"] != "ok"


def test_an_unknown_operation_is_refused_by_name(project):
    with pytest.raises(PlanError):
        fs_plan.stage_operation("format_disk", "notes.md")


# ======================================================
# What the user is shown before the second consent
# ======================================================
def test_the_plan_reads_as_sentences(project):
    run_answer_actions(DELETE + "\n" + FOLDER, "yes, do it")

    summary = fs_plan.describe_plan()

    assert "delete notes.md" in summary
    assert "create folder docs" in summary


def test_destructive_operations_are_listed_first(project):
    run_answer_actions(FOLDER + "\n" + DELETE, "yes, do it")

    summary = fs_plan.describe_plan()

    # A delete listed under three folder creations is a delete that gets
    # skimmed past.
    assert summary.index("delete") < summary.index("create folder")


def test_the_workspace_reports_its_staged_operations(project):
    from backend.core import workspace_manager as wm

    wm.reset_registry()
    wm.ensure_default_workspace()
    run_answer_actions(DELETE, "yes, do it")

    described = wm.describe_workspace()
    details = wm.get_workspace_details(wm.get_workspace_list()[0]["id"])

    # "0 staged" while a delete is waiting would be true and misleading.
    assert described["staged_operations"] == 1
    assert len(details["pending_operations"]) == 1
    assert details["pending_operations"][0]["destructive"] is True
    wm.reset_registry()


def test_the_same_operation_twice_is_one_entry(project):
    run_answer_actions(DELETE, "yes, do it")
    run_answer_actions(DELETE, "yes, do it")

    assert len(fs_plan.pending_operations()) == 1


def test_a_plan_that_no_longer_matches_the_project_is_not_applied(project):
    run_answer_actions(DELETE, "yes, do it")
    # The user deleted it themselves in the meantime.
    (project / "notes.md").unlink()

    result = ghost_workspace.commit_changes("commit the changes")

    # Re-validated immediately before it runs. Applying a stale plan is
    # how a "safe" staged operation destroys the wrong thing.
    assert result["status"] == "partial"
    assert result["failed"]


def test_arias_bookkeeping_is_never_staged(project):
    # .snapshots was caught doing this once; the operation journal was
    # caught doing it again the day it was added. Both live in the
    # staging root, and staged_files() is what decides whether something
    # gets committed into the user's project.
    run_answer_actions(DELETE, "yes, do it")

    assert ghost_workspace.staged_files() == []
    assert not any(name.startswith(".fs_plan")
                   for name in ghost_workspace.staged_files())


def test_a_users_own_dotfile_is_still_stageable(project):
    # The reason bookkeeping is named rather than excluded by the leading
    # dot: .gitignore, .env and .editorconfig are files a user edits.
    from backend.core import file_tools

    (project / ".gitignore").write_text("build/\n", encoding="utf-8", newline="")
    file_tools.edit_file(ghost_workspace.stage_path(".gitignore"),
                         "build/\ndist/\n", confirm=True)

    assert ghost_workspace.staged_files() == [".gitignore"]


def test_every_bookkeeping_name_really_is_bookkeeping():
    from backend.core import fs_plan as plan

    # If a new scratch file is added to the staging root without being
    # registered here, it becomes a "pending change" and gets committed.
    assert plan.PLAN_FILENAME in ghost_workspace._BOOKKEEPING
    assert ghost_workspace.SNAPSHOT_DIRNAME in ghost_workspace._BOOKKEEPING
