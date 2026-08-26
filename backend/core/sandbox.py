# backend/core/sandbox.py

"""
Best-effort execution sandbox — CPU/time/memory limits for tool and
plugin execution (Phase 2, items 2 & 8).

Honest scope note: this is a Windows-developed, cross-platform-running
app, and neither `resource.setrlimit` (POSIX-only) nor Job Objects
(the real Windows mechanism for hard memory/CPU caps on a process) are
available from pure Python without a native extension this project
doesn't depend on. So:

  - the TIME limit is real and hard: run_in_sandbox() executes the
    callable on a worker thread and simply stops waiting at the
    timeout, returning a "timed out" SandboxResult. The callable's
    thread is NOT forcibly killed (Python has no safe API to kill an
    arbitrary thread) — it keeps running in the background, but the
    caller is unblocked and never sees output from it past the deadline.
  - the MEMORY limit is a monitor-and-report cap, not a hard kill: a
    watcher polls the current process's RSS via psutil while the
    callable runs and flags a violation if it grows past the budget.
    For first-party tool code (the only thing this sandbox runs today)
    that's sufficient to catch a runaway allocation; it is NOT isolation
    from a genuinely malicious plugin, which would need real process-
    level isolation (a subprocess sandbox) — see run_in_subprocess()
    below for that stronger, slower option when it's actually needed.
  - CPU is reported (time actually spent), not capped — a hard CPU-time
    cap has the same POSIX-only limitation as memory.

Every SandboxResult tells the caller exactly what was and wasn't
enforced, rather than silently pretending a soft cap was a hard one.
"""

from __future__ import annotations

import concurrent.futures
import multiprocessing
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import psutil

from logger import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MEMORY_LIMIT_MB = 512.0
_MEMORY_POLL_INTERVAL_SECONDS = 0.05


@dataclass
class SandboxLimits:
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    memory_limit_mb: float = DEFAULT_MEMORY_LIMIT_MB


@dataclass
class SandboxResult:
    ok: bool
    value: Any = None
    error: Optional[str] = None
    timed_out: bool = False
    memory_exceeded: bool = False
    elapsed_seconds: float = 0.0
    peak_memory_mb: float = 0.0
    enforcement: dict = field(default_factory=lambda: {
        "timeout": "hard",
        "memory": "monitored-not-killed",
        "cpu": "reported-not-capped",
    })


def run_in_sandbox(fn: Callable[..., Any], *args, limits: Optional[SandboxLimits] = None, **kwargs) -> SandboxResult:
    """
    Run fn(*args, **kwargs) on a worker thread with a hard wall-clock
    timeout and best-effort memory monitoring. See module docstring for
    exactly what "hard" and "best-effort" mean here.
    """
    limits = limits or SandboxLimits()
    start = time.perf_counter()

    process = psutil.Process()
    baseline_mb = process.memory_info().rss / (1024 * 1024)
    peak_mb = [baseline_mb]
    memory_exceeded = threading.Event()
    stop_monitor = threading.Event()

    def _monitor():
        while not stop_monitor.is_set():
            try:
                current_mb = process.memory_info().rss / (1024 * 1024)
                peak_mb[0] = max(peak_mb[0], current_mb)
                if current_mb - baseline_mb > limits.memory_limit_mb:
                    memory_exceeded.set()
            except Exception:
                pass
            stop_monitor.wait(_MEMORY_POLL_INTERVAL_SECONDS)

    monitor_thread = threading.Thread(target=_monitor, daemon=True)
    monitor_thread.start()

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn, *args, **kwargs)

    try:
        value = future.result(timeout=limits.timeout_seconds)
        elapsed = time.perf_counter() - start
        stop_monitor.set()
        # executor.shutdown(wait=False) intentionally does not cancel the
        # already-submitted task — nothing to cancel here since it already
        # completed by the time we get this far.
        executor.shutdown(wait=False)
        return SandboxResult(
            ok=not memory_exceeded.is_set(),
            value=value,
            memory_exceeded=memory_exceeded.is_set(),
            elapsed_seconds=elapsed,
            peak_memory_mb=peak_mb[0] - baseline_mb,
            error="Memory budget exceeded during execution" if memory_exceeded.is_set() else None,
        )

    except concurrent.futures.TimeoutError:
        elapsed = time.perf_counter() - start
        stop_monitor.set()
        logger.warning(f"run_in_sandbox() → timed out after {limits.timeout_seconds}s")
        # Deliberately does NOT call future.cancel() / executor.shutdown(wait=True) —
        # see module docstring: the underlying thread cannot be safely
        # force-killed, so this returns control to the caller immediately
        # rather than blocking further on a thread that's already over budget.
        executor.shutdown(wait=False)
        return SandboxResult(
            ok=False, timed_out=True, elapsed_seconds=elapsed,
            peak_memory_mb=peak_mb[0] - baseline_mb,
            error=f"Execution exceeded the {limits.timeout_seconds}s timeout",
        )

    except Exception as e:
        elapsed = time.perf_counter() - start
        stop_monitor.set()
        executor.shutdown(wait=False)
        logger.debug(f"run_in_sandbox() → callable raised: {e}")
        return SandboxResult(
            ok=False, error=str(e), elapsed_seconds=elapsed,
            peak_memory_mb=peak_mb[0] - baseline_mb,
        )


