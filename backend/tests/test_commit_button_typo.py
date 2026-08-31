# backend/tests/test_commit_button_typo.py
#
# "The Commit inside the Settings doesn't work"
#
# From the packet log:
#
#   20:15:21.450  workspace_commit_request  user_text: "commit the changess"
#   20:15:21.463  progress  "running your full test suite"
#   ...           nothing, for as long as the user waited
#
# Two faults, one letter apart.
#
# The typo. "commit the changess" is "commit the changes" with one letter
# doubled, and the consent check was a substring test, so it matched
# nothing and the commit was refused. The same user hit the same slip
# earlier in the week with "discard the changess". From their side the
# button simply did nothing.
#
# And the wait. commit_changes refused in thirteen milliseconds and the
# verification then ran the WHOLE test suite -- three minutes on this
# project -- over a commit that had written nothing at all. The answer
# was known before the tests started.

from __future__ import annotations

import pathlib
import time

import pytest

from backend.core import file_tools
from backend.core import ghost_workspace as ghost
from backend.core import workspace_manager


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))
    (tmp_path / "tests").mkdir()
    (tmp_path / "greet.py").write_text("def hello():\n    return 'hi'\n",
                                       encoding="utf-8", newline="")
    (tmp_path / "tests" / "test_greet.py").write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n"
        "from greet import hello\n"
        "def test_hello():\n"
        "    assert hello() == 'hi'\n", encoding="utf-8", newline="")
    return tmp_path


def stage_an_edit():
    pathlib.Path(ghost.stage_path("greet.py")).write_text(
        "def hello():\n    # tidied\n    return 'hi'\n", encoding="utf-8", newline="")


# --- the typo ---------------------------------------------------------

@pytest.mark.parametrize("said", [
    "commit the changess",     # the reported one
    "commit the chnages",      # a transposition, the commonest slip there is
    "commit teh changes",
    "commit the changes",      # and the exact phrase, still
])
def test_a_commit_survives_one_typo(said):
    assert ghost.requests_commit(said) is True


@pytest.mark.parametrize("said", [
    "dont commit the changess",
    "do not commit the changes",
    "don't commit the changes yet",
])
def test_a_negation_wins_however_it_is_spelled(said):
    """The veto runs first and is not fuzzy in the permissive direction."""
    assert ghost.requests_commit(said) is False


@pytest.mark.parametrize("said", [
    "delete the changes",
    "revert the changes",
    "discard the changes",
    "show me the changes",
    "I am worried about the changes",
])
def test_a_different_sentence_is_not_one_typo_away(said):
    assert ghost.requests_commit(said) is False


def test_asking_about_a_commit_is_not_asking_for_one():
    """A substring test cannot tell a question from an instruction, and
    this one contained the phrase."""
    assert ghost.requests_commit("what happens if I commit the changes?") is False
    assert ghost.requests_commit("should I commit the changes") is False


@pytest.mark.parametrize("said", ["do it", "go", "proceed"])
def test_short_phrases_stay_exact(said):
    """At five characters one edit is a different phrase, not a slip."""
    assert ghost.requests_commit(said + "x") is False


def test_the_same_slip_in_a_discard(project):
    """Reported first, and fixed by the same rule."""
    assert ghost.requests_discard("discard the changess") is True
    assert ghost.requests_discard("dont discard the changess") is False


# --- the wait ---------------------------------------------------------

def test_a_refused_commit_does_not_run_the_test_suite(project):
    """The answer was known thirteen milliseconds in."""
    stage_an_edit()
    workspace_id = workspace_manager.add_workspace(str(project), "typo").id

    said = []
    started = time.monotonic()
    report = workspace_manager.commit_workspace(
        workspace_id, "please don't commit anything", on_progress=said.append)
    elapsed = time.monotonic() - started

    assert report["status"] == "refused"
    assert said == [], "a commit that wrote nothing must not announce a test run"
    # The real suite takes minutes; even this tiny one takes a second.
    assert elapsed < 0.5, f"a refused commit took {elapsed:.1f}s"


def test_a_commit_with_nothing_staged_does_not_run_the_suite(project):
    workspace_id = workspace_manager.add_workspace(str(project), "empty").id

    said = []
    report = workspace_manager.commit_workspace(
        workspace_id, "commit the changes", on_progress=said.append)

    assert report["status"] == "empty"
    assert said == []


def test_a_real_commit_still_runs_the_suite(project):
    """The fail-fast path must not swallow the check it was added beside."""
    stage_an_edit()
    workspace_id = workspace_manager.add_workspace(str(project), "real").id

    said = []
    report = workspace_manager.commit_workspace(
        workspace_id, "commit the changes", on_progress=said.append)

    assert report["status"] == "committed"
    assert said, "a commit that wrote something must run the tests"
    assert report["verification"]["ran"] is True
