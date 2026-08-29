# backend/tests/test_ghost_workspace.py
#
# A staging area, so an edit is a proposal until it isn't.
#
# Actions went live in a28c278, and with no ARIA_TOOL_WORKSPACE set the
# workspace root is the project directory itself -- so "apply the
# changes" wrote straight into the source tree. Right confinement, wrong
# default: an edit the user has seen only as a sentence should not
# already be on disk where they keep their work.
#
# Two consents now, not one. Running an action stages it; committing
# writes it into the project. They are different sentences and each has
# its own check.

from __future__ import annotations

import pytest

from backend.core import file_tools
from backend.core import ghost_workspace as ghost

ORIGINAL = "one\ntwo\n"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))
    (tmp_path / "notes.txt").write_text(ORIGINAL, encoding="utf-8")
    (tmp_path / "backend").mkdir()
    return tmp_path


def stage(relative, content):
    """Write `content` to the ghost copy of `relative`."""
    target = ghost.stage_path(relative)
    file_tools.edit_file(target, content, confirm=True)
    return target


# ------------------------------------------------------
# Where a staged edit goes
# ------------------------------------------------------
def test_a_staged_edit_lands_in_the_ghost_and_not_the_project(project):
    stage("notes.txt", "one\nchanged\n")

    assert (project / "notes.txt").read_text(encoding="utf-8") == ORIGINAL
    assert ghost.staged_files() == ["notes.txt"]


def test_the_staging_root_is_named_for_the_project(project):
    assert ghost.staging_root() == project / ghost.STAGING_DIRNAME / project.name


def test_a_new_file_can_be_staged(project):
    # "Validate the target exists in the project" and "Create
    # backend/api.py" cannot both hold. What is validated is the parent.
    stage("backend/api.py", "print('hi')\n")

    assert ghost.staged_files() == ["backend/api.py"]
    assert not (project / "backend" / "api.py").exists()


def test_a_directory_the_project_does_not_have_is_refused(project):
    # A new file in a real directory is an edit. A new file in an
    # invented tree is a hallucinated path wearing an edit's clothes.
    with pytest.raises(ghost.GhostError):
        ghost.stage_path("made/up/tree/api.py")


def test_a_path_outside_the_project_is_refused(project):
    with pytest.raises(Exception):
        ghost.stage_path("../escape.txt")


def test_staging_cannot_stage_itself(project):
    already = ghost.stage_path("notes.txt")

    # A staging area that could stage itself is a loop waiting to be
    # found.
    with pytest.raises(ghost.GhostError):
        ghost.stage_path(already)


# ------------------------------------------------------
# Diffs
# ------------------------------------------------------
def test_a_diff_compares_the_ghost_against_the_project(project):
    stage("notes.txt", "one\nchanged\n")

    diff = ghost.diff_for("notes.txt")

    assert "-two" in diff
    assert "+changed" in diff
    assert "project/notes.txt" in diff and "staged/notes.txt" in diff


def test_a_new_file_diffs_against_nothing(project):
    stage("backend/api.py", "print('hi')\n")

    diff = ghost.diff_for("backend/api.py")

    assert "+print('hi')" in diff


def test_a_diff_is_deterministic(project):
    stage("notes.txt", "one\nchanged\n")

    # No timestamps in the header: the same inputs give the same text,
    # which is what makes a diff safe to show and safe to compare.
    assert ghost.diff_for("notes.txt") == ghost.diff_for("notes.txt")


def test_diff_all_covers_every_staged_file(project):
    stage("notes.txt", "one\nchanged\n")
    stage("backend/api.py", "x = 1\n")

    assert sorted(ghost.diff_all()) == ["backend/api.py", "notes.txt"]


# ------------------------------------------------------
# Commit
# ------------------------------------------------------
def test_a_commit_needs_the_user_to_ask(project):
    stage("notes.txt", "one\nchanged\n")

    report = ghost.commit("what would that look like?")

    assert report["status"] == "refused"
    assert (project / "notes.txt").read_text(encoding="utf-8") == ORIGINAL


def test_a_commit_the_user_asked_for_writes_the_project(project):
    stage("notes.txt", "one\nchanged\n")

    report = ghost.commit("commit the changes")

    assert report["status"] == "committed"
    assert report["files"] == ["notes.txt"]
    assert (project / "notes.txt").read_text(encoding="utf-8") == "one\nchanged\n"


def test_a_commit_creates_a_new_file(project):
    stage("backend/api.py", "print('hi')\n")

    ghost.commit("apply the changes")

    assert (project / "backend" / "api.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_a_negation_vetoes_a_commit(project):
    stage("notes.txt", "one\nchanged\n")

    # The same table action_plan uses, imported rather than copied:
    # "don't apply the changes yet" has to veto a commit for exactly the
    # reason it vetoes a live run.
    report = ghost.commit("don't apply the changes yet")

    assert report["status"] == "refused"
    assert (project / "notes.txt").read_text(encoding="utf-8") == ORIGINAL


def test_committing_nothing_is_not_a_failure(project):
    assert ghost.commit("commit the changes")["status"] == "empty"


def test_a_commit_reports_the_diffs_it_applied(project):
    stage("notes.txt", "one\nchanged\n")

    report = ghost.commit("commit the changes")

    assert "notes.txt" in report["diffs"]


# ------------------------------------------------------
# Discard
# ------------------------------------------------------
def test_a_discard_needs_the_user_to_ask(project):
    stage("notes.txt", "one\nchanged\n")

    assert ghost.discard("show me the diff")["status"] == "refused"
    assert ghost.staged_files() == ["notes.txt"]


def test_a_discard_throws_the_staging_away(project):
    stage("notes.txt", "one\nchanged\n")

    report = ghost.discard("discard the changes")

    assert report["status"] == "discarded"
    assert report["files"] == ["notes.txt"]
    assert ghost.staged_files() == []
    # And leaves the project exactly as it was.
    assert (project / "notes.txt").read_text(encoding="utf-8") == ORIGINAL


# ------------------------------------------------------
# Consent shares one negation table
# ------------------------------------------------------
def test_the_negation_table_is_action_plans_own():
    from backend.core.action_plan import NEGATION_VETO

    # Imported, not copied. Two tables would be two things to keep in
    # step, and the one that drifts is the one nobody is looking at.
    assert ghost.NEGATION_VETO is NEGATION_VETO


@pytest.mark.parametrize("said,commits", [
    ("commit the changes", True),
    ("apply the changes", True),
    ("commit it", True),
    # A question about committing is not a request to commit. It matches
    # no phrase in the table -- "commit" alone is not one -- so it needs
    # no negation to be refused.
    ("what would you commit?", False),
    ("don't commit it yet", False),
    ("never apply it", False),
    ("show me the diff", False),
    ("", False),
])
def test_commit_consent(said, commits):
    assert ghost.requests_commit(said) is commits


def test_the_model_cannot_commit_by_writing_the_words():
    from backend.core.action_plan import ACTION_TOOLS

    # Commit is not a tool. If it were on ACTION_TOOLS a model could emit
    # one in its answer, and "only the user authorises a commit" would be
    # enforced by nothing but the model's manners.
    assert "commit_changes" not in ACTION_TOOLS
    assert "discard_staged_changes" not in ACTION_TOOLS
    assert ACTION_TOOLS == frozenset({"edit_file", "run_tests"})
