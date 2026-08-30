# backend/tests/test_new_folders_and_honest_failures.py
#
# Two faults from one live turn, and they were the same fault twice.
#
# Asked to "create a player_inventory.cs with stackable slots", nemo-12b
# proposed src/player_inventory.cs. The project had no src/, so
# stage_path refused it -- correctly, by its own rule, because a missing
# parent directory is usually a model inventing a path. Nothing was
# written.
#
# The reply said:
#
#     Staged:
#     - write `src/player_inventory.cs` (17 lines)
#
# Nothing was staged. The renderer described the model's PROPOSAL and
# never looked at what happened to it, so a refusal and a success read
# identically. That is the failure the user reported as "she says she
# did it and she didn't", and no amount of better prompting touches it.
#
# The second fault is the refusal itself. A guard that cannot tell "the
# file is not there" from "the file should not be there" refuses the
# ordinary request -- put a new file in a new folder -- to catch the rare
# one. The filesystem cannot make that distinction, because the
# directory is missing either way. The user's own words can, and they
# are the authority this codebase already uses for consent.

from __future__ import annotations

import json

import pytest

from backend.core import file_tools
from backend.core import ghost_workspace as ghost
from backend.core.action_render import render_actions_for_reading
from backend.core.tool_orchestrator import run_answer_actions
from backend.websocket.handlers import _failed_paths

CODE = "public class PlayerInventory\n{\n    int slots = 36;\n}\n"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))
    return tmp_path


def turn(prompt, path, content=CODE):
    """Run one file-creating turn and return (report, what the user reads)."""
    answer = "```json\n" + json.dumps(
        {"tool": "edit_file", "path": path, "content": content}) + "\n```"
    report = run_answer_actions(answer, prompt) or {}
    shown = render_actions_for_reading(
        answer,
        staged=bool(report.get("staged")),
        expected_action=True,
        created=report.get("created") or (),
        problems=report.get("problems") or {},
        failed=_failed_paths(report),
        new_folders=report.get("new_folders") or (),
        relocated=report.get("relocated") or {},
    )
    return report, shown


# --- the reply says what happened -------------------------------------

def test_a_refused_action_is_not_reported_as_staged(project):
    """The exact live failure: refused, and described as done."""
    report, shown = turn("update the api file in the scripts folder",
                         "made/up/tree/api.cs")

    assert not (project / "made").exists()
    assert report["created"] == []
    assert "Staged:" not in shown
    assert "Done:" not in shown
    assert "could not" in shown
    # And the reason, so the user can act on it rather than guess.
    assert "is not a directory in the project" in shown


def test_a_refused_action_does_not_claim_a_folder_it_never_made(project):
    """No "could not write it" and "this will add the folder" together."""
    _, shown = turn("update the api file in the scripts folder",
                    "made/up/tree/api.cs")

    assert "will add the folder" not in shown


def test_failed_paths_reads_results_not_the_proposal():
    """The mapping is built from what ran, which is the whole point."""
    report = {
        "actions": [{"tool": "edit_file", "args": {"path": "a.py"}, "step_id": "1"},
                    {"tool": "edit_file", "args": {"path": "b.py"}, "step_id": "2"}],
        "results": [{"step_id": "1", "status": "ok", "error": None},
                    {"step_id": "2", "status": "failed", "error": "nope"}],
    }
    assert _failed_paths(report) == {"b.py": "nope"}


# --- a new file may bring the folder the user asked for ---------------

def test_a_new_file_creates_the_folder_the_user_named(project):
    report, shown = turn("create src/player_inventory.cs with stackable slots",
                         "src/player_inventory.cs")

    assert (project / "src" / "player_inventory.cs").read_text() == CODE
    assert report["created"] == ["src/player_inventory.cs"]
    # The new tree is named, not left to be discovered.
    assert "`src/`" in shown


def test_an_edit_into_an_invented_tree_is_still_refused(project):
    """The guard this replaced was right about this case, and still is."""
    with pytest.raises(ghost.GhostError):
        ghost.stage_path("made/up/tree/api.py")


def test_a_folder_the_user_never_mentioned_is_not_created(project):
    """The live turn. The user asked for a file; they get a file."""
    report, shown = turn(
        "create a player_inventory.cs with stackable slots up to 99 items",
        "src/player_inventory.cs")

    assert (project / "player_inventory.cs").read_text() == CODE
    assert not (project / "src").exists()
    # And the reply names where the file actually is. Saying
    # "src/player_inventory.cs" would send the user to a folder that does
    # not exist.
    assert "`player_inventory.cs`" in shown
    assert "`src/player_inventory.cs`" not in shown
    assert report["relocated"] == {"src/player_inventory.cs": "player_inventory.cs"}


def test_an_existing_folder_is_always_kept(project):
    """Filing something into a tree that exists is helpful, not invented."""
    (project / "src").mkdir()
    turn("create a player_inventory.cs with stackable slots",
         "src/player_inventory.cs")

    assert (project / "src" / "player_inventory.cs").exists()


def test_a_user_talking_about_folders_keeps_the_model_s_path(project):
    """Silence is the signal. If they mentioned folders at all, do not guess.

    Relocating here would mean overruling a placement that may well be a
    reading of something said earlier in the request.
    """
    from backend.core.tool_orchestrator import _where_the_user_asked_for_it

    assert _where_the_user_asked_for_it(
        "made/up/api.cs", "put the api file in the scripts folder") == "made/up/api.cs"
    assert _where_the_user_asked_for_it(
        "made/up/api.cs", "create an api file") == "api.cs"


# --- the folder list itself -------------------------------------------

def test_new_directories_for_names_every_missing_level_outermost_first(project):
    (project / "docs").mkdir()

    assert ghost.new_directories_for("docs/api/v2/spec.md") == ["docs/api", "docs/api/v2"]
    assert ghost.new_directories_for("docs/spec.md") == []
    assert ghost.new_directories_for("spec.md") == []


def test_new_directories_for_never_raises_on_a_path_outside_the_project(project):
    """Display code calls this. A refusal belongs to stage_path, not here."""
    assert ghost.new_directories_for("../../etc/passwd") == []


def test_a_new_file_in_a_new_folder_still_has_its_content_checked(project):
    """The shortcut is what a syntax error withdraws, not the folder rule."""
    report, shown = turn("create tools/open_world.py",
                         "tools/open_world.py",
                         content="def open_world:\n    print('hi')\n")

    assert report["created"] == []
    assert not (project / "tools" / "open_world.py").exists()
    assert "does not parse" in shown


# --- an answer does not ask permission for work it just finished ------

def test_a_trailing_confirmation_clause_is_dropped_once_the_file_exists():
    """Measured on nemo-12b, and the two lines sat next to each other.

        Here's a proposal for creating a player inventory in C#. Please confirm:

        Done:
        - created `player_inventory.cs` (21 lines)

    The ask started mid-line, so line matching never saw it.
    """
    from backend.core.action_render import _drop_confirmation_requests as drop

    assert drop("Here's a proposal for a player inventory. Please confirm:") == (
        "Here's a proposal for a player inventory.")
    assert drop("This uses a Dictionary. Let me know if you want a List.") == (
        "This uses a Dictionary.")
    assert drop("Please confirm if this meets your requirement") == ""


def test_the_model_s_own_explanation_survives():
    """Only the ask goes. What it says about the work is worth reading."""
    from backend.core.action_render import _drop_confirmation_requests as drop

    kept = "I made a PlayerInventory class with a 6x6 grid and a toolbar."
    assert drop(kept) == kept
    assert drop("I reviewed it and the braces balance.") == (
        "I reviewed it and the braces balance.")
