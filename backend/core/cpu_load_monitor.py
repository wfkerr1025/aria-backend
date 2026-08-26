# backend/core/cpu_load_monitor.py

"""
Lightweight CPU load monitor for the auto-balancer
(backend.core.auto_balancer) — polled at 100-250ms while any eligible
local CPU-only inference is actively running, not continuously in the
background.

Deliberately separate from backend.core.hardware_snapshot_cache (which
caches CPU *identity* — physical cores, AVX tier — for up to 5 minutes,
the wrong tool for a value that needs to be current within a quarter of
a second) and from backend.core.resource_monitor.get_resource_snapshot()
(which also probes VRAM/GPU/Unity-process-detection on every call —
unnecessary work for a tight poll loop that only cares about one
number). This wraps psutil.cpu_percent() directly, the same real data
source those other modules ultimately read from.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import psutil

from logger import get_logger

logger = get_logger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 0.15  # within the requested 100-250ms range


class CPULoadMonitor:
    """
    One instance per active inference session (created/started by
    backend.core.auto_balancer.AutoBalancer, not shared globally) — a
    global always-on poller would keep a background thread alive for
    the whole process lifetime for a number nothing needs outside an
    active eligible generation.
    """

    def __init__(self, poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS):
        self.poll_interval = poll_interval
        self._current_cpu: float = 0.0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        # Seed with a real (blocking) reading so get_current_cpu() never
        # returns a meaningless 0.0 before the first poll tick lands.
        self._current_cpu = psutil.cpu_percent(interval=None)
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="cpu-load-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_interval * 3)
        self._thread = None

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                # interval=None → non-blocking, compares against the
                # last call (either the seed reading in start() or the
                # previous loop iteration) — this is what makes 100-250ms
                # polling actually cheap instead of each tick blocking
                # for its own measurement window.
                value = psutil.cpu_percent(interval=None)
                with self._lock:
                    self._current_cpu = value
            except Exception as e:
                logger.debug(f"CPULoadMonitor._poll_loop() → psutil read failed: {e}")
            self._stop_event.wait(self.poll_interval)

    def get_current_cpu(self) -> float:
        with self._lock:
            return self._current_cpu

    def __enter__(self) -> "CPULoadMonitor":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()