def _subprocess_entry(fn: Callable[..., Any], args: tuple, kwargs: dict, result_queue) -> None:
    try:
        result_queue.put(("ok", fn(*args, **kwargs)))
    except Exception as e:
        result_queue.put(("error", str(e)))


def run_in_subprocess(fn: Callable[..., Any], *args, limits: Optional[SandboxLimits] = None, **kwargs) -> SandboxResult:
    """
    Stronger isolation than run_in_sandbox(): a genuinely separate OS
    process, which CAN be forcibly terminated on timeout (unlike a
    thread) — real isolation for a plugin/tool whose trustworthiness is
    lower than "first-party code we wrote". fn must be picklable
    (module-level, not a closure/lambda) since multiprocessing pickles
    it to hand to the child process.
    """
    limits = limits or SandboxLimits()
    start = time.perf_counter()

    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue()
    proc = ctx.Process(target=_subprocess_entry, args=(fn, args, kwargs, result_queue))
    proc.start()

    peak_mb = 0.0
    try:
        ps_proc = psutil.Process(proc.pid)
    except psutil.NoSuchProcess:
        ps_proc = None

    proc.join(timeout=limits.timeout_seconds)
    elapsed = time.perf_counter() - start

    if proc.is_alive():
        # A real process CAN be killed outright — this is the actual
        # hard-isolation win over run_in_sandbox()'s thread-based timeout.
        proc.terminate()
        proc.join(timeout=1.0)
        logger.warning(f"run_in_subprocess() → timed out after {limits.timeout_seconds}s, process terminated")
        return SandboxResult(
            ok=False, timed_out=True, elapsed_seconds=elapsed, peak_memory_mb=peak_mb,
            error=f"Execution exceeded the {limits.timeout_seconds}s timeout",
            enforcement={"timeout": "hard-process-kill", "memory": "not-monitored", "cpu": "reported-not-capped"},
        )

    if ps_proc is not None:
        try:
            peak_mb = ps_proc.memory_info().rss / (1024 * 1024)
        except psutil.NoSuchProcess:
            pass

    if result_queue.empty():
        return SandboxResult(
            ok=False, elapsed_seconds=elapsed, peak_memory_mb=peak_mb,
            error="Subprocess exited without producing a result",
            enforcement={"timeout": "hard-process-kill", "memory": "not-monitored", "cpu": "reported-not-capped"},
        )

    status, payload = result_queue.get()
    return SandboxResult(
        ok=status == "ok",
        value=payload if status == "ok" else None,
        error=None if status == "ok" else payload,
        elapsed_seconds=elapsed,
        peak_memory_mb=peak_mb,
        enforcement={"timeout": "hard-process-kill", "memory": "not-monitored", "cpu": "reported-not-capped"},
    )
