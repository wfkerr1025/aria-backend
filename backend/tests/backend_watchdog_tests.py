# backend/tests/backend_watchdog_tests.py
#
# Regression tests for Batch 4 — backend crash/freeze detection,
# cooperative restart, and last-known-good state restoration
# (backend.core.backend_watchdog), integrated with:
#   - backend.core.connection_state (Batch 3) for the actual
#     multi-connection-aware "is anything connected" truth
#   - backend.core.routing_invariants / mode_manager / provider_config
#     (Batch 1/2) for state validation and fallback
#   - backend/websocket/handlers.py's WebSocketHandler.handle(), which
#     now pushes fresh mode_status_result / diagnostics_backend_result
#     to every newly (re)connected client
#
# Self-contained, plain-assert tests, matching backend/tests/model_switching_tests.py
# and backend/tests/weather_and_tools_truth_tests.py — not pytest. Run directly:
#
#   python backend/tests/backend_watchdog_tests.py

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class FakeWebSocket:
    """
    Minimal async-iterable fake matching what backend.websocket.handlers.
    WebSocketHandler.handle() actually needs: `async for raw in
    self.websocket` and `await self.websocket.send(raw)`. `packets` is a
    list of already-JSON-encoded strings (or plain dicts, auto-encoded)
    fed to the handler one at a time; the loop ends (as a real
    connection closing would) once they're exhausted.
    """

    def __init__(self, packets=None):
        self.sent = []
        self._packets = packets or []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for p in self._packets:
            yield p if isinstance(p, str) else json.dumps(p)


def _run(coro):
    return asyncio.run(coro)


def _with_mode_manager(fn):
    from backend.core.mode_manager import ModeManager

    mm = ModeManager()
    original_mode = mm.get_mode()
    original_cloud_provider = mm.get_cloud_provider()
    original_override = mm.get_explicit_model_override()
    try:
        return fn(mm)
    finally:
        mm.set_explicit_model_override(original_override)
        mm.set_cloud_provider(original_cloud_provider)
        mm.set_mode(original_mode)


def _fresh_watchdog():
    """A brand-new BackendWatchdog instance, isolated from the real
    process-wide singleton (backend.core.backend_watchdog.watchdog) —
    every test gets its own, so none of these tests can leak state into
    each other or into the real singleton other tests/production code
    reads."""
    from backend.core.backend_watchdog import BackendWatchdog
    return BackendWatchdog()


# ============================================================
# PART 1 — missing heartbeat -> backend marked disconnected / stale
# ============================================================
def test_heartbeat_not_stale_before_any_heartbeat_recorded():
    wd = _fresh_watchdog()
    assert wd.is_heartbeat_stale() is False, "no heartbeat yet is 'unknown', not 'stale'"


def test_heartbeat_stale_after_timeout_elapses():
    from backend.core import backend_watchdog as bw_mod

    wd = _fresh_watchdog()
    wd.record_heartbeat()
    assert wd.is_heartbeat_stale() is False, "just recorded — must not be stale yet"

    future = wd.get_snapshot()["lastHeartbeat"] + bw_mod.HEARTBEAT_TIMEOUT_SECONDS + 1
    assert wd.is_heartbeat_stale(now=future) is True


def test_mark_disconnected_is_recorded():
    wd = _fresh_watchdog()
    wd.record_heartbeat()
    wd.mark_disconnected()
    # mark_disconnected() records the event; get_snapshot()'s
    # backend_connected field deliberately reads from
    # backend.core.connection_state instead (see get_snapshot()'s own
    # comment — a multi-connection-aware source of truth), so assert
    # against the watchdog's OWN internal record via the private state
    # rather than get_snapshot() here.
    assert wd._state.connected is False


# ============================================================
# PART 2 — should_restart() decision logic
# ============================================================
def test_should_restart_false_with_no_active_connections():
    from backend.core import connection_state

    wd = _fresh_watchdog()
    wd.record_heartbeat()
    future = wd.get_snapshot()["lastHeartbeat"] + 9999
    # No connection_state.connection_opened() call — zero active
    # connections is the ordinary "nobody has a window open" state, not
    # a crash, even with a long-stale heartbeat timestamp sitting around.
    assert connection_state.get_status().active_connections == 0
    assert wd.should_restart(now=future) is False


def test_should_restart_true_with_active_connection_and_stale_heartbeat():
    from backend.core import connection_state

    wd = _fresh_watchdog()
    wd.record_heartbeat()
    connection_state.connection_opened()
    try:
        future = wd.get_snapshot()["lastHeartbeat"] + 9999
        assert wd.should_restart(now=future) is True
    finally:
        connection_state.connection_closed()


def test_should_restart_false_with_active_connection_and_fresh_heartbeat():
    from backend.core import connection_state

    wd = _fresh_watchdog()
    wd.record_heartbeat()
    connection_state.connection_opened()
    try:
        assert wd.should_restart() is False
    finally:
        connection_state.connection_closed()


