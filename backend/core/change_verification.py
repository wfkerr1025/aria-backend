"""ARIA Lite - checking her own work before she claims it worked.

The user's words: "I don't see Aria running tests, checking to make sure
the file doesn't harm my file/project before applying/creating it like
you do... when I start having her create large files and editing files,
I need her to ensure that her changes do no harm to my project."

content_check answers "does this parse". That is real and it is not the
question asked. A file can parse perfectly and still break the project
it lands in.

WHAT THIS IS NOT
----------------
It is NOT "run tests on edits, skip them on new files". That rule sounds
reasonable and is wrong, and the counter-example came from this very
session: a brand-new test file, importing nothing and editing nothing,
took this suite from green to red -- because the project has a guard
asserting every test file is registered with its runner, and the new file
was not. A creation is not automatically safe. What matters is not the
file's age but whether the change can reach code that already works.

THE TIERS, AND WHY EACH EARNS ITS TIME
--------------------------------------
Measured on this project:

    parse check          instant   already in content_check
    related suites       ~2-5s     suites naming what changed, plus guards
    the whole suite      ~92s      everything

The first two run on every file ARIA creates, because five seconds is
not a decision anybody needs to think about. The last runs when the
change can actually reach the rest of the project -- when something
outside the tests already imports what was touched. A new file nothing
imports cannot break another module, and the guards cover the way it
CAN break things.

BASELINE, THEN AGAIN
--------------------
A suite that was already red proves nothing about a change, so only
failures that are NEW are attributed to ARIA. Without that, a project
carrying one known failure would have every turn reported as harmful,
and the check would be switched off within a day.

The baseline is taken LAST, and only when it is needed. A run that comes
back green has nothing to attribute, and green is the ordinary case --
so the change goes in, the tests run once, and that is the whole cost.
Only a red run pays for a second pass, with the change removed, to find
out whether those failures were already there. Measured: 16 seconds for
a typical creation on this project rather than 31.

WHAT HAPPENS WHEN IT BREAKS
---------------------------
The same thing content_check does: the SHORTCUT is withdrawn, not the
work. The new file comes back out of the project, its staged copy is put
back, and the reply names the tests that went red. Nothing is thrown
away and nothing is left broken -- the file is still there to be read,
fixed, or committed deliberately.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "Verification",
    "guard_suites",
    "needs_the_whole_suite",
    "select_suites",
    "verify_new_files",
]

# Set to "always" or "never" to override the blast-radius decision about
# the expensive tier. Anything else leaves it to needs_the_whole_suite.
ENV_FULL_SUITE = "ARIA_VERIFY_FULL_SUITE"

# The fast tier must never quietly become the slow one. A selection that
# grows without limit is how a five-second check turns into a
# ninety-second one nobody agreed to.
_MAX_RELATED_SUITES = 12

# Wall-clock ceilings. Exceeding one is reported as "not verified", which
# is honest, rather than failing the turn over a slow test run.
_QUICK_TIMEOUT = 180
_FULL_TIMEOUT = 900

# pytest -q prints these in its short summary. Both forms matter: a
# collection ERROR is exactly what a new file with a bad import causes,
# and it never appears as a FAILED.
_FAILURE_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)

# A test that walks the tree is checking an invariant about the PROJECT,
# not the behaviour of one module -- "every suite is registered", "every
# module is in the manifest", "no file imports this directly". Those are
# precisely the tests a new file breaks, and it will never be named in
# one of them, so nothing else in the selection would find them.
#
# Deliberately broad. Over-including a guard costs a fraction of a second
# and the cap still applies; missing one costs a green report on a
# project that just went red.
_WALKS_THE_TREE = ("rglob(", ".glob(", "os.walk", "listdir", "iterdir")

# ...and walks it from the REPOSITORY, which is what separates a guard
# from an ordinary test that happens to list a directory. Measured here:
# the walk marker alone selected six suites and four were false
# positives -- logging_server_tests globs a temp folder,
# test_file_tools_staging iterdirs a fixture, and skr_and_ipc merely
# mentions the runner in a comment. All four are slow, and none of them
# can be broken by adding a file.
#
# A test that climbs to parents[] or names the tests directory is
# looking at the real project. That is the one a new file breaks.
_AT_THE_REPOSITORY = ("parents[", "tests_dir", "workspace_root(", "REPO")


@dataclass
class Verification:
    """What was checked, and what it found."""

    ran: bool = False
    passed: bool = True
    # Test ids that are red now and were not red before. Only these are
    # attributable to the change.
    new_failures: list[str] = field(default_factory=list)
    suites: list[str] = field(default_factory=list)
    whole_suite: bool = False
    # Failures that were already there. Reported separately because
    # "they pass" would be untrue and "this broke them" would be unfair.
    pre_existing: int = 0
    seconds: float = 0.0
    # Set when the check could not be completed -- no tests, a missing
    # runner, a timeout. Not a pass and not a failure.
    skipped_because: str | None = None

    @property
    def harmed(self) -> bool:
        return self.ran and bool(self.new_failures)

    def _scope(self) -> str:
        if self.whole_suite:
            return "your full test suite"
        count = len(self.suites)
        return f"{count} related suite{'' if count == 1 else 's'}"

    def describe(self) -> str:
        """One line for the reply, or "" when there is nothing to say."""
        if self.skipped_because:
            return f"I did not test this: {self.skipped_because}."
        if not self.ran:
            return ""
        if self.new_failures:
            listed = ", ".join(f"`{name}`" for name in self.new_failures[:3])
            more = (f", and {len(self.new_failures) - 3} more"
                    if len(self.new_failures) > 3 else "")
            return (f"I ran {self._scope()} and this change broke {listed}{more}. "
                    f"I took it back out of your project -- it is staged, so "
                    f"nothing is lost.")
        if self.pre_existing:
            return (f"I ran {self._scope()} ({self.seconds:.0f}s). This change "
                    f"broke nothing -- the {self.pre_existing} failure"
                    f"{'' if self.pre_existing == 1 else 's'} there were already "
                    f"failing before it.")
        return f"I ran {self._scope()} ({self.seconds:.0f}s) and they pass."


def _say(on_progress, message: str) -> None:
    """Tell the user what is taking the time. Never fails the check."""
    if on_progress is None:
        return
    try:
        on_progress(message)
    except Exception:  # pragma: no cover - a progress line is not the work
        logger.debug("could not report progress", exc_info=True)


def _tests_directory(root: Path) -> Path | None:
    for candidate in ("backend/tests", "tests", "test"):
        folder = root / candidate
        if folder.is_dir():
            return folder
    return None


def _holds_tests(path: Path) -> bool:
    """Whether this file actually contains tests.

    The naming patterns catch more than suites. This project's runner is
    called run_all_tests.py, which matches "*_tests.py" exactly -- and
    handing the RUNNER to pytest as if it were a suite would invite it to
    run the whole suite as a side effect of a five-second check. A suite
    has test functions in it; a runner and a conftest do not.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover
        return False
    return "def test_" in text or "class Test" in text


