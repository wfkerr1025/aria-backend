# backend/tests/sandbox_and_tool_registry_tests.py
#
# Regression tests for Phase 2:
#   - backend/core/sandbox.py: run_in_sandbox() (thread + hard timeout +
#     monitored-not-killed memory), run_in_subprocess() (real process
#     isolation, CAN be force-killed on timeout).
#   - backend/core/tool_registry.py: ToolSchema/register_tool()/
#     execute_tool() — validation, permission gating, sandboxed
#     execution, and the two real built-in tools (weather/web_search).
#
# Self-contained plain-assert tests. Run directly:
#
#   python backend/tests/sandbox_and_tool_registry_tests.py

from __future__ import annotations

import os
import sys
import time
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if str(os.path.dirname(os.path.abspath(__file__))) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ============================================================
# PART 1 — sandbox.py: run_in_sandbox() (thread-based)
# ============================================================
def test_run_in_sandbox_returns_the_callables_result_on_success():
    from backend.core.sandbox import run_in_sandbox

    result = run_in_sandbox(lambda: 2 + 2)
    assert result.ok is True
    assert result.value == 4
    assert result.timed_out is False


def test_run_in_sandbox_enforces_a_hard_timeout():
    from backend.core.sandbox import run_in_sandbox, SandboxLimits

    def slow():
        time.sleep(2)
        return "too late"

    start = time.perf_counter()
    result = run_in_sandbox(slow, limits=SandboxLimits(timeout_seconds=0.2))
    elapsed = time.perf_counter() - start

    assert result.ok is False
    assert result.timed_out is True
    assert elapsed < 1.0, f"caller must be unblocked near the timeout, not the callable's real duration (took {elapsed}s)"


def test_run_in_sandbox_reports_enforcement_honestly():
    from backend.core.sandbox import run_in_sandbox

    result = run_in_sandbox(lambda: 1)
    # Must not claim a hard memory/CPU cap it doesn't actually have —
    # see sandbox.py's module docstring for why.
    assert result.enforcement["timeout"] == "hard"
    assert "not-killed" in result.enforcement["memory"] or "monitored" in result.enforcement["memory"]


def test_run_in_sandbox_captures_an_exception_without_crashing_the_caller():
    from backend.core.sandbox import run_in_sandbox

    def boom():
        raise ValueError("deliberate failure")

    result = run_in_sandbox(boom)
    assert result.ok is False
    assert "deliberate failure" in result.error


def test_run_in_sandbox_flags_a_real_memory_overshoot():
    from backend.core.sandbox import run_in_sandbox, SandboxLimits

    def allocate_a_lot():
        # ~50MB of real allocation — comfortably over a tiny budget,
        # comfortably under any real system's actual limit.
        block = bytearray(50 * 1024 * 1024)
        time.sleep(0.15)  # give the monitor thread time to observe it
        return len(block)

    result = run_in_sandbox(allocate_a_lot, limits=SandboxLimits(timeout_seconds=5.0, memory_limit_mb=5.0))
    assert result.memory_exceeded is True
    assert result.ok is False


# ============================================================
# PART 2 — sandbox.py: run_in_subprocess() (real process isolation)
# ============================================================
def test_run_in_subprocess_returns_a_real_result():
    from backend.core.sandbox import run_in_subprocess
    import _sandbox_subprocess_fixtures as fx

    result = run_in_subprocess(fx.add, 10, 20)
    assert result.ok is True
    assert result.value == 30


def test_run_in_subprocess_can_actually_kill_a_hung_process():
    from backend.core.sandbox import run_in_subprocess, SandboxLimits
    import _sandbox_subprocess_fixtures as fx

    start = time.perf_counter()
    result = run_in_subprocess(fx.infinite_loop, limits=SandboxLimits(timeout_seconds=0.5))
    elapsed = time.perf_counter() - start

    assert result.timed_out is True
    assert elapsed < 3.0, "the process must actually be terminated, not left running to completion"
    assert result.enforcement["timeout"] == "hard-process-kill"


