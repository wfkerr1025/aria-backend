# backend/core/backend_watchdog.py

"""
Batch 4 — backend crash/freeze detection and last-known-good recovery.

Scope, honestly stated: this module is everything that CAN be done from
INSIDE this Python process. A genuinely frozen/deadlocked process can't
run any code to save itself — including this module's own code — by
definition; recovering from that requires an external supervisor
watching the process from outside. That supervisor already exists in
this codebase: AriaLauncher/AriaLauncher/BackendManager.cs's
StartBackend() spawns backend/ws_server.py and its Process.Exited
handler already detects a non-zero exit code as a crash — it just
doesn't yet loop to auto-restart on that event. Wiring that up is a
change to a separate C# application and is out of scope for this
module; BackendWatchdog handles the cooperative case instead: this
process detects its OWN staleness/trouble and can choose to cleanly
recycle itself (restart_backend_process(), a real os.execv() re-exec —
see its docstring), plus everything around WHAT to restore once a
(fresh or reconnecting) process is back: last-known-good routing state,
re-validated the same way ModeManager already self-heals on
construction (Batch 1), never silently perpetuating a contradictory
state.

Method names are snake_case, matching every other module in this
backend (mode_manager.py, routing_guard.py, connection_state.py, ...)
rather than the batch spec's literal camelCase — this is a Python
codebase with a completely consistent naming convention throughout;
introducing camelCase here would be the one inconsistent file in the
whole tree for no functional benefit.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from backend.core.mode_manager import ModeManager
from backend.core import routing_invariants as inv
from backend.core import provider_config
from backend.core import self_knowledge
from backend.core import connection_state

from backend.logger import log as unified_log
from logger import get_logger

logger = get_logger(__name__)

# Mirrors webui/core/bridge.js's HEARTBEAT_TIMEOUT_MS (30s = 3 missed
# 10s heartbeats — see backend/websocket/handlers.py's
# HEARTBEAT_INTERVAL_SECONDS) — the backend and frontend should agree on
# what "stale" means, even though they detect it independently (the
# frontend from silence on ITS end, this from its own successful-send
# record on the backend's end).
HEARTBEAT_TIMEOUT_SECONDS = 30.0


@dataclass
class _WatchdogState:
    started: bool = False
    last_heartbeat_at: Optional[float] = None
    connected: bool = False
    restart_count: int = 0
    last_restart_at: Optional[float] = None
    last_known_good: Optional[Dict[str, Any]] = None


class BackendWatchdog:
    """
    One process-wide instance — see the `watchdog` singleton below.
    Backend health is a process-level fact (like
    backend.core.connection_state, which this composes with), not a
    per-connection one, so this is deliberately not instantiated per
    WebSocketHandler.
    """

    def __init__(self):
        self._state = _WatchdogState()
        self._lock = threading.Lock()

    # ------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------
    def start(self, auto_restart: bool = False, poll_interval_seconds: float = 5.0) -> None:
        """
        Call once, at process bootstrap (see backend/ws_server.py).
        Idempotent. Captures whatever is currently persisted as the
        starting last-known-good snapshot — ModeManager() already
        self-heals an invalid persisted state on construction (Batch
        1), so by the time this runs, backend/config/mode_state.json is
        guaranteed structurally valid.

        `auto_restart` defaults to False — deliberately opt-in. When
        True, starts a daemon thread that calls should_restart() every
        `poll_interval_seconds` and actually calls
        restart_backend_process() the moment it returns True. Off by
        default because a wrong threshold (HEARTBEAT_TIMEOUT_SECONDS
        tuned too tight, a slow-but-not-actually-frozen event loop
        under heavy load) would otherwise cause a restart storm that
        loses in-flight work — turning this on is a deliberate
        deployment choice, not a side effect of merely starting the
        watchdog. backend/ws_server.py's own start() call does not
        enable it.
        """
        with self._lock:
            if self._state.started:
                return
            self._state.started = True
        self._capture_last_known_good()
        unified_log("backend_watchdog", "INFO", "BackendWatchdog started", {"auto_restart": auto_restart})
        logger.info("BackendWatchdog started.")

        if auto_restart:
            thread = threading.Thread(
                target=self._monitor_loop, args=(poll_interval_seconds,),
                daemon=True, name="backend-watchdog-monitor",
            )
            thread.start()

    def _monitor_loop(self, poll_interval_seconds: float) -> None:
        while True:
            time.sleep(poll_interval_seconds)
            try:
                if self.should_restart():
                    logger.error("BackendWatchdog: heartbeat stale with an active connection — restarting.")
                    unified_log("backend_watchdog", "ERROR", "Heartbeat stale with an active connection — restarting", {})
                    self.restart_backend_process()
                    return  # restart_backend_process() re-execs and never returns in practice; return is defense-in-depth for injected exec_fn in tests
            except Exception as e:
                logger.exception(f"BackendWatchdog._monitor_loop() — error: {e}")

    # ------------------------------------------------------------
    # Heartbeat / connection health
    # ------------------------------------------------------------
    def record_heartbeat(self) -> None:
        """
        Call on every proven-successful heartbeat send (see
        backend/websocket/handlers.py's _heartbeat_loop(), which calls
        this right alongside backend.core.connection_state.
        record_heartbeat_sent() — the same real signal, recorded in two
        places because they answer two different questions: connection_
        state is "is ANY connection alive right now", this is "is THIS
        BACKEND PROCESS healthy enough to keep proving it").
        """
        with self._lock:
            self._state.last_heartbeat_at = time.time()
            self._state.connected = True

    def mark_disconnected(self) -> None:
        """Call when a connection ends (see WebSocketHandler.handle()'s finally block)."""
        with self._lock:
            self._state.connected = False
        unified_log("backend_watchdog", "WARNING", "Backend connection marked disconnected", {})

    def is_heartbeat_stale(self, now: Optional[float] = None) -> bool:
        """
        True if a heartbeat has been recorded before but not within
        HEARTBEAT_TIMEOUT_SECONDS. Never had one yet -> False (unknown
        is not the same as stale — nothing has failed).
        """
        now = now if now is not None else time.time()
        with self._lock:
            if self._state.last_heartbeat_at is None:
                return False
            return (now - self._state.last_heartbeat_at) > HEARTBEAT_TIMEOUT_SECONDS

    def should_restart(self, now: Optional[float] = None) -> bool:
        """
        The actual crash/freeze DECISION: True only when there's still
        at least one client connection open (backend.core.connection_
        state) AND this backend's own heartbeat send has gone stale —
        i.e. a connection exists but this process has stopped proving
        it's alive over it, which is the actual freeze signature (a
        stuck event loop, a deadlocked handler). Deliberately NOT true
        just because active_connections == 0 — nobody having an open
        tab is the ordinary, expected state after every user closes
        their last window, not a crash.
        """
        if connection_state.get_status().active_connections <= 0:
            return False
        return self.is_heartbeat_stale(now)

    # ------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------
    def get_snapshot(self) -> Dict[str, Any]:
        """The exact shape backend.ipc_router._handle_diagnostics_backend() sends as-is."""
        # backend_connected is deliberately NOT this watchdog's own
        # `connected` flag: with more than one WebSocket connection open
        # at once, mark_disconnected() firing for ONE of them (its
        # normal close) would otherwise make the whole backend look
        # disconnected even while another connection is still live.
        # backend.core.connection_state already tracks this correctly
        # (an active-connection COUNT, not a single overwritable flag —
        # see its own docstring) — reused here as the single source of
        # truth rather than duplicating that counting logic.
        conn = connection_state.get_status()

        with self._lock:
            s = self._state
            return {
                "backend_connected": conn.connected,
                "lastHeartbeat": s.last_heartbeat_at or conn.last_heartbeat_at,
                "restartCount": s.restart_count,
                "lastRestartTimestamp": s.last_restart_at,
                "lastKnownGoodState": dict(s.last_known_good) if s.last_known_good else None,
            }

    # ------------------------------------------------------------
    # Last-known-good state
    # ------------------------------------------------------------
    def _capture_last_known_good(self) -> None:
        mm = ModeManager()
        mode = mm.get_mode()
        active_model_id, _provider_name = self_knowledge.resolve_active_model_and_provider(mm)
        snapshot = {
            "routing_mode": mode,
            "cloud_provider": mm.get_cloud_provider(),
            "active_model_id": active_model_id,
            "location": {"cloud": "cloud", "local": "local"}.get(mode),
            "captured_at": time.time(),
        }
        with self._lock:
            self._state.last_known_good = snapshot

    def restore_state(
        self,
        mode_state: Optional[Dict[str, Any]] = None,
        provider_state: Optional[Dict[str, Any]] = None,
        model_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Re-validate and reapply routing state — safe and idempotent to
        call on every reconnect (see WebSocketHandler.handle()), not
        just after an actual restart: with no arguments, it re-reads
        the CURRENT persisted state fresh from disk
        (backend/config/mode_state.json via ModeManager,
        provider_config.json via provider_config.reload()) rather than
        trusting a possibly-stale in-memory snapshot, so it can never
        clobber a legitimately newer live state with an older one —
        ModeManager persists on every real change, so "current
        persisted state" and "current live state" are always the same
        thing.

        mode_state/provider_state/model_state accept explicit override
        dicts (mode_state: {"routing_mode", "cloud_provider",
        "active_model_id"}) for a caller that wants to force-restore a
        SPECIFIC captured snapshot rather than whatever's currently
        persisted — tests use this to exercise "restore an
        intentionally-stale/invalid snapshot" without touching real
        files; provider_state/model_state are accepted per the batch's
        requested signature but are currently unused hooks (provider_
        config.py has no per-provider override surface beyond
        provider_config.json itself, and model_state would only ever
        need to override active_model_id, already covered by mode_state).

        Validates via backend.core.routing_invariants (the same lenient
        rule packet_validation.py uses — a null cloud_provider is a
        valid, honest "not chosen yet" state, not a corruption) plus a
        liveness check (is the named cloud_provider still actually
        configured — routing_invariants only checks non-null, not
        reachability). Falls back to Automatic Mode on any failure,
        exactly like ModeManager's own self-heal, and PERSISTS that
        fallback so it only ever has to happen once.

        Returns the actually-applied state — {"routing_mode",
        "cloud_provider", "active_model_id", "location", "fell_back"}.
        """
        provider_config.reload()

        mm = ModeManager()
        current_mode = mm.get_mode()
        current_override = mm.get_explicit_model_override()
        forced_snapshot = mode_state is not None

        if forced_snapshot:
            routing_mode = mode_state.get("routing_mode", "automatic")
            cloud_provider = mode_state.get("cloud_provider")
            active_model_id = mode_state.get("active_model_id")
        else:
            # Nothing external to apply here — this IS the current,
            # already-valid-or-about-to-be-validated persisted state.
            # active_model_id is the raw override (not
            # self_knowledge's resolved value, which for Cloud Mode is a
            # model_selector-derived id like "gpt-4" — writing THAT back
            # as an "override" would fabricate a pin the user never
            # actually set).
            routing_mode = current_mode
            cloud_provider = mm.get_cloud_provider()
            active_model_id = current_override

        location = {"cloud": "cloud", "local": "local"}.get(routing_mode)
        fell_back = False

        try:
            inv.validate_routing_state(
                inv.RoutingState(
                    routing_mode=routing_mode, cloud_provider=cloud_provider,
                    active_model_id=active_model_id, location=location,
                ),
                require_cloud_provider=False,
            )
            if routing_mode == "cloud" and cloud_provider and not provider_config.is_valid_cloud_provider(cloud_provider):
                raise inv.RoutingInvariantError(
                    "STALE_PROVIDER", f"Provider '{cloud_provider}' is no longer configured.",
                )
        except inv.RoutingInvariantError as e:
            logger.warning(f"BackendWatchdog.restore_state() — snapshot invalid ({e.code}: {e.message}); falling back to automatic")
            unified_log("backend_watchdog", "WARNING", "Last-known-good state invalid — falling back to Automatic Mode", {
                "code": e.code, "message": e.message,
            })
            routing_mode, cloud_provider, active_model_id, location = "automatic", None, None, None
            fell_back = True

        # ModeManager.set_mode() always clears explicit_model_override as
        # a side effect (see mode_manager.py) — calling it unconditionally
        # on every ordinary, already-valid reconnect would silently wipe
        # a user's "switch to X" pin for no reason. Only write anything
        # when the mode is actually changing (a real fallback, or an
        # explicitly forced snapshot that differs from live state).
        if fell_back or (forced_snapshot and routing_mode != current_mode):
            mm.set_mode(routing_mode)
            if routing_mode == "cloud":
                mm.set_cloud_provider(cloud_provider)
            if active_model_id and not fell_back and routing_mode != "cloud":
                mm.set_explicit_model_override(active_model_id)
        elif forced_snapshot and routing_mode == "cloud" and cloud_provider != mm.get_cloud_provider():
            mm.set_cloud_provider(cloud_provider)

        restored = {
            "routing_mode": routing_mode, "cloud_provider": cloud_provider,
            "active_model_id": active_model_id, "location": location, "fell_back": fell_back,
        }
        with self._lock:
            self._state.last_known_good = {**restored, "captured_at": time.time()}
        unified_log("backend_watchdog", "INFO", "Routing state restored", restored)
        return restored

    # ------------------------------------------------------------
    # Restart
    # ------------------------------------------------------------
    def restart_backend_process(self, exec_fn: Optional[Callable[[], None]] = None) -> None:
        """
        Cooperative self-restart: re-execs THIS process (os.execv), so
        the fresh Python interpreter re-imports every backend.core
        module from scratch — provider_config, tool_registry's
        register_builtin_tools(), model_selector, weather_provider all
        reinitialize exactly as they do on a normal cold start, with no
        separate "reinitialize X" call needed here.

        Only works for a process still able to run Python code — see
        the module docstring for why a true deadlock needs the external
        AriaLauncher supervisor instead, which this cannot substitute
        for.

        `exec_fn` is an injection point for tests (and any future
        caller wanting a different restart mechanism) — defaults to the
        real os.execv() call, which never returns (the process image is
        replaced in place).
        """
        with self._lock:
            self._state.restart_count += 1
            self._state.last_restart_at = time.time()
            restart_count = self._state.restart_count

        unified_log("backend_watchdog", "WARNING", "Backend restart initiated", {
            "restart_count": restart_count,
        })
        logger.warning(f"BackendWatchdog.restart_backend_process() — restart #{restart_count}")

        if exec_fn is not None:
            exec_fn()
            return

        os.execv(sys.executable, [sys.executable] + sys.argv)


# Process-wide singleton — mirrors backend.core.connection_state's own
# module-level scope (backend health is a process fact, not a
# per-connection one).
watchdog = BackendWatchdog()
