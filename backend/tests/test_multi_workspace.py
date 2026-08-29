# backend/tests/test_multi_workspace.py
#
# More than one project at once, and the rule that makes it safe.
#
# The specification for this arrived twice. The first version said staged
# reads should search every workspace and return the first match:
#
#     for workspace in workspaces:
#         if path in workspace.staged: return staged version
#
# and, in the same document, that ARIA must never merge workspaces. Those
# cannot both hold, and the search version is the dangerous one: reading
# "config.py" in project B would hand back project A's staged copy, and
# nothing about the result would say so. A wrong answer that looks
# exactly like a right one.
#
# So every operation names its workspace, and isolation is structural
# rather than checked: a workspace stages inside itself
# (<root>/.aria_staging/<name>/), so one project's staged files are not
# under another project's root and cannot be reached from it.

from __future__ import annotations

import pytest

from backend.core import file_tools, ghost_workspace
from backend.core import workspace_manager as wm

ALPHA_TEXT = "alpha original\n"
BETA_TEXT = "beta original\n"


@pytest.fixture
def projects(tmp_path, monkeypatch):
    """Two real projects on disk, each with a file of the same name."""
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    for root, text in ((alpha, ALPHA_TEXT), (beta, BETA_TEXT)):
        root.mkdir()
        (root / "config.py").write_text(text, encoding="utf-8", newline="")

    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(alpha))
    wm.reset_registry()
    a = wm.add_workspace(alpha, "alpha")
    b = wm.add_workspace(beta, "beta")
    yield {"alpha": a, "beta": b, "root": tmp_path}
    wm.reset_registry()


def stage_in(workspace, relative, content):
    # root on both calls. Without it the write is confined to whatever
    # the environment points at, and staging into a non-primary
    # workspace reads as "outside the workspace" -- which is exactly
    # what these tests found the first time they ran.
    target = ghost_workspace.stage_path(relative, workspace.root)
    file_tools.edit_file(target, content, confirm=True, root=workspace.root)


# ------------------------------------------------------
# The registry
# ------------------------------------------------------
def test_two_projects_can_be_registered(projects):
    names = [w["name"] for w in wm.get_workspace_list()]

    assert sorted(names) == ["alpha", "beta"]


def test_the_first_workspace_becomes_primary(projects):
    primary = [w for w in wm.get_workspace_list() if w["primary"]]

    assert len(primary) == 1
    assert primary[0]["name"] == "alpha"


def test_the_primary_is_the_directory_the_file_tools_use(projects):
    wm.set_primary_workspace(projects["beta"].id)

    # One definition of "where ARIA is". A second copy is how a path
    # check passes in one place and fails in another.
    assert file_tools.workspace_root() == projects["beta"].root


def test_adding_the_same_directory_twice_returns_the_first(projects):
    again = wm.add_workspace(projects["alpha"].root_path, "alpha-again")

    # Identity is the resolved path, not the name: two entries for one
    # directory would each keep their own staging view of the same
    # files, and committing one would silently make the other's diffs
    # wrong.
    assert again.id == projects["alpha"].id
    assert len(wm.get_workspace_list()) == 2


def test_removing_a_workspace_leaves_its_staging_alone(projects):
    stage_in(projects["beta"], "config.py", "beta staged\n")
    ghost = projects["beta"].root / ghost_workspace.STAGING_DIRNAME

    assert wm.remove_workspace(projects["beta"].id) is True

    # Bookkeeping, never a delete. Staged work in a project ARIA stopped
    # tracking is still the user's work.
    assert ghost.is_dir()
    assert [w["name"] for w in wm.get_workspace_list()] == ["alpha"]


def test_removing_the_primary_promotes_another(projects):
    wm.remove_workspace(projects["alpha"].id)

    primary = [w for w in wm.get_workspace_list() if w["primary"]]
    assert len(primary) == 1
    assert primary[0]["name"] == "beta"
    assert file_tools.workspace_root() == projects["beta"].root


def test_an_unknown_workspace_is_an_error_not_a_guess(projects):
    for call in (lambda: wm.get_workspace("nope"),
                 lambda: wm.set_primary_workspace("nope"),
                 lambda: wm.get_workspace_details("nope")):
        with pytest.raises(wm.WorkspaceError):
            call()


def test_the_primary_is_always_active(projects):
    active = wm.set_active_workspaces([projects["beta"].id])

    assert projects["alpha"].id in active     # it is primary
    assert projects["beta"].id in active


# ------------------------------------------------------
# Isolation, which is the point
# ------------------------------------------------------
def test_each_workspace_stages_inside_itself(projects):
    alpha_ghost = projects["alpha"].ghost_directory_path
    beta_ghost = projects["beta"].ghost_directory_path

    assert alpha_ghost.startswith(str(projects["alpha"].root))
    assert beta_ghost.startswith(str(projects["beta"].root))
    # Not merely different paths: neither is reachable from the other's
    # root, which is what makes the isolation structural.
    assert not alpha_ghost.startswith(str(projects["beta"].root))


