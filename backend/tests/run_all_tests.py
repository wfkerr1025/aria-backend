# backend/tests/run_all_tests.py
#
# A wrapper. pytest is the runner.
#
# This file used to be the runner, and it held a hand-maintained list of
# every suite: TESTS, PYTEST_TESTS, LEGACY_TESTS, NETWORK_TESTS. Each
# was launched in its own subprocess, and a suite absent from all four
# lists simply never ran.
#
# That is exactly what happened. pytest's default python_files is
# "test_*.py *_test.py", which never matched the 33 *_tests.py suites
# here -- so "pytest backend/tests" reported 3623 passing while
# collecting about seventy percent of the tests, and nothing anywhere
# said so. Inside those uncollected suites were four assertions that had
# been contradicting deliberate, shipped behaviour for months, and one
# genuine routing bug.
#
# pytest.ini now names both patterns, so collection is automatic and a
# new suite is picked up by existing, not by being remembered. There is
# no list here to fall out of date because there is no list.
#
# Kept rather than deleted because scripts, notes and habits refer to it.
# It runs pytest ONCE -- not per file, which is what made the old runner
# slow and let a suite's result depend on which subprocess it landed in.
#
# WHY THE __main__ GUARD MATTERS
# ------------------------------
# This filename matches "*_tests.py", so pytest collects THIS FILE too.
# Without the guard, collecting it would import it, importing it would
# run the whole suite, and that suite would collect this file again. The
# first attempt at unified collection hung for exactly this reason.

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def main(argv=None) -> int:
    """Run the whole suite once, through pytest, and return its exit code."""
    arguments = list(argv if argv is not None else sys.argv[1:])
    command = [sys.executable, "-m", "pytest", "backend/tests", *arguments]

    print("The runner is pytest. Running:", " ".join(command))
    return subprocess.run(command, cwd=str(REPO_ROOT)).returncode


if __name__ == "__main__":
    raise SystemExit(main())
