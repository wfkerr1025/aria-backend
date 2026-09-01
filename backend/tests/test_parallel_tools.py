"""Running several tools at once, without losing track of any of them.

ONE DOOR, ENTERED CONCURRENTLY
------------------------------
Every tool already goes through tool_registry.execute_tool(), which
validates arguments, checks permissions and applies the sandbox's
limits. Parallel routing widens that door rather than building a
second one, because a second execution path means all three of those
exist twice -- and the copy nobody updated is the one somebody is
using. These tests pin that the widening did not become a bypass.

THE POOLS ARE NOT A PERFORMANCE KNOB
------------------------------------
The families fail differently under fan-out. search is bounded by
network latency, cli by CPU (one Blender is already a whole core), and
ludo by MONEY: "generate 3 models at once" is SIX generations, because
Ludo has no text-to-3D, and all six are charged before any can be
judged bad. Every other family degrades gracefully; that one converts
a mistake into an invoice.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from backend.core import tool_jobs
from backend.core import tool_registry
from backend.core.tool_jobs import ToolJobSet, ToolJobSpec
from backend.websocket import handlers
from backend.websocket.handlers import WebSocketHandler


# ======================================================
# Fixtures
# ======================================================

class Watcher:
    """Records how many handlers were inside at once, per family."""

    def __init__(self):
        self.lock = threading.Lock()
        self.current = {}
        self.peak = {}
        self.calls = []

    def enter(self, family):
        with self.lock:
            self.current[family] = self.current.get(family, 0) + 1
            self.peak[family] = max(self.peak.get(family, 0),
                                    self.current[family])
            self.calls.append(family)

    def leave(self, family):
        with self.lock:
            self.current[family] -= 1


@pytest.fixture
def watcher():
    return Watcher()


@pytest.fixture
def tools(watcher, monkeypatch):
    """Register a slow tool per family, and undo it afterwards."""
    made = []

    def make(name, family, seconds=0.05, fail=False):
        def handler():
            watcher.enter(family)
            try:
                time.sleep(seconds)
                if fail:
                    raise RuntimeError("this tool is broken")
                return f"{name}-ok"
            finally:
                watcher.leave(family)

        tool_registry.register_tool(
            tool_registry.ToolSchema(
                name=name, description="test tool", parameters={},
                timeout_seconds=5.0, family=family),
            handler)
        made.append(name)
        return name

    yield make

    for name in made:
        tool_registry.unregister_tool(name)


@pytest.fixture
def job_set():
    return ToolJobSet()


def _run(coro):
    return asyncio.run(coro)


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        try:
            self.sent.append(json.loads(message)
                             if isinstance(message, str) else message)
        except ValueError:
            self.sent.append({"type": "raw", "message": message})

    async def close(self):
        pass


@pytest.fixture
def handler():
    return WebSocketHandler(FakeWebSocket())


# ======================================================
# They actually run at once
# ======================================================

def test_jobs_actually_run_concurrently(tools, job_set, watcher):
    tools("wide_a", "search")

    async def scenario():
        started = time.monotonic()
        jobs = await job_set.run_all(
            [ToolJobSpec(tool="wide_a") for _ in range(6)])
        return time.monotonic() - started, jobs

    took, jobs = _run(scenario())

    assert all(job.state == tool_jobs.DONE for job in jobs)
    assert watcher.peak["search"] > 1, "nothing overlapped"
    assert took < 0.25, f"six 50ms jobs took {took:.2f}s -- that is serial"


def test_a_family_limit_is_respected(tools, job_set, watcher, monkeypatch):
    monkeypatch.setitem(tool_jobs.FAMILY_LIMITS, "search", 3)
    tools("limited", "search")

    async def scenario():
        return await job_set.run_all(
            [ToolJobSpec(tool="limited") for _ in range(9)])

    _run(scenario())

    assert watcher.peak["search"] <= 3, watcher.peak


def test_cli_work_runs_one_at_a_time(tools, job_set, watcher):
    """One Blender is already a whole core. Two is a machine that
    stops responding."""
    tools("heavy", "cli")

    async def scenario():
        return await job_set.run_all(
            [ToolJobSpec(tool="heavy") for _ in range(4)])

    _run(scenario())

    assert watcher.peak["cli"] == 1, watcher.peak
    assert tool_jobs.FAMILY_LIMITS["cli"] == 1


def test_ludo_never_runs_more_than_two_at_once(tools, job_set, watcher):
    """The money cap, pinned separately from the others because it is
    a different kind of limit."""
    tools("paid", "ludo")

    async def scenario():
        return await job_set.run_all(
            [ToolJobSpec(tool="paid") for _ in range(5)],
        )

    _run(scenario())

    assert watcher.peak["ludo"] <= 2, watcher.peak


def test_families_do_not_block_each_other(tools, job_set, watcher):
    """A queued Blender must not hold up a search."""
    tools("heavy2", "cli", seconds=0.08)
    tools("quick", "search", seconds=0.02)

    async def scenario():
        specs = ([ToolJobSpec(tool="heavy2") for _ in range(3)]
                 + [ToolJobSpec(tool="quick") for _ in range(3)])
        started = time.monotonic()
        await job_set.run_all(specs)
        return time.monotonic() - started

    took = _run(scenario())

    # Three serial cli jobs alone are ~0.24s; the searches ride along.
    assert took < 0.45, f"{took:.2f}s suggests the families shared a queue"
    assert watcher.peak["search"] > 1


# ======================================================
# Money
# ======================================================

def test_fanning_out_beyond_one_ludo_job_needs_confirmation(tools, job_set):
    """Six generations charged before any can be looked at."""
    tools("paid2", "ludo")
    specs = [ToolJobSpec(tool="paid2") for _ in range(3)]

    refusal = tool_jobs.spend_check(specs)

    assert refusal is not None
    assert "3 generations" in refusal
    assert "charged before" in refusal


def test_a_confirmed_fan_out_runs(tools, job_set):
    tools("paid3", "ludo")
    specs = [ToolJobSpec(tool="paid3") for _ in range(3)]

    assert tool_jobs.spend_check(specs, confirmed=True) is None


def test_one_paid_job_needs_no_confirmation(tools, job_set):
    tools("paid4", "ludo")

    assert tool_jobs.spend_check([ToolJobSpec(tool="paid4")]) is None


def test_a_refused_fan_out_spends_nothing(tools, job_set, watcher):
    tools("paid5", "ludo")

    async def scenario():
        return await tool_jobs.route_tools_in_parallel(
            [ToolJobSpec(tool="paid5") for _ in range(4)], job_set=job_set)

    jobs = _run(scenario())

    assert watcher.calls == [], "a refused fan-out called a handler"
    assert len(jobs) == 1
    assert jobs[0].state == tool_jobs.FAILED
    assert jobs[0].result.error_code == "SPEND_NOT_CONFIRMED"


def test_free_families_are_never_gated(tools, job_set):
    """Only money is guarded. Ten searches are ten searches."""
    tools("free", "search")

    assert tool_jobs.spend_check(
        [ToolJobSpec(tool="free") for _ in range(10)]) is None


# ======================================================
# Failure isolation
# ======================================================

def test_one_failing_tool_does_not_lose_the_others(tools, job_set):
    tools("fine", "search")
    tools("broken", "search", fail=True)

    async def scenario():
        return await job_set.run_all([
            ToolJobSpec(tool="fine"), ToolJobSpec(tool="broken"),
            ToolJobSpec(tool="fine"), ToolJobSpec(tool="fine"),
        ])

    jobs = _run(scenario())

    assert len(jobs) == 4
    assert sum(1 for j in jobs if j.state == tool_jobs.DONE) == 3
    assert sum(1 for j in jobs if j.state == tool_jobs.FAILED) == 1
    assert all(job.done for job in jobs)


def test_a_missing_tool_is_a_failed_job_not_an_exception(job_set):
    async def scenario():
        return await job_set.run_all([ToolJobSpec(tool="no_such_tool")])

    jobs = _run(scenario())

    assert jobs[0].state == tool_jobs.FAILED
    assert jobs[0].result is not None


def test_the_result_type_is_the_registry_s_own(tools, job_set):
    """A parallel layer with its own result type would force every
    consumer to handle two."""
    tools("typed", "search")

    async def scenario():
        return await job_set.run_all([ToolJobSpec(tool="typed")])

    jobs = _run(scenario())

    assert isinstance(jobs[0].result, tool_registry.ToolResult)


def test_execution_still_goes_through_the_one_door(tools, job_set, monkeypatch):
    """Permissions, validation and sandbox limits live in
    execute_tool. A parallel path that skipped it would skip all
    three."""
    seen = []
    real = tool_registry.execute_tool
    monkeypatch.setattr(tool_jobs, "execute_tool",
                        lambda name, args=None: seen.append(name) or real(name, args))
    tools("door", "search")

    async def scenario():
        return await job_set.run_all([ToolJobSpec(tool="door")])

    _run(scenario())

    assert seen == ["door"]


# ======================================================
# Stopping
# ======================================================

def test_cancelling_a_queued_job_costs_nothing(tools, job_set, watcher,
                                               monkeypatch):
    """The one clean case, and for a spending family it is the whole
    difference between spending and not."""
    monkeypatch.setitem(tool_jobs.FAMILY_LIMITS, "cli", 1)
    tools("slow_cli", "cli", seconds=0.15)

    async def scenario():
        first = await job_set.submit(ToolJobSpec(tool="slow_cli"))
        queued = await job_set.submit(ToolJobSpec(tool="slow_cli"))
        await asyncio.sleep(0.02)          # first is running, second is not

        assert queued.state == tool_jobs.QUEUED
        await job_set.cancel(queued.job_id)
        await job_set.cancel_all()
        return first, queued

    first, queued = _run(scenario())

    assert queued.state == tool_jobs.CANCELLED_BEFORE_START
    assert watcher.calls.count("cli") == 1, "the queued job ran anyway"


def test_cancel_all_leaves_nothing_running(tools, job_set):
    tools("lingering", "search", seconds=0.5)

    async def scenario():
        for _ in range(4):
            await job_set.submit(ToolJobSpec(tool="lingering"))
        await asyncio.sleep(0.02)
        await asyncio.wait_for(job_set.cancel_all(), timeout=3)
        return job_set.running()

    assert _run(scenario()) == []


def test_a_cancelled_thread_tool_is_not_called_stopped():
    """The tier-honesty test. run_in_sandbox's own docstring says it
    deliberately does not cancel the thread, so the work continues and
    its value is discarded -- claiming otherwise would be the
    half-truth this codebase keeps paying for."""
    said = tool_jobs.cancel_truth("registry")

    assert "finishes on its own" in said
    assert "discarded" in said


def test_a_cancelled_ludo_job_says_the_money_is_gone():
    said = tool_jobs.cancel_truth("ludo")

    assert "not refund" in said


def test_a_cancelled_cli_job_may_say_it_really_stopped():
    """cli_runner owns a timer and kill_tree, so this one is true."""
    said = tool_jobs.cancel_truth("cli")

    assert "killed" in said


def test_a_slow_tool_times_out_alone(tools, job_set):
    """Its siblings must not die with it."""
    tools("dawdler", "search", seconds=0.4)
    tools("prompt", "search", seconds=0.01)

    async def scenario():
        return await job_set.run_all(
            [ToolJobSpec(tool="dawdler"), ToolJobSpec(tool="prompt"),
             ToolJobSpec(tool="prompt")],
            deadline_seconds=0.15)

    jobs = _run(scenario())

    states = [job.state for job in jobs]
    assert states.count(tool_jobs.TIMEOUT) == 1
    assert states.count(tool_jobs.DONE) == 2


# ======================================================
# The connection owns them
# ======================================================

def test_job_packets_are_not_serialised():
    """A job request that queued behind a running turn would start
    only once that turn had finished, which is the opposite of running
    it alongside."""
    assert handlers.JOB_REQUEST_TYPE not in handlers.SERIALISED_TYPES
    assert handlers.JOB_CANCEL_TYPE not in handlers.SERIALISED_TYPES


def test_a_job_request_overtakes_a_running_turn(handler, tools, monkeypatch):
    tools("overtaker", "search", seconds=0.02)
    started = asyncio.Event()

    async def never_ends(_packet):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(handler, "_handle_chat_request", never_ends)

    async def scenario():
        await handler._dispatch({"type": "chat_request"})
        await asyncio.wait_for(started.wait(), timeout=2)

        await handler._dispatch({
            "type": handlers.JOB_REQUEST_TYPE,
            "jobs": [{"tool": "overtaker", "label": "while busy"}]})
        await asyncio.sleep(0.15)

        kinds = [m.get("type") for m in handler.websocket.sent]
        assert "tool_jobs_done" in kinds, kinds

        await handler._end_turn("stopped")
        await handler.wait_for_turns()

    _run(scenario())


def test_context_reset_cancels_the_jobs_it_would_orphan(handler, tools):
    """Resetting the conversation while three of its jobs are still
    writing results into it is the race this design exists to
    prevent."""
    tools("orphan", "search", seconds=0.4)
    reset = []

    async def do_reset(_packet):
        reset.append(len(handler._tool_jobs.running()))

    async def scenario():
        handler._handle_context_reset = do_reset
        for _ in range(3):
            await handler._tool_jobs.submit(ToolJobSpec(tool="orphan"))
        await asyncio.sleep(0.02)
        assert handler._tool_jobs.running(), "nothing was running to orphan"

        await handler._dispatch({"type": "context_reset"})
        await asyncio.wait_for(handler.wait_for_turns(), timeout=3)

    _run(scenario())

    assert reset == [0], "context_reset ran while jobs were still going"


def test_context_reset_is_still_serialised():
    """It must not overtake a turn just because jobs are parallel now."""
    assert "context_reset" in handlers.SERIALISED_TYPES


def test_no_job_outlives_its_connection(handler, tools):
    tools("outliver", "search", seconds=0.5)

    async def scenario():
        for _ in range(3):
            await handler._tool_jobs.submit(ToolJobSpec(tool="outliver"))
        await asyncio.sleep(0.02)
        await asyncio.wait_for(handler._tool_jobs.cancel_all(), timeout=3)
        return handler._tool_jobs.running()

    assert _run(scenario()) == []


def test_the_teardown_cancels_jobs_before_the_heartbeat_stops():
    import inspect
    source = inspect.getsource(WebSocketHandler.handle)

    assert "_tool_jobs.cancel_all()" in source
    assert (source.index("_tool_jobs.cancel_all()")
            < source.index("heartbeat_task.cancel()"))


def test_finished_jobs_are_not_kept_forever(tools, job_set):
    """A connection that ran a thousand tools should not hold a
    thousand records for the life of the socket."""
    tools("transient", "search", seconds=0.01)

    async def scenario():
        await job_set.run_all([ToolJobSpec(tool="transient") for _ in range(3)])
        return job_set.forget_finished(), len(job_set)

    dropped, remaining = _run(scenario())

    assert dropped == 3
    assert remaining == 0


# ======================================================
# Every tool declares where it belongs
# ======================================================

def test_every_registered_tool_declares_a_family():
    """A tool with no family silently lands in the widest pool -- which
    for anything expensive is exactly wrong."""
    tool_registry.register_builtin_tools()

    for tool in tool_registry.list_tools():
        assert tool.get("family"), tool["name"]
        assert tool["family"] in tool_jobs.FAMILY_LIMITS, (
            f"{tool['name']} declares family {tool['family']!r}, "
            f"which has no pool")


def test_the_long_running_tools_are_not_in_a_wide_pool():
    """run_tests takes eleven minutes and the Unity tools drive a
    build. Eight at once is a machine that stops responding."""
    tool_registry.register_builtin_tools()

    by_name = {t["name"]: t for t in tool_registry.list_tools()}
    for name in ("run_tests",):
        assert by_name[name]["family"] == "cli", name
    assert tool_jobs.FAMILY_LIMITS["cli"] <= 2


def test_a_spec_takes_its_family_from_the_schema(tools):
    """The router keeps no second table of which tool is which -- one
    place to be wrong instead of two."""
    tools("declared", "cli")

    assert ToolJobSpec(tool="declared").resolved_family() == "cli"
    assert ToolJobSpec(tool="declared", family="search").resolved_family() == "search"
    assert ToolJobSpec(tool="not_registered").resolved_family() == \
        tool_jobs.DEFAULT_FAMILY