def _test_files(root: Path) -> list[Path]:
    folder = _tests_directory(root)
    if folder is None:
        return []
    seen = {}
    for pattern in ("test_*.py", "*_test.py", "*_tests.py"):
        for path in folder.glob(pattern):
            if _holds_tests(path):
                seen[path.name] = path
    return [seen[name] for name in sorted(seen)]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover
        return ""


def guard_suites(root: Path) -> list[str]:
    """Suites that assert something about the project as a whole.

    Found by what they DO rather than by a hardcoded list: a test that
    walks the tree is checking an invariant, and that is the kind a new
    file breaks without ever being mentioned in it.
    """
    found = []
    for path in _test_files(root):
        text = _read(path)
        if (any(marker in text for marker in _WALKS_THE_TREE)
                and any(marker in text for marker in _AT_THE_REPOSITORY)):
            found.append(path.relative_to(root).as_posix())
    return found


def select_suites(paths, root: Path) -> list[str]:
    """Test files worth running for a change to `paths`.

    Three sources, in order of how directly they bear on the change: the
    changed file itself when it IS a test, suites that name what changed,
    and the project's guards.
    """
    wanted: list[str] = []

    def add(name: str) -> None:
        if name not in wanted:
            wanted.append(name)

    by_name = {path.relative_to(root).as_posix(): path for path in _test_files(root)}

    stems = set()
    for path in paths or []:
        posix = PurePosixPath(str(path).replace(chr(92), "/"))
        if posix.stem:
            stems.add(posix.stem)
        # A changed test file is its own best test.
        if posix.as_posix() in by_name:
            add(posix.as_posix())

    related = [
        name for name, path in by_name.items()
        if name not in wanted and any(stem in _read(path) for stem in stems)
    ]

    # Shortest first. A suite named for the thing that changed is a
    # closer match than a long one mentioning it in passing, and the cap
    # means this ordering decides what gets dropped.
    related.sort(key=lambda name: (len(name), name))
    for name in related[:_MAX_RELATED_SUITES]:
        add(name)

    for name in guard_suites(root):
        add(name)

    return wanted


def needs_the_whole_suite(paths, root: Path) -> bool:
    """Whether this change can reach code beyond itself.

    Not "is the file new" -- whether anything else already depends on it.
    A module the project imports can break callers no selected suite
    mentions; a file nothing imports yet cannot.
    """
    override = str(os.environ.get(ENV_FULL_SUITE, "")).strip().lower()
    if override == "always":
        return True
    if override == "never":
        return False

    changed = {PurePosixPath(str(p).replace(chr(92), "/")) for p in paths or []}
    stems = {p.stem for p in changed if p.suffix == ".py" and p.stem}
    if not stems:
        return False

    for source in root.rglob("*.py"):
        try:
            relative = source.relative_to(root).as_posix()
        except ValueError:  # pragma: no cover
            continue
        if any(part.startswith(".") for part in PurePosixPath(relative).parts):
            continue
        if relative in {p.as_posix() for p in changed}:
            continue
        if "test" in PurePosixPath(relative).parts:
            continue
        text = _read(source)
        for stem in stems:
            if f"import {stem}" in text or f"from {stem}" in text:
                logger.info("%s is imported by %s; checking the whole suite",
                            stem, relative)
                return True
    return False


