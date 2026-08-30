# backend/tests/test_change_verification.py
#
# "I need her to ensure that her changes do no harm to my project."
#
# content_check answers "does this parse", which is a different question.
# These cover the one actually asked: after ARIA writes a file, do the
# tests that bear on it still pass, and if not, what does she do about
# it.
#
# The rule under test is deliberately NOT "run tests on edits, skip them
# on new files". That rule was proposed in this session and disproved by
# this session: a brand-new test file, importing nothing and editing
# nothing, took this project from green to red because a guard asserts
# every suite is registered with the runner. A creation is not
# automatically safe, so the tier is chosen by blast radius instead --
# whether anything already imports what changed.
#
# These spawn real pytest subprocesses against small temporary projects.
# That is slower than faking the runner and it is the point: the thing
# being tested is whether ARIA can tell a red suite from a green one,
# and a fake runner would only prove she can read a dict.

from __future__ import annotations

import json

import pytest

from backend.core import change_verification as cv
from backend.core import file_tools
from backend.core import ghost_workspace as ghost
from backend.core.action_render import render_actions_for_reading
from backend.core.tool_orchestrator import run_answer_actions

GUARD = (
    "import pathlib\n"
    "def test_every_module_is_in_the_manifest():\n"
    "    root = pathlib.Path(__file__).resolve().parents[1]\n"
    "    listed = set((root / 'MANIFEST').read_text().split())\n"
    "    found = {p.name for p in root.glob('*.py')}\n"
    "    assert found <= listed, f'not listed: {sorted(found - listed)}'\n"
)

