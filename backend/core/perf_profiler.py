# backend/core/perf_profiler.py

"""
Lightweight in-process performance instrumentation.

This is the "detect slow paths" half of backend performance
profiling/auto-tuning — a bounded ring buffer of {label, elapsed_ms,
ts} samples, recorded via the `timed()` context manager, queryable by
/v1/diagnostics/performance and the model_performance_request /
DIAGNOSTICS_PERFORMANCE_REQUEST IPC handlers.

Deliberately NOT a live auto-tuner that changes thread counts or quant
multipliers on its own: doing that safely requires a real feedback loop
from actual observed inference throughput back into
backend.core.safety_profiles/performance_estimator, which this
codebase doesn't have wired up yet (estimate_speed() is a pre-flight
estimate; nothing today records what a load actually achieved). Rather
than have unverified auto-tuning silently change inference behavior
unattended, this module gives that future feedback loop a real,
tested place to plug in (get_slow_paths()) without guessing at
numbers no one has measured yet.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Deque, Dict, List, Optional

from logger import get_logger

logger = get_logger(__name__)

_MAX_SAMPLES = 2000
_lock = threading.Lock()
_samples: Deque[dict] = deque(maxlen=_MAX_SAMPLES)

# Anything slower than this, on average, across its recorded samples is
# reported as a "slow path" by get_slow_paths()'s default threshold.
DEFAULT_SLOW_THRESHOLD_MS = 250.0


@contextmanager
def timed(label: str):
    """
    with perf_profiler.timed("compatibility_checker.check_requirements"):
        ...

    Never raises on its own and never suppresses the wrapped block's own
    exception — this is instrumentation, not error handling.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        record(label, elapsed_ms)


def record(label: str, elapsed_ms: float) -> None:
    with _lock:
        _samples.append({"label": label, "elapsed_ms": round(elapsed_ms, 3), "ts": time.time()})


def get_samples(label: Optional[str] = None, limit: int = 200) -> List[dict]:
    with _lock:
        items = list(_samples)
    if label:
        items = [s for s in items if s["label"] == label]
    return items[-limit:]


def get_summary() -> Dict[str, dict]:
    """Per-label {count, avg_ms, max_ms, min_ms, last_ms} across every
    sample currently in the ring buffer."""
    with _lock:
        items = list(_samples)

    by_label: Dict[str, List[float]] = {}
    last_by_label: Dict[str, float] = {}
    for s in items:
        by_label.setdefault(s["label"], []).append(s["elapsed_ms"])
        last_by_label[s["label"]] = s["elapsed_ms"]

    summary = {}
    for label, durations in by_label.items():
        summary[label] = {
            "count": len(durations),
            "avg_ms": round(sum(durations) / len(durations), 3),
            "max_ms": round(max(durations), 3),
            "min_ms": round(min(durations), 3),
            "last_ms": round(last_by_label[label], 3),
        }
    return summary


def get_slow_paths(threshold_ms: float = DEFAULT_SLOW_THRESHOLD_MS) -> List[dict]:
    """Labels whose average recorded duration exceeds threshold_ms, worst first."""
    summary = get_summary()
    slow = [
        {"label": label, **stats}
        for label, stats in summary.items()
        if stats["avg_ms"] > threshold_ms
    ]
    slow.sort(key=lambda s: s["avg_ms"], reverse=True)
    return slow


def clear() -> None:
    with _lock:
        _samples.clear()