def _failures(output: str) -> set:
    return set(_FAILURE_LINE.findall(str(output or "")))


# pytest's exit codes. 0 and 1 are verdicts -- everything passed, or
# something failed and was named. The rest are not verdicts at all, and
# treating them as one is the dangerous mistake here: a run that died
# during collection prints no FAILED lines, so "no failures" and "the
# runner crashed" look identical, and comparing two crashed runs reports
# a clean pass over a project nobody actually tested.
#
# Measured on this project: `pytest backend/tests/*_tests.py` dies with
# INTERNALERROR> SystemExit: 1, because one of those suites exits during
# collection. Without this, a selection containing it would have come
# back green every time.
_ALL_PASSED, _TESTS_FAILED = 0, 1


def _run(suites, whole: bool, root: Path):
    """Failures from one run, or None when the run could not happen."""
    from backend.core import file_tools

    try:
        if whole:
            result = file_tools.run_tests("")
        else:
            result = file_tools.run_test_files(suites, timeout=_QUICK_TIMEOUT)
    except Exception as error:
        logger.warning("could not run tests: %s", error)
        return None

    code = result.get("exit_code")
    if code not in (_ALL_PASSED, _TESTS_FAILED):
        logger.warning("test run did not produce a verdict (exit %s)", code)
        return None

    return _failures(result.get("output"))


def _restore(created, staged_bytes, root: Path) -> None:
    """Take the files back out, and put the staged copies back."""
    from backend.core import ghost_workspace

    for name in created:
        try:
            (root / name).unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            logger.exception("could not remove %s after a failed verification", name)
        if name in staged_bytes:
            restored = ghost_workspace.staging_root(root) / name
            restored.parent.mkdir(parents=True, exist_ok=True)
            restored.write_bytes(staged_bytes[name])


def verify_new_files(additions, commit, root: Path, on_progress=None) -> tuple:
    """Run the checks around `commit`, undoing it if the change broke something.

    `commit` is called with the paths and returns the list actually
    written. It is passed in rather than imported so this module stays a
    checker: it decides whether the work stands, not what committing
    means.

    Returns (created, verification).
    """
    from backend.core import ghost_workspace

    verification = Verification()
    names = [str(name) for name in (additions or [])]
    if not names:
        return [], verification

    started = time.monotonic()

    if not _test_files(root):
        verification.skipped_because = "this project has no test suite I could find"
        return commit(names), verification

    suites = select_suites(names, root)
    whole = needs_the_whole_suite(names, root)
    verification.suites = suites
    verification.whole_suite = whole

    if not suites and not whole:
        verification.skipped_because = (
            "nothing in your suite bears on this file")
        return commit(names), verification

    # The staged bytes, kept before the commit consumes them. A revert
    # has to put the work back, not merely remove it: the file is the
    # point of the turn, and losing it to a red test would be a worse
    # outcome than the red test.
    staged_bytes = {}
    for name in names:
        source = ghost_workspace.staging_root(root) / name
        try:
            staged_bytes[name] = source.read_bytes()
        except OSError:  # pragma: no cover - commit will skip it too
            pass

    created = commit(names)
    if not created:
        return created, verification

    # The change goes in and the tests run ONCE. A green run has nothing
    # to attribute, so it needs no baseline -- and green is the ordinary
    # case, which is the one worth making fast. Measured on this project:
    # 16 seconds instead of 31 for a typical creation.
    # Said out loud, because on a real project this is the longest
    # silence in the turn -- sixteen seconds on this one. A progress line
    # is the difference between "she is checking her work" and "it has
    # frozen", and the second reading is the one a blank screen gets.
    _say(on_progress, "checking that this does no harm"
         if not whole else "running your full test suite")

    after = _run(suites, whole, root)
    verification.seconds = time.monotonic() - started

    if after is None:
        verification.skipped_because = "the test command did not produce a verdict"
        return created, verification

    if not after:
        verification.ran = True
        return created, verification

    # Something is red. Only NOW is a baseline worth its cost, and it has
    # to be taken with the change removed -- otherwise it is not a
    # baseline, it is the same run twice.
    logger.info("tests are red; taking a baseline to see whether this change did it")
    _say(on_progress, "something went red; checking whether it was me")
    _restore(created, staged_bytes, root)
    before = _run(suites, whole, root)
    verification.seconds = time.monotonic() - started

    if before is None:
        # The baseline crashed, so nothing can be attributed either way.
        # The file goes back in: an unprovable suspicion is not grounds
        # for taking someone's work away.
        verification.skipped_because = "the baseline run did not complete"
        return commit(names), verification

    new_failures = sorted(after - before)
    verification.ran = True
    verification.new_failures = new_failures
    verification.passed = not new_failures

    if not new_failures:
        logger.info("those failures predate this change; not attributing them")
        verification.pre_existing = len(after)
        return commit(names), verification

    logger.warning("leaving %s out of the project: new failures %s",
                   created, new_failures)
    return [], verification
