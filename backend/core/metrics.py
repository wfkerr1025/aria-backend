# backend/core/metrics.py

"""
Metrics + telemetry (local only) — Phase 2, item 6.

Does not introduce a second measurement system: tokens/sec and latency
come from backend.core.perf_profiler's real recorded samples (Phase 1's
streaming_engine.py already records
streaming_engine.stream.{total,time_to_first_token,observed_tokens_per_sec}
per turn); memory/CPU/GPU utilization come from
backend.core.hardware_snapshot_cache + backend.core.resource_monitor.
This module's only job is assembling those already-real numbers into
one snapshot for GET /v1/metrics and a matching IPC packet — nothing
here is telemetry that leaves the machine; "local only" per the spec
means exactly that, there is no network call anywhere in this file.
"""

from __future__ import annotations

from typing import Any, Dict

from . import perf_profiler
from . import hardware_snapshot_cache
from .resource_monitor import get_resource_snapshot

from logger import get_logger

logger = get_logger(__name__)

# Both v1 (backend.core.streaming_engine, live on every real chat entry
# point today) and v2 (backend.core.streaming_engine_v2, new — see that
# module's docstring on why it isn't wired into the live paths yet) —
# whichever one actually ran shows real numbers here; the other's
# fields stay null until it's used at least once.
_STREAMING_LABELS = {
    "v1": (
        "streaming_engine.stream.total",
        "streaming_engine.stream.time_to_first_token",
        "streaming_engine.stream.observed_tokens_per_sec",
    ),
    "v2": (
        "streaming_engine_v2.stream.total",
        "streaming_engine_v2.stream.time_to_first_token",
        "streaming_engine_v2.stream.observed_tokens_per_sec",
    ),
}


def get_streaming_metrics() -> Dict[str, Any]:
    summary = perf_profiler.get_summary()
    result: Dict[str, Any] = {}
    for engine_version, labels in _STREAMING_LABELS.items():
        for label in labels:
            key = f"{engine_version}_{label.rsplit('.', 1)[-1]}"
            result[key] = summary.get(label)
    return result


def get_resource_metrics() -> Dict[str, Any]:
    snapshot = get_resource_snapshot()
    gpu = hardware_snapshot_cache.get_gpu()
    return {
        "cpu_usage_pct": snapshot.cpu_usage,
        "ram_used_gb": round(snapshot.ram_used_gb, 2),
        "ram_total_gb": round(snapshot.ram_total_gb, 2),
        "ram_used_pct": round(snapshot.ram_used_pct, 2),
        "vram_used_gb": round(snapshot.vram_used_gb, 2),
        "vram_total_gb": round(snapshot.vram_total_gb, 2),
        "vram_used_pct": round(snapshot.vram_used_pct, 2),
        "gpu_name": gpu["name"] if gpu else None,
    }


def get_metrics_snapshot() -> Dict[str, Any]:
    """
    The full /v1/metrics payload: tokens/sec + latency (from real
    observed streaming turns, when any have happened this run — null
    fields until the first one), throughput/perf-path summary (every
    perf_profiler-instrumented function, not just streaming), and live
    memory/CPU/GPU utilization.
    """
    return {
        "streaming": get_streaming_metrics(),
        "resources": get_resource_metrics(),
        "instrumented_functions": perf_profiler.get_summary(),
    }