def test_staging_in_one_project_is_invisible_to_the_other(projects):
    stage_in(projects["alpha"], "config.py", "alpha staged\n")

    assert ghost_workspace.staged_files(projects["alpha"].root) == ["config.py"]
    assert ghost_workspace.staged_files(projects["beta"].root) == []


def test_a_read_never_answers_from_another_workspace(projects):
    """The failure the first specification would have produced."""
    stage_in(projects["alpha"], "config.py", "alpha staged\n")

    from_alpha = wm.get_effective_file("config.py", projects["alpha"].id)
    from_beta = wm.get_effective_file("config.py", projects["beta"].id)

    assert from_alpha.read_text(encoding="utf-8") == "alpha staged\n"
    # Beta has nothing staged, so it reads ITS OWN file -- not alpha's
    # staged copy, which a search across workspaces would have returned.
    assert from_beta.read_text(encoding="utf-8") == BETA_TEXT


def test_a_read_names_its_workspace_rather_than_searching(projects):
    import inspect

    source = inspect.getsource(wm.get_effective_file)

    # get_workspace raises on an unknown id; there is no loop over
    # workspaces and no fallback to try another one.
    assert "for " not in source
    assert "get_workspace(workspace_id)" in source


def test_committing_one_project_does_not_touch_the_other(projects):
    stage_in(projects["alpha"], "config.py", "alpha staged\n")
    stage_in(projects["beta"], "config.py", "beta staged\n")

    wm.commit_workspace(projects["alpha"].id, "commit the changes")

    assert (projects["alpha"].root / "config.py").read_text(encoding="utf-8") == "alpha staged\n"
    assert (projects["beta"].root / "config.py").read_text(encoding="utf-8") == BETA_TEXT
    # And beta's proposal is still waiting.
    assert ghost_workspace.staged_files(projects["beta"].root) == ["config.py"]


def test_discarding_one_project_does_not_touch_the_other(projects):
    stage_in(projects["alpha"], "config.py", "alpha staged\n")
    stage_in(projects["beta"], "config.py", "beta staged\n")

    wm.discard_workspace(projects["alpha"].id, "discard the changes")

    assert ghost_workspace.staged_files(projects["alpha"].root) == []
    assert ghost_workspace.staged_files(projects["beta"].root) == ["config.py"]


def test_rolling_back_one_project_does_not_touch_the_other(projects):
    stage_in(projects["alpha"], "config.py", "alpha first\n")
    stage_in(projects["alpha"], "config.py", "alpha second\n")
    stage_in(projects["beta"], "config.py", "beta staged\n")

    wm.rollback_workspace(projects["alpha"].id)

    alpha_staged = ghost_workspace.staging_root(projects["alpha"].root) / "config.py"
    beta_staged = ghost_workspace.staging_root(projects["beta"].root) / "config.py"
    assert alpha_staged.read_text(encoding="utf-8") == "alpha first\n"
    assert beta_staged.read_text(encoding="utf-8") == "beta staged\n"


def test_a_path_from_one_project_is_refused_by_another(projects):
    intruder = str(projects["beta"].root / "config.py")

    # Confinement is checked against the named workspace, not the one the
    # environment happens to point at.
    with pytest.raises(Exception):
        ghost_workspace.stage_path(intruder, projects["alpha"].root)


# ------------------------------------------------------
# What the Control Center will render
# ------------------------------------------------------
def test_a_workspace_describes_itself_completely(projects):
    described = wm.get_workspace_list()[0]

    assert set(described) >= {
        "id", "name", "root_path", "ghost_directory_path", "staged_count",
        "tool_capable", "tool_capable_model", "active", "last_used", "primary",
    }


def test_details_carry_the_pending_changes_and_diffs(projects):
    stage_in(projects["beta"], "config.py", "beta staged\n")

    details = wm.get_workspace_details(projects["beta"].id)
    [pending] = details["pending_changes"]

    assert pending["path"] == "config.py"
    assert "-beta original" in pending["diff"]


def test_the_staged_count_is_read_rather_than_remembered(projects):
    before = wm.get_workspace_details(projects["alpha"].id)["staged_count"]
    stage_in(projects["alpha"], "config.py", "alpha staged\n")
    after = wm.get_workspace_details(projects["alpha"].id)["staged_count"]

    # A cached count disagrees with the directory the moment anything
    # writes to it, and it is the number a user decides to commit from.
    assert (before, after) == (0, 1)


def test_the_single_workspace_view_still_works(projects):
    # describe_workspace predates the registry and is what the existing
    # panel reads. The primary is the same directory it reports.
    described = wm.describe_workspace()

    assert described["project_root"] == str(projects["alpha"].root)
