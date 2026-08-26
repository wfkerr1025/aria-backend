# backend/core/hardware_snapshot_cache.py

"""
TTL-cached wrapper around backend.core.hardware_detector.

Root cause of the "15-second model card stall": backend.core.model_manager
.list_models() (called by every models_list_request / GET /v1/models) and
backend.core.compatibility_checker.check_requirements() /
backend.core.performance_estimator.estimate_speed() (called once per
model, per request) each independently called hardware_detector.detect_cpu()
/ detect_ram() / detect_gpu() / detect_storage() fresh, every single call —
including detect_storage()'s real local read/write benchmark. None of
that data changes between one request and the next half-second later; the
CPU doesn't change generation, the GPU doesn't change VRAM, and a spinning
disk's sequential throughput doesn't change from one Models-page click to
the next.

This module is the single place those four detectors get called from now.
Static-ish facts (CPU/GPU/storage) are cached with a long TTL; RAM gets a
much shorter one since "available"/"pressure" genuinely changes as other
processes run. Callers ask for a snapshot; a cache hit is a dict lookup, a
miss re-probes (mirroring, and instrumented by, backend.core.perf_profiler
so a systemic slowdown here shows up in /v1/diagnostics/performance instead
of silently degrading every model card load again).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

from . import hardware_detector
from . import perf_profiler

from logger import get_logger

logger = get_logger(__name__)

# CPU/GPU/storage identity essentially never changes mid-session (no
# hot-swappable CPUs; a hot-plugged GPU or newly-mounted drive is a rare
# edge case a manual refresh() call — or just restarting the app —
# covers). RAM's "available"/"pressure" fields, by contrast, genuinely
# move from one request to the next, so that one gets a short TTL instead
# of being lumped in with the others.
_TTL_SECONDS = {
    "cpu": 300.0,
    "ram": 15.0,
    "gpu": 300.0,
    "storage": 300.0,
}

_lock = threading.Lock()
_cache: Dict[str, Dict[str, Any]] = {}  # key -> {"value": ..., "expires_at": float}


def _get_cached(key: str, ttl: float, compute: Callable[[], Any]) -> Any:
    now = time.monotonic()

    with _lock:
        entry = _cache.get(key)
        if entry is not None and entry["expires_at"] > now:
            return entry["value"]

    with perf_profiler.timed(f"hardware_snapshot_cache.{key}_miss"):
        value = compute()

    with _lock:
        _cache[key] = {"value": value, "expires_at": now + ttl}

    return value


def get_cpu() -> dict:
    return _get_cached("cpu", _TTL_SECONDS["cpu"], hardware_detector.detect_cpu)


def get_ram() -> dict:
    return _get_cached("ram", _TTL_SECONDS["ram"], hardware_detector.detect_ram)


def get_gpu() -> Optional[dict]:
    return _get_cached("gpu", _TTL_SECONDS["gpu"], hardware_detector.detect_gpu)


def get_storage(path: Optional[str] = None) -> dict:
    # Keyed by path too — a 405B-tier model's storage check and a generic
    # "system storage" diagnostics call may reasonably target different
    # directories, and each deserves its own cached benchmark rather than
    # one clobbering the other.
    key = f"storage:{path or ''}"
    return _get_cached(key, _TTL_SECONDS["storage"], lambda: hardware_detector.detect_storage(path))


def get_all(storage_path: Optional[str] = None) -> dict:
    return {
        "cpu": get_cpu(),
        "ram": get_ram(),
        "gpu": get_gpu(),
        "storage": get_storage(storage_path),
    }


def refresh() -> dict:
    """Force-invalidate everything and re-probe immediately. Used by the
    startup pipeline (so the very first request gets a warm cache instead
    of paying the first-probe cost) and by /v1/diagnostics/hardware?refresh=1."""
    with _lock:
        _cache.clear()
    logger.debug("hardware_snapshot_cache.refresh() — cache cleared, re-probing")
    return get_all()


def cache_state() -> dict:
    """For /v1/diagnostics/hardware — what's cached and how fresh it is,
    without forcing a re-probe."""
    now = time.monotonic()
    with _lock:
        return {
            key: {
                "cached": True,
                "age_seconds": round(max(0.0, now - (entry["expires_at"] - _TTL_SECONDS.get(key.split(":")[0], 0))), 2),
                "expires_in_seconds": round(max(0.0, entry["expires_at"] - now), 2),
            }
            for key, entry in _cache.items()
        }
