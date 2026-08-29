# backend/tests/test_workspace_and_capability.py
#
# Which project ARIA is working in, and whether the model can be asked
# to act in it.
#
# The working directory has been implicit: file_tools reads
# ARIA_TOOL_WORKSPACE or falls back to cwd, and nothing displayed it or
# let anyone change it. On this install that means the workspace is the
# ARIA-Lite source tree, which is a surprising thing to discover by
# watching a commit land.
#
# Capability is the other half. An action is a fenced JSON block naming a
# tool; a model that cannot hold that shape writes prose where an action
# was wanted, and the turn looks like ARIA talking about a change instead
# of proposing one.

from __future__ import annotations

import pytest

from backend.core import file_tools, ghost_workspace
from backend.core import model_capability as mc
from backend.core import workspace_manager as wm


@pytest.fixture
def elsewhere(tmp_path, monkeypatch):
    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))
    return tmp_path


# ------------------------------------------------------
# The working directory
# ------------------------------------------------------
def test_the_project_root_is_the_one_the_file_tools_enforce(elsewhere):
    # One authority. A second copy is how a path check passes in one
    # place and fails in another.
    assert wm.get_project_root() == file_tools.workspace_root()


def test_changing_it_moves_the_boundary_too(tmp_path, monkeypatch):
    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))
    other = tmp_path / "other_project"
    other.mkdir()

    wm.set_project_root(str(other))

    assert wm.get_project_root() == other
    # The boundary enforced at write time moved with it, rather than
    # being kept in step by hand.
    assert file_tools.workspace_root() == other


def test_the_ghost_root_follows_the_project(tmp_path, monkeypatch):
    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))
    other = tmp_path / "another"
    other.mkdir()

    wm.set_project_root(str(other))

    assert wm.get_ghost_root() == other / ghost_workspace.STAGING_DIRNAME / "another"


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_an_empty_directory_is_refused(elsewhere, bad):
    with pytest.raises(wm.WorkspaceError):
        wm.set_project_root(bad)


def test_a_directory_that_does_not_exist_is_refused(elsewhere, tmp_path):
    # Validated before it takes effect: otherwise every later read fails
    # with a confusing error a long way from the setting that caused it.
    with pytest.raises(wm.WorkspaceError):
        wm.set_project_root(str(tmp_path / "no_such_place"))


def test_a_file_is_not_a_working_directory(elsewhere):
    target = elsewhere / "notes.txt"
    target.write_text("x", encoding="utf-8")

    with pytest.raises(wm.WorkspaceError):
        wm.set_project_root(str(target))


def test_a_refused_change_leaves_the_directory_alone(elsewhere, tmp_path):
    before = wm.get_project_root()

    with pytest.raises(wm.WorkspaceError):
        wm.set_project_root(str(tmp_path / "nope"))

    assert wm.get_project_root() == before


# ------------------------------------------------------
# What the panel shows
# ------------------------------------------------------
def test_the_description_has_what_the_panel_needs(elsewhere):
    described = wm.describe_workspace()

    assert set(described) >= {
        "project_root", "ghost_root", "staged_count",
        "active_model", "tool_capable", "capability_warning",
    }


def test_the_staged_count_is_reported(elsewhere):
    (elsewhere / "notes.txt").write_text("one\n", encoding="utf-8", newline="")
    target = ghost_workspace.stage_path("notes.txt")
    file_tools.edit_file(target, "two\n", confirm=True)

    described = wm.describe_workspace()

    # "You have changes waiting" is what a user most needs before
    # changing directory: staging is per-project, so moving away leaves
    # them behind rather than carrying them along.
    assert described["staged_count"] == 1
    assert described["staged_files"] == ["notes.txt"]


def test_refresh_is_the_same_reading(elsewhere):
    assert wm.refresh_workspace() == wm.describe_workspace()


# ------------------------------------------------------
# Capability
# ------------------------------------------------------
@pytest.mark.parametrize("model_id,capable", [
    ("nemo-12b-q5", True),
    ("mistral-7b-q4km", True),
    ("phi-3-mini-4k-instruct-q4", False),
    ("qwen2.5-0.5b-instruct-q4_k_m", False),
])
def test_capability_is_read_off_the_installed_models(model_id, capable):
    assert mc.supports_tool_use(model_id) is capable


def test_no_model_id_means_the_router_chooses_and_that_is_capable():
    # None is Cloud or Automatic, where a frontier model answers.
    # Reporting False would warn a user about the strongest
    # configuration they have.
    assert mc.supports_tool_use(None) is True
    assert mc.capability_warning(None) is None


def test_the_capability_list_is_the_evidence_list():
    from backend.core.evidence_routing import EVIDENCE_MODEL_ALLOWLIST

    # Not a second roster of "the good models". Two lists drift, and the
    # one that drifts is the one nobody is looking at.
    assert mc.TOOL_CAPABLE_MODELS is EVIDENCE_MODEL_ALLOWLIST


def test_a_capable_model_needs_no_recommendation():
    assert mc.recommended_tool_model("nemo-12b-q5") is None


def test_an_incapable_model_gets_one_that_is_installed():
    from backend.core.model_registry import get_model_ids

    recommended = mc.recommended_tool_model("qwen2.5-0.5b-instruct-q4_k_m")

    assert recommended in get_model_ids()
    assert mc.supports_tool_use(recommended)


def test_nothing_capable_installed_recommends_nothing(monkeypatch):
    from backend.core import complexity_router as cr

    monkeypatch.setattr(cr, "_is_installed", lambda model_id: False)

    # Naming a model that is not there would send the caller to a load
    # that cannot succeed.
    assert mc.recommended_tool_model("qwen2.5-0.5b-instruct-q4_k_m") is None


# ------------------------------------------------------
# Warnings, not silence
# ------------------------------------------------------
def test_an_incapable_model_produces_a_warning():
    warning = mc.capability_warning("qwen2.5-0.5b-instruct-q4_k_m")

    assert warning
    assert "qwen2.5-0.5b" in warning
    # It says what will happen instead, not just that something is wrong.
    assert "prose" in warning


def test_the_warning_names_a_way_forward():
    assert "nemo-12b" in mc.capability_warning("phi-3-mini-4k-instruct-q4")


def test_the_warning_is_honest_when_there_is_no_way_forward(monkeypatch):
    from backend.core import complexity_router as cr

    monkeypatch.setattr(cr, "_is_installed", lambda model_id: False)

    warning = mc.capability_warning("qwen2.5-0.5b-instruct-q4_k_m")

    assert "no model that can is installed" in warning


# ------------------------------------------------------
# What this deliberately does not do
# ------------------------------------------------------
def test_capability_detection_switches_nothing():
    import inspect

    source = inspect.getsource(mc)
    code = " ".join(
        line for line in source.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )

    # Pinning a model was implemented, measured and removed once already:
    # a concrete model_id is one the safety gate evaluates, and on a
    # loaded machine the gate refuses turns that would otherwise have
    # run. This module recommends; it does not switch.
    for forbidden in ("set_explicit_model_override", "switch_model", "set_default_model"):
        assert forbidden not in code


def test_the_evidence_floor_already_lands_action_turns_on_a_capable_model():
    """Which is why no switch is needed on the path that matters.

    complexity_router floors an evidence-bearing prompt at the 7B and
    steps down no further. Both models at or above that floor are
    tool-capable, so an action turn arrives on one without anything
    being pinned.
    """
    from backend.core import complexity_router as cr

    floor = cr._LADDER.index(cr.MEDIUM_MODEL_ID)
    for model_id in cr._LADDER[:floor + 1]:
        assert mc.supports_tool_use(model_id), model_id