# ============================================================
# PART 3 — restart_backend_process() — decision -> action, injectable
# ============================================================
def test_restart_backend_process_calls_injected_exec_fn_and_counts_it():
    wd = _fresh_watchdog()
    calls = {"n": 0}

    wd.restart_backend_process(exec_fn=lambda: calls.__setitem__("n", calls["n"] + 1))

    assert calls["n"] == 1
    snapshot = wd.get_snapshot()
    assert snapshot["restartCount"] == 1
    assert snapshot["lastRestartTimestamp"] is not None


def test_restart_count_accumulates_across_multiple_restarts():
    wd = _fresh_watchdog()
    for _ in range(3):
        wd.restart_backend_process(exec_fn=lambda: None)
    assert wd.get_snapshot()["restartCount"] == 3


# ============================================================
# PART 4 — last-known-good state restoration
# ============================================================
def test_restore_state_no_args_is_idempotent_and_preserves_override():
    """
    Calling restore_state() with no arguments must never clobber a
    legitimately-set explicit_model_override — it re-reads and
    re-validates the CURRENT persisted state, it doesn't overwrite it
    with something older (see the method's own docstring on why
    ModeManager.set_mode()'s override-clearing side effect makes this
    load-bearing, not incidental).
    """
    from backend.core.model_registry import get_default_model_id

    def scenario(mm):
        wd = _fresh_watchdog()
        local_id = get_default_model_id()
        mm.set_mode("local")
        mm.set_explicit_model_override(local_id)

        result = wd.restore_state()

        assert result["fell_back"] is False
        assert result["routing_mode"] == "local"
        from backend.core.mode_manager import ModeManager
        assert ModeManager().get_explicit_model_override() == local_id, "no-args restore must not clear a valid override"

    _with_mode_manager(scenario)


def test_restore_state_falls_back_to_automatic_on_invalid_forced_snapshot():
    """A local model_id forced under routing_mode='cloud' is structurally
    impossible (backend.core.routing_invariants) — restore_state() must
    fall back to Automatic Mode and persist that correction."""
    from backend.core.model_registry import get_default_model_id
    from backend.core.mode_manager import ModeManager

    def scenario(mm):
        wd = _fresh_watchdog()
        local_id = get_default_model_id()

        result = wd.restore_state(mode_state={
            "routing_mode": "cloud", "cloud_provider": "openai", "active_model_id": local_id,
        })

        assert result["fell_back"] is True
        assert result["routing_mode"] == "automatic"
        assert ModeManager().get_mode() == "automatic"
        assert ModeManager().get_cloud_provider() is None

    _with_mode_manager(scenario)


def test_restore_state_falls_back_when_cloud_provider_no_longer_configured():
    from backend.core import provider_config
    from backend.core.mode_manager import ModeManager

    def scenario(mm):
        wd = _fresh_watchdog()
        original_is_valid = provider_config.is_valid_cloud_provider
        provider_config.is_valid_cloud_provider = lambda name: False
        try:
            result = wd.restore_state(mode_state={
                "routing_mode": "cloud", "cloud_provider": "openai", "active_model_id": None,
            })
        finally:
            provider_config.is_valid_cloud_provider = original_is_valid

        assert result["fell_back"] is True
        assert ModeManager().get_mode() == "automatic"

    _with_mode_manager(scenario)


def test_restore_state_applies_a_valid_forced_snapshot():
    from backend.core import provider_config
    from backend.core.mode_manager import ModeManager

    def scenario(mm):
        wd = _fresh_watchdog()
        mm.set_mode("local")

        original_is_valid = provider_config.is_valid_cloud_provider
        provider_config.is_valid_cloud_provider = lambda name: name == "anthropic"
        try:
            result = wd.restore_state(mode_state={
                "routing_mode": "cloud", "cloud_provider": "anthropic", "active_model_id": None,
            })
        finally:
            provider_config.is_valid_cloud_provider = original_is_valid

        assert result["fell_back"] is False
        assert result["routing_mode"] == "cloud"
        assert result["cloud_provider"] == "anthropic"
        assert ModeManager().get_mode() == "cloud"
        assert ModeManager().get_cloud_provider() == "anthropic"

    _with_mode_manager(scenario)


# ============================================================
# PART 5 — WebSocketHandler integration: fresh truth on (re)connect
# ============================================================
def test_handle_pushes_fresh_mode_status_and_diagnostics_backend_on_connect():
    """
    "Restart -> backend reconnects cleanly" / "Fresh truth packets after
    reconnect": a brand-new connection (which is exactly what a client
    does after the backend restarts) must receive mode_status_result and
    diagnostics_backend_result unsolicited, before anything else, so it
    never has to ask first and never renders stale/absent state while
    waiting.
    """
    from backend.websocket.handlers import WebSocketHandler

    async def scenario():
        ws = FakeWebSocket(packets=[])  # closes immediately after connecting
        handler = WebSocketHandler(ws)
        await handler.handle()
        return ws.sent

    sent = _run(scenario())
    types_seen = [p["type"] for p in sent]
    assert "mode_status_result" in types_seen, types_seen
    assert "diagnostics_backend_result" in types_seen, types_seen
    # mode_status_result must come before diagnostics_backend_result —
    # order isn't arbitrary, both fire unsolicited right at connection
    # start, in the sequence handle() actually issues them.
    assert types_seen.index("mode_status_result") < types_seen.index("diagnostics_backend_result")


