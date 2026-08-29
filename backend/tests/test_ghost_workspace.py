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
    # newline="" so the fixture writes exactly these bytes. write_text's
    # default translates "\n" to "\r\n" on Windows, which edit_file no
    # longer does -- so a fixture left on the default seeds a file whose
    # line endings differ from anything staged over it, and every
    # comparison between the two reads as a change nobody made.
    (tmp_path / "notes.txt").write_text(ORIGINAL, encoding="utf-8", newline="")
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


# ------------------------------------------------------
# Staged reads
# ------------------------------------------------------
def test_a_read_sees_the_staged_version(project):
    stage("notes.txt", "one\nchanged\n")

    # A model that edits a file and then reads it back should see its own
    # edit. Reading the project's copy is how it concludes the change did
    # not happen and does it again.
    assert file_tools.read_file("notes.txt")["text"] == "one\nchanged\n"


def test_an_unstaged_file_reads_from_the_project(project):
    assert file_tools.read_file("notes.txt")["text"] == ORIGINAL


def test_a_read_reports_the_project_path_not_the_staged_one(project):
    stage("notes.txt", "one\nchanged\n")

    # What the reader wants to know is which file this is, not which copy
    # of it happened to be on disk.
    assert file_tools.read_file("notes.txt")["path"] == "notes.txt"


def test_get_effective_file_prefers_staging(project):
    assert ghost.get_effective_file("notes.txt") == project / "notes.txt"

    stage("notes.txt", "one\nchanged\n")

    assert ghost.get_effective_file("notes.txt") == ghost.staging_root() / "notes.txt"


def test_a_path_outside_the_project_is_returned_unchanged(project):
    # Confinement is file_tools' job and it has already ruled. This layer
    # does not get a second opinion on where a file may be.
    outside = str(project.parent / "elsewhere.txt")

    assert str(ghost.get_effective_file(outside)) == outside


# ------------------------------------------------------
# The merged directory view
# ------------------------------------------------------
def test_a_listing_merges_staged_and_real(project):
    stage("backend/api.py", "x = 1\n")

    listing = ghost.list_effective_directory("backend")

    assert listing == ["api.py"]


def test_a_file_in_both_places_appears_once(project):
    stage("notes.txt", "one\nchanged\n")

    listing = ghost.list_effective_directory("")

    assert listing.count("notes.txt") == 1


def test_the_staging_directory_is_not_project_content(project):
    stage("notes.txt", "one\nchanged\n")

    assert ghost.STAGING_DIRNAME not in ghost.list_effective_directory("")


# ------------------------------------------------------
# Snapshots
# ------------------------------------------------------
def test_the_first_staging_of_a_file_has_nothing_to_snapshot(project):
    stage("notes.txt", "first\n")

    assert ghost.rollback_staged_changes()["status"] == "no_snapshot"


def test_restaging_snapshots_the_previous_version(project):
    stage("notes.txt", "first\n")
    stage("notes.txt", "second\n")

    report = ghost.rollback_staged_changes()

    assert report["status"] == "rolled_back"
    assert (ghost.staging_root() / "notes.txt").read_text(encoding="utf-8") == "first\n"


def test_a_rollback_never_touches_the_project(project):
    stage("notes.txt", "first\n")
    stage("notes.txt", "second\n")

    ghost.rollback_staged_changes()

    assert (project / "notes.txt").read_text(encoding="utf-8") == ORIGINAL


def test_snapshots_are_not_staged_files(project):
    stage("notes.txt", "first\n")
    stage("notes.txt", "second\n")

    # Listing them would put .snapshots/index.json in the pending list,
    # show it a diff, and commit ARIA's own bookkeeping into the project.
    assert ghost.staged_files() == ["notes.txt"]
    for entry in ghost.get_pending_changes():
        assert ghost.SNAPSHOT_DIRNAME not in entry["path"]


def test_a_commit_never_writes_the_snapshot_directory(project):
    stage("notes.txt", "first\n")
    stage("notes.txt", "second\n")

    ghost.commit_changes("commit the changes")

    assert not (project / ghost.SNAPSHOT_DIRNAME).exists()


# ------------------------------------------------------
# Pending changes
# ------------------------------------------------------
def test_pending_changes_describe_each_staged_file(project):
    stage("notes.txt", "one\nchanged\n")
    stage("backend/api.py", "x = 1\n")

    by_path = {entry["path"]: entry for entry in ghost.get_pending_changes()}

    assert by_path["notes.txt"]["status"] == ghost.STATUS_STAGED_MODIFIED
    assert by_path["backend/api.py"]["status"] == ghost.STATUS_STAGED_NEW
    assert "-two" in by_path["notes.txt"]["diff"]


def test_a_staged_file_identical_to_the_project_says_so(project):
    stage("notes.txt", ORIGINAL)

    # Worth reporting rather than hiding: a proposal that turned out to
    # be a no-op is something a reviewer should see.
    assert ghost.get_pending_changes()[0]["status"] == ghost.STATUS_STAGED_UNCHANGED


# ------------------------------------------------------
# Selective commit and discard
# ------------------------------------------------------
def test_a_selective_commit_writes_only_what_was_named(project):
    stage("notes.txt", "one\nchanged\n")
    stage("backend/api.py", "x = 1\n")

    report = ghost.commit_changes("commit the changes", files=["notes.txt"])

    assert report["files"] == ["notes.txt"]
    assert (project / "notes.txt").read_text(encoding="utf-8") == "one\nchanged\n"
    assert not (project / "backend" / "api.py").exists()
    # And the one not committed is still staged.
    assert "backend/api.py" in ghost.staged_files()


def test_naming_a_file_that_is_not_staged_is_reported(project):
    stage("notes.txt", "one\nchanged\n")

    report = ghost.commit_changes("commit the changes", files=["nope.txt"])

    # Rather than a successful-looking report about nothing.
    assert report["unknown"] == ["nope.txt"]
    assert report["files"] == []


def test_a_selective_discard_keeps_the_rest(project):
    stage("notes.txt", "one\nchanged\n")
    stage("backend/api.py", "x = 1\n")

    report = ghost.discard_changes("discard the changes", files=["notes.txt"])

    assert report["files"] == ["notes.txt"]
    assert ghost.staged_files() == ["backend/api.py"]


def test_a_selective_discard_never_touches_the_project(project):
    stage("notes.txt", "one\nchanged\n")

    ghost.discard_changes("discard the changes", files=["notes.txt"])

    assert (project / "notes.txt").read_text(encoding="utf-8") == ORIGINAL


def test_the_new_commands_take_the_same_consent(project):
    stage("notes.txt", "one\nchanged\n")

    assert ghost.commit_changes("don't apply the changes yet")["status"] == "refused"
    assert ghost.discard_changes("not yet")["status"] == "refused"
    assert ghost.staged_files() == ["notes.txt"]
