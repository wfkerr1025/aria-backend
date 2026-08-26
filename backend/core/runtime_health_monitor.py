# backend/core/runtime_health_monitor.py

"""
Idle/background resource + thermal poller -- one instance per active
WebSocket connection (backend/websocket/handlers.py), independent of
whether a chat turn is in flight.

This is what makes the warning system's critical conditions ("CPU >95%
sustained", "RAM >90%", thermal throttling -- see backend.core.
warning_manager) detectable even while the user is just sitting on the
chat screen with nothing generating. backend.core.cpu_load_monitor.
CPULoadMonitor is the wrong tool for that: it's explicitly scoped to
"one instance per active inference session" and its docstring says so
directly -- it starts when a CPU-only generation begins and stops the
instant it ends, so a resource spike between messages (or with no chat
running at all) would never register there. This module exists
specifically to cover that idle gap; it is not a replacement for
CPULoadMonitor's own tight, high-frequency, in-generation polling.

Thermal detection is polled far less often than CPU/RAM: hardware_
detector.detect_thermal()'s Windows path shells out to `wmic`, which is
too heavyweight to run every couple of seconds per open connection.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from . import hardware_detector
from .resource_monitor import get_resource_snapshot, ResourceSnapshot

from logger import get_logger

logger = get_logger(__name__)

CPU_RAM_POLL_INTERVAL_SECONDS = 2.0
THERMAL_POLL_INTERVAL_SECONDS = 30.0

# "Sustained" per the warning spec's CPU condition -- a single
# instantaneous spike must not itself trigger the critical warning; CPU
# has to stay at/above the critical line for this long first. Mirrors
# the spirit of auto_balancer.py's own duration-gated tiers
# (THREADS_ENTER_AFTER_SECONDS et al.) rather than inventing an
# unrelated debounce scheme.
CPU_SUSTAINED_SECONDS = 5.0
CPU_SUSTAINED_THRESHOLD_PCT = 95.0

_UNSUPPORTED_THERMAL = hardware_detector.classify_thermal(supported=False, throttling_detected=False, max_temp_c=None)


class RuntimeHealthMonitor:
    """One per WebSocket connection: start() on connect, stop() on
    disconnect (see backend/websocket/handlers.py)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._thermal_thread: Optional[threading.Thread] = None

        self._latest_snapshot: Optional[ResourceSnapshot] = None
        self._latest_thermal: dict = _UNSUPPORTED_THERMAL
        self._above_critical_cpu_since: Optional[float] = None
        self._cpu_sustained_critical = False

    def start(self) -> None:
        if self._poll_thread is not None and self._poll_thread.is_alive():
            return
        self._stop_event.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True, name="runtime-health-monitor")
        self._poll_thread.start()
        self._thermal_thread = threading.Thread(target=self._thermal_loop, daemon=True, name="runtime-health-monitor-thermal")
        self._thermal_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=CPU_RAM_POLL_INTERVAL_SECONDS * 2)
        self._poll_thread = None
        # Deliberately not joined -- it may be mid-`wmic` subprocess
        # call (up to its own 3s timeout); it's a daemon thread, so
        # process/interpreter exit still can't hang on it.
        self._thermal_thread = None

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                snapshot = get_resource_snapshot()
                now = time.time()
                with self._lock:
                    self._latest_snapshot = snapshot
                    if snapshot.cpu_usage >= CPU_SUSTAINED_THRESHOLD_PCT:
                        self._above_critical_cpu_since = self._above_critical_cpu_since or now
                    else:
                        self._above_critical_cpu_since = None
                    self._cpu_sustained_critical = (
                        self._above_critical_cpu_since is not None
                        and (now - self._above_critical_cpu_since) >= CPU_SUSTAINED_SECONDS
                    )
            except Exception as e:
                logger.debug(f"RuntimeHealthMonitor._poll_loop() → snapshot failed: {e}")
            self._stop_event.wait(CPU_RAM_POLL_INTERVAL_SECONDS)

    def _thermal_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                thermal = hardware_detector.detect_thermal()
                with self._lock:
                    self._latest_thermal = thermal
            except Exception as e:
                logger.debug(f"RuntimeHealthMonitor._thermal_loop() → detect_thermal failed: {e}")
            self._stop_event.wait(THERMAL_POLL_INTERVAL_SECONDS)

    def get_snapshot(self) -> Optional[ResourceSnapshot]:
        with self._lock:
            return self._latest_snapshot

    def get_thermal(self) -> dict:
        with self._lock:
            return self._latest_thermal

    def is_cpu_sustained_critical(self) -> bool:
        with self._lock:
            return self._cpu_sustained_critical