def test_handle_marks_connection_opened_and_closed_around_the_session():
    from backend.websocket.handlers import WebSocketHandler
    from backend.core import connection_state

    async def scenario():
        ws = FakeWebSocket(packets=[])
        handler = WebSocketHandler(ws)
        before = connection_state.get_status().active_connections
        await handler.handle()
        after = connection_state.get_status().active_connections
        return before, after

    before, after = _run(scenario())
    assert after == before, "a closed connection must not leak into the active-connection count"


# ============================================================
# PART 6 — diagnostics reflect restartCount / lastRestartTimestamp / backend_connected
# ============================================================
def test_diagnostics_backend_result_reflects_restart_count_and_timestamp():
    from backend import ipc_router
    from backend.core import backend_watchdog as bw_mod

    original_watchdog = bw_mod.watchdog
    fresh = _fresh_watchdog()
    bw_mod.watchdog = fresh
    try:
        fresh.restart_backend_process(exec_fn=lambda: None)
        result = ipc_router.dispatch({"type": "diagnostics_backend_request", "payload": {}})
        payload = result["payload"]
        assert payload["restartCount"] == 1
        assert payload["lastRestartTimestamp"] is not None
    finally:
        bw_mod.watchdog = original_watchdog


def test_diagnostics_backend_result_reflects_connection_truth():
    from backend import ipc_router
    from backend.core import connection_state

    result_before = ipc_router.dispatch({"type": "diagnostics_backend_request", "payload": {}})
    assert result_before["payload"]["backend_connected"] is False

    connection_state.connection_opened()
    try:
        result_after = ipc_router.dispatch({"type": "diagnostics_backend_request", "payload": {}})
        assert result_after["payload"]["backend_connected"] is True
    finally:
        connection_state.connection_closed()


def test_diagnostics_backend_result_includes_last_known_good_state():
    from backend import ipc_router
    from backend.core import backend_watchdog as bw_mod

    bw_mod.watchdog.start()  # idempotent — captures a snapshot if not already started
    result = ipc_router.dispatch({"type": "diagnostics_backend_request", "payload": {}})
    snapshot = result["payload"]["lastKnownGoodState"]
    assert snapshot is not None
    assert "routing_mode" in snapshot


# ============================================================
# PART 7 — weather/tools blocked during downtime, resume after reconnect
#
# The actual GATING mechanism (backend.core.tool_router /
# WebSocketHandler._connection_healthy) is Batch 3's — see
# backend/tests/weather_and_tools_truth_tests.py for the exhaustive
# coverage of that gate itself. These tests are the Batch 4-specific
# angle: that the watchdog's own connection truth agrees with, and a
# real reconnect (a fresh WebSocketHandler.handle()) genuinely restores,
# normal tool execution.
# ============================================================
def test_tools_blocked_while_backend_watchdog_reports_disconnected():
    from backend.core import tool_router

    packet, error = tool_router.execute_tool_truthful("weather", {"location": "Richmond"}, connection_healthy=False)
    assert packet is None
    assert error.code == tool_router.BACKEND_DISCONNECTED


def test_tools_resume_once_reconnected_end_to_end():
    """
    A fresh WebSocketHandler (as a real reconnect produces) starts with
    _connection_healthy=True and successfully dispatches a
    tool_execute_request — no residual "disconnected" state survives
    from a PREVIOUS connection into a new one (each WebSocketHandler
    instance owns its own flag; there is no shared "still down" latch).
    """
    from backend.websocket.handlers import WebSocketHandler
    from backend.core import tool_registry

    tool_registry.register_tool(
        tool_registry.ToolSchema(name="_reconnect_test_tool", description="x", permission=tool_registry.PERMISSION_SAFE),
        lambda: "reconnected-ok",
    )

    async def scenario():
        ws = FakeWebSocket(packets=[
            {"type": "tool_execute_request", "payload": {"tool_name": "_reconnect_test_tool", "args": {}}},
        ])
        handler = WebSocketHandler(ws)
        await handler.handle()
        return ws.sent

    try:
        sent = _run(scenario())
    finally:
        tool_registry.unregister_tool("_reconnect_test_tool")

    result_packet = next(p for p in sent if p["type"] == "tool_execute_result")
    assert result_packet["payload"]["ok"] is True
    assert result_packet["payload"]["value"] == "reconnected-ok"


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