def test_run_in_subprocess_propagates_a_real_exception():
    from backend.core.sandbox import run_in_subprocess
    import _sandbox_subprocess_fixtures as fx

    result = run_in_subprocess(fx.raises)
    assert result.ok is False
    assert "boom from subprocess" in result.error


# ============================================================
# PART 3 — tool_registry.py
# ============================================================
def test_builtin_tools_are_registered_on_import():
    from backend.core.tool_registry import list_tools

    names = {t["name"] for t in list_tools()}
    assert "weather" in names
    assert "web_search" in names


def test_execute_tool_rejects_missing_required_argument():
    from backend.core.tool_registry import execute_tool

    result = execute_tool("weather", {})
    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGS"


def test_execute_tool_rejects_wrong_argument_type():
    from backend.core.tool_registry import execute_tool

    result = execute_tool("weather", {"location": 12345})
    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGS"


def test_execute_tool_enforces_declared_permission():
    from backend.core.tool_registry import execute_tool, PERMISSION_SAFE

    result = execute_tool("weather", {"location": "Richmond"}, allowed_permissions={PERMISSION_SAFE})
    assert result.ok is False
    assert result.error_code == "TOOL_PERMISSION_DENIED"


def test_execute_tool_allows_when_permission_matches():
    """
    Registers a fake safe tool (no real network call) rather than
    exercising the real weather tool's actual HTTP dependency — this
    test is about the registry's permission-gating logic, not whether
    a live weather API happens to be reachable right now.
    """
    from backend.core.tool_registry import execute_tool, register_tool, unregister_tool, ToolSchema, PERMISSION_SAFE

    register_tool(ToolSchema(name="_test_echo", description="echoes back", permission=PERMISSION_SAFE, parameters={"msg": {"type": "string", "required": True}}), lambda msg: msg)
    try:
        result = execute_tool("_test_echo", {"msg": "hello"}, allowed_permissions={PERMISSION_SAFE})
        assert result.ok is True
        assert result.value == "hello"
    finally:
        unregister_tool("_test_echo")


def test_execute_tool_unknown_tool_returns_not_found():
    from backend.core.tool_registry import execute_tool

    result = execute_tool("definitely_not_a_registered_tool")
    assert result.ok is False
    assert result.error_code == "TOOL_NOT_FOUND"


def test_execute_tool_respects_per_tool_timeout():
    from backend.core.tool_registry import execute_tool, register_tool, unregister_tool, ToolSchema, PERMISSION_SAFE

    def slow_handler():
        time.sleep(2)
        return "too slow"

    register_tool(ToolSchema(name="_test_slow", description="always too slow", permission=PERMISSION_SAFE, timeout_seconds=0.2), slow_handler)
    try:
        start = time.perf_counter()
        result = execute_tool("_test_slow", {})
        elapsed = time.perf_counter() - start

        assert result.ok is False
        assert result.error_code == "TOOL_TIMEOUT"
        assert elapsed < 1.0
    finally:
        unregister_tool("_test_slow")


def test_register_tool_rejects_an_unknown_permission():
    from backend.core.tool_registry import register_tool, ToolSchema

    try:
        register_tool(ToolSchema(name="_test_bad_perm", description="x", permission="not-a-real-permission"), lambda: None)
        assert False, "expected a ValueError for an unknown permission level"
    except ValueError:
        pass


# ============================================================
# RUNNER
# ============================================================
def _all_tests():
    return [obj for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]


def main() -> int:
    failures = []
    for test in _all_tests():
        name = test.__name__
        try:
            test()
            print(f"PASS  {name}")
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            failures.append(name)
        except Exception as e:
            print(f"ERROR {name}: {e}")
            traceback.print_exc()
            failures.append(name)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1

    print("All tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