PASSING = (
    "import sys, pathlib\n"
    "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n"
    "from greet import hello\n"
    "def test_hello():\n"
    "    assert hello() == 'hi'\n"
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A small real project: a module, a suite, and a project-wide guard."""
    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))
    monkeypatch.delenv(cv.ENV_FULL_SUITE, raising=False)

    (tmp_path / "tests").mkdir()
    (tmp_path / "greet.py").write_text("def hello():\n    return 'hi'\n",
                                       encoding="utf-8", newline="")
    (tmp_path / "MANIFEST").write_text("greet.py\n", encoding="utf-8", newline="")
    (tmp_path / "tests" / "test_greet.py").write_text(PASSING, encoding="utf-8",
                                                      newline="")
    (tmp_path / "tests" / "test_manifest_guard.py").write_text(GUARD, encoding="utf-8",
                                                               newline="")
    return tmp_path


def create(path, content, prompt=None):
    """Have ARIA create a file, and return (report, what the user reads)."""
    answer = "```json\n" + json.dumps(
        {"tool": "edit_file", "path": path, "content": content}) + "\n```"
    report = run_answer_actions(answer, prompt or f"create {path} for me") or {}
    shown = render_actions_for_reading(
        answer, staged=bool(report.get("staged")), expected_action=True,
        created=report.get("created") or (), problems=report.get("problems") or {},
        new_folders=report.get("new_folders") or (),
        relocated=report.get("relocated") or {},
        verification=report.get("verification"))
    return report, shown


# --- choosing what to run ---------------------------------------------

def test_a_tree_walking_test_is_recognised_as_a_guard(project):
    """The tier that catches a new file, since no new file is named in one."""
    assert cv.guard_suites(project) == ["tests/test_manifest_guard.py"]


def test_the_runner_is_never_selected_as_a_suite(project):
    """run_all_tests.py matches "*_tests.py" and is not a suite.

    Handing the runner to pytest would run everything as a side effect of
    a check that is supposed to take seconds.
    """
    (project / "tests" / "run_all_tests.py").write_text(
        'SUITES = ["test_greet.py"]\n', encoding="utf-8", newline="")

    assert "tests/run_all_tests.py" not in cv.select_suites(["greet.py"], project)


def test_a_suite_naming_what_changed_is_selected(project):
    """test_greet.py mentions greet, so it bears on a change to greet.py."""
    chosen = cv.select_suites(["greet.py"], project)

    assert "tests/test_greet.py" in chosen
    assert "tests/test_manifest_guard.py" in chosen


def test_a_new_unrelated_file_still_gets_the_guards(project):
    """Nothing names it, which is exactly why the invariants matter."""
    assert cv.select_suites(["notes.md"], project) == ["tests/test_manifest_guard.py"]


# --- how far the change can reach -------------------------------------

def test_a_module_the_project_imports_gets_the_whole_suite(project):
    """greet is imported elsewhere, so callers no suite names may break."""
    (project / "app.py").write_text("from greet import hello\n",
                                    encoding="utf-8", newline="")

    assert cv.needs_the_whole_suite(["greet.py"], project) is True


def test_a_file_nothing_imports_does_not(project):
    """The expensive tier is for reach, not for age."""
    assert cv.needs_the_whole_suite(["helper.py"], project) is False
    assert cv.needs_the_whole_suite(["notes.md"], project) is False


def test_the_decision_can_be_overridden(project, monkeypatch):
    monkeypatch.setenv(cv.ENV_FULL_SUITE, "always")
    assert cv.needs_the_whole_suite(["notes.md"], project) is True

    monkeypatch.setenv(cv.ENV_FULL_SUITE, "never")
    (project / "app.py").write_text("from greet import hello\n",
                                    encoding="utf-8", newline="")
    assert cv.needs_the_whole_suite(["greet.py"], project) is False


# --- what happens to the file -----------------------------------------

def test_a_harmless_file_is_created_and_the_tests_are_reported(project):
    report, shown = create("notes.md", "Some notes.\n")

    assert (project / "notes.md").exists()
    assert report["verification"].ran is True
    assert report["verification"].new_failures == []
    assert "they pass" in shown


def test_a_file_that_breaks_the_project_is_taken_back_out(project):
    """It parses. It is new. It imports nothing. It still breaks the build.

    This is the case the rejected "new files are safe" rule would have
    waved straight through.
    """
    report, shown = create("helper.py", "def helper():\n    return 42\n")

    assert not (project / "helper.py").exists()
    assert report["created"] == []
    # The work is not lost -- only the shortcut is.
    assert ghost.staged_files() == ["helper.py"]

    verification = report["verification"]
    assert verification.harmed is True
    assert verification.new_failures == [
        "tests/test_manifest_guard.py::test_every_module_is_in_the_manifest"]


def test_the_reply_does_not_contradict_the_verdict(project):
    """No "Done", and no invitation to approve what just failed."""
    _, shown = create("helper.py", "def helper():\n    return 42\n")

    assert "took it back out" in shown
    assert "Done:" not in shown
    assert "yes, do it" not in shown


def test_a_suite_that_was_already_red_is_not_blamed_on_the_change(project):
    """Otherwise every turn on an imperfect project reads as harmful.

    The broken test walks the tree, so it IS in the selection -- this
    only tests attribution if the failing test actually runs.
    """
    (project / "tests" / "test_already_broken.py").write_text(
        "import pathlib\n"
        "def test_that_was_failing_before_aria_arrived():\n"
        "    root = pathlib.Path(__file__).resolve().parents[1]\n"
        "    list(root.glob('*.py'))\n"
        "    assert False\n",
        encoding="utf-8", newline="")

    # It has to actually run, or this proves nothing about attribution.
    assert "tests/test_already_broken.py" in cv.select_suites(["notes.md"], project)

    report, shown = create("notes.md", "Some notes.\n")

    assert (project / "notes.md").exists()
    assert report["verification"].ran is True
    assert report["verification"].new_failures == []
    assert report["verification"].harmed is False
    assert report["verification"].pre_existing == 1
    # And it says so, rather than claiming a clean run it did not have.
    assert "broke nothing" in shown
    assert "already failing" in shown


def test_a_project_with_no_tests_is_told_so_rather_than_reassured(project):
    """"I ran the tests" would be a lie; silence would imply it too."""
    for suite in (project / "tests").glob("*.py"):
        suite.unlink()

    report, shown = create("notes.md", "Some notes.\n")

    assert (project / "notes.md").exists()
    assert report["verification"].ran is False
    assert "no test suite" in shown


def test_a_broken_checker_costs_the_check_and_not_the_file(project, monkeypatch):
    """A fault here must never be the reason a user loses their work."""
    def explode(*a, **k):
        raise RuntimeError("the checker is broken")

    monkeypatch.setattr(cv, "verify_new_files", explode)
    report, _ = create("notes.md", "Some notes.\n")

    assert (project / "notes.md").exists()
    assert report["created"] == ["notes.md"]
    assert report["verification"] is None


# --- the runner it uses ------------------------------------------------

def test_run_test_files_refuses_anything_that_is_not_a_test_file(project):
    """The argument list is built here, so it cannot become an option."""
    with pytest.raises(file_tools.WorkspaceError):
        file_tools.run_test_files(["-x"])
    with pytest.raises(file_tools.WorkspaceError):
        file_tools.run_test_files(["../../../etc/passwd"])
    with pytest.raises(file_tools.WorkspaceError):
        file_tools.run_test_files([])


def test_run_test_files_runs_several_suites_in_one_process(project):
    """run_tests takes one scope and appends it as a single argument."""
    result = file_tools.run_test_files(
        ["tests/test_greet.py", "tests/test_manifest_guard.py"])

    assert result["passed"] is True
    assert result["files"] == ["tests/test_greet.py", "tests/test_manifest_guard.py"]


# --- checking must not itself be a change ------------------------------

def test_running_the_tests_leaves_nothing_behind(project):
    """The step whose purpose is leaving the project alone must do that.

    Measured: a verification run dropped .pytest_cache/ and __pycache__/
    into the user's project -- ARIA creating files nobody asked for while
    checking that she had not.
    """
    create("NOTES.md", "Notes about greet.\n")

    left = sorted(p.name for p in project.iterdir()
                  if p.name not in {".aria_staging"})

    assert ".pytest_cache" not in left
    assert "__pycache__" not in left
    assert left == ["MANIFEST", "NOTES.md", "greet.py", "tests"]


def test_the_cache_flag_is_only_added_for_pytest(project, monkeypatch):
    """A different runner would reject it and fail for the wrong reason."""
    monkeypatch.setenv(file_tools.ENV_TEST_COMMAND, "python -c pass")
    result = file_tools.run_test_files(["tests/test_greet.py"])

    assert "no:cacheprovider" not in result["command"]


# --- saying what is taking the time ------------------------------------

def test_the_user_is_told_while_the_tests_run(project):
    """Sixteen seconds of blank screen reads as a hang, not as care."""
    said = []
    answer = '```json\n{"tool": "edit_file", "path": "NOTES.md", "content": "hi\n"}\n```'
    run_answer_actions(answer, "create NOTES.md", on_progress=said.append)

    assert "checking that this does no harm" in said


def test_a_progress_callback_that_throws_does_not_lose_the_file(project):
    """A progress line is not the work, and must never cost it."""
    def explode(_message):
        raise RuntimeError("no listener")

    answer = '```json\n{"tool": "edit_file", "path": "NOTES.md", "content": "hi\n"}\n```'
    report = run_answer_actions(answer, "create NOTES.md", on_progress=explode)

    assert report["created"] == ["NOTES.md"]
    assert (project / "NOTES.md").exists()
