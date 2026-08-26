# backend/tests/_sandbox_subprocess_fixtures.py
#
# Module-level (picklable) functions for sandbox_and_tool_registry_tests.py's
# run_in_subprocess() tests — multiprocessing pickles the target callable,
# so it cannot be a closure/lambda defined inside the test function itself.

import time


def add(a, b):
    return a + b


def infinite_loop():
    while True:
        time.sleep(0.05)


def raises():
    raise ValueError("boom from subprocess")
