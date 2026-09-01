# backend/tests/weather_and_tools_truth_tests.py
#
# Regression tests for Batch 3 — weather and tool truth alignment:
#   - backend.core.weather_provider — single source of truth for
#     WeatherAPI's configuration status (enabled/hasApiKey)
#   - backend.core.weather_router — the centralized, always-truthful
#     weather entry point (provider/location/temperature/conditions/
#     timestamp always non-null on success; never fabricates data;
#     never claims WeatherAPI ran when a different real provider did)
#   - backend.core.tool_router — the centralized tool-execution gate
#     (never runs during a disconnected connection, never runs a tool
#     whose required provider isn't configured, always a structured
#     error otherwise, toolName/providerName/location/timestamp always
#     present)
#   - ipc_router's diagnostics_weather_result / diagnostics_tools_result /
#     diagnostics_providers_result reflect exactly this same truth
#
# Self-contained, plain-assert tests, matching backend/tests/routing_invariants_tests.py
# and backend/tests/model_switching_tests.py — not pytest. Run directly:
#
#   python backend/tests/weather_and_tools_truth_tests.py

from __future__ import annotations

import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)



def _result_packets(sent):
    """The packets that carry a result, with status decoration removed.

    Status packets ({"type": "status", ...}) drive the turn indicator and
    carry no result -- see backend/core/turn_status.py. The assertions in
    this file index answer packets positionally (sent[0] is the
    stream_start, sent[1] the token), so the decoration is filtered at the
    point of capture rather than at every assertion.
    """
    return [packet for packet in sent if packet.get("type") != "status"]


def _with_weather_env(configured: bool, fn):
    """
    Deterministically force WeatherAPI's configuration truth for the
    duration of `fn(...)`, restoring the real environment AND the real
    persisted module-key store afterward.

    backend.core.weather_provider.is_weather_configured() checks BOTH
    os.environ["WEATHERAPI_KEY"] AND the encrypted module-key store
    (backend.core.key_manager, via module_manager) — matching
    key_manager.list_configured_providers()'s identical "env var OR
    stored key" pattern for LLM providers. Clearing only the env var
    isn't enough to simulate "not configured" on a machine that already
    has a real WeatherAPI key saved through the settings UI (this repo's
    own dev machine does) — this also temporarily removes the stored
    key for configured=False, and restores the REAL original value
    (decrypted, then re-saved) afterward, never discarding it.
    """
    from backend.core import module_manager

    original_env = os.environ.get("WEATHERAPI_KEY")
    original_stored = module_manager.get_module_key("weather")
    try:
        if configured:
            os.environ["WEATHERAPI_KEY"] = "test-weatherapi-key"
        else:
            os.environ.pop("WEATHERAPI_KEY", None)
            if original_stored:
                module_manager.delete_module_key("weather")
        return fn()
    finally:
        if original_env is None:
            os.environ.pop("WEATHERAPI_KEY", None)
        else:
            os.environ["WEATHERAPI_KEY"] = original_env
        if original_stored and not module_manager.get_module_key("weather"):
            module_manager.set_module_key("weather", original_stored)


def _patch_geocode_and_fusion(geocode_result, fusion_result):
    """
    Batch 3.5: backend.core.weather_router.get_weather_truthful() now
    calls open_meteo_provider.geocode() (turn the location string into
    coordinates) and weather_fusion.get_fused_weather() (the actual
    multi-provider blend) instead of tools.get_weather.get_weather()
    directly — this monkeypatches weather_router's own references to
    both (it does `from backend.core import open_meteo_provider` /
    `from backend.core import weather_fusion` and calls through the
    module, so patching the attribute on weather_router's imported
    module object affects exactly what it calls).

    geocode_result: a dict like {"lat":.., "lon":.., "name":.., ...} or
    None (geocoding failed).
    fusion_result: a WeatherFusionResult or FusionError (backend.core.weather_types).
    """
    from backend.core import weather_router

    original_geocode = weather_router.open_meteo_provider.geocode
    original_fuse = weather_router.weather_fusion.get_fused_weather

    weather_router.open_meteo_provider.geocode = lambda location: geocode_result
    weather_router.weather_fusion.get_fused_weather = lambda lat, lon, target_elevation=None: fusion_result

    def restore():
        weather_router.open_meteo_provider.geocode = original_geocode
        weather_router.weather_fusion.get_fused_weather = original_fuse

    return restore


# ============================================================
# PART 1 — WeatherAPI configuration truth (backend.core.weather_provider)
# ============================================================
def test_weather_provider_status_reports_configured_truthfully():
    from backend.core import weather_provider

    def scenario():
        status = weather_provider.get_weather_provider_status()
        assert status.isWeatherConfigured is True
        assert status.weather_provider_name == "weatherapi"
        assert status.weather_provider_display_name == "WeatherAPI"

    _with_weather_env(True, scenario)


def test_weather_provider_status_reports_not_configured_truthfully():
    from backend.core import weather_provider

    def scenario():
        assert weather_provider.get_weather_provider_status().isWeatherConfigured is False

    _with_weather_env(False, scenario)


def test_require_weather_configured_returns_structured_error_when_missing():
    from backend.core import weather_provider

    def scenario():
        error = weather_provider.require_weather_configured()
        assert error is not None
        assert error.code == weather_provider.WEATHER_PROVIDER_NOT_CONFIGURED

    _with_weather_env(False, scenario)


def test_require_weather_configured_returns_none_when_present():
    from backend.core import weather_provider

    def scenario():
        assert weather_provider.require_weather_configured() is None

    _with_weather_env(True, scenario)


# ============================================================
# PART 2 — WeatherAPI configured/not-configured -> weather_router outcome
#
# Batch 3.5: weather_router.get_weather_truthful() now geocodes the
# location (open_meteo_provider.geocode()) and calls
# weather_fusion.get_fused_weather() — the actual multi-provider
# fetch/blend has moved to backend/tests/weather_fusion_tests.py, which
# tests weather_fusion.py directly against mocked provider modules.
# These tests exercise weather_router's OWN responsibilities: the
# require_weatherapi gate (still WeatherAPI-specific and unrelated to
# fusion), geocoding failure handling, and correctly wrapping whatever
# weather_fusion returns into a WeatherPacket/WeatherError.
# ============================================================
_SAMPLE_GEOCODE = {"lat": 37.5407, "lon": -77.4360, "name": "Richmond", "region": "Virginia", "country": "United States", "elevation": 50.0, "geocodeConfidence": "high"}


def _fake_fusion_result(**overrides):
    from backend.core.weather_types import WeatherFusionResult, ProviderWeatherSample

    defaults = dict(
        fusedTemperatureC=21.5, fusedConditionsText="Sunny", fusedWindSpeedMps=3.0,
        fusedHumidityPercent=55.0, confidence="high",
        samples=[ProviderWeatherSample("noaa", 21.5, "Sunny", 3.0, 55.0, 0.0)],
    )
    defaults.update(overrides)
    return WeatherFusionResult(**defaults)


def test_weatherapi_required_and_configured_still_runs_fusion():
    """
    require_weatherapi=True gates WHETHER the request is allowed at all
    (WeatherAPI must be configured) — it does not mean "only use
    WeatherAPI"; the fused result (however many providers actually
    contributed) is still what's returned.
    """
    from backend.core import weather_router

    restore = _patch_geocode_and_fusion(_SAMPLE_GEOCODE, _fake_fusion_result())
    try:
        packet, error = _with_weather_env(True, lambda: weather_router.get_weather_truthful("Richmond, VA", require_weatherapi=True))
    finally:
        restore()

    assert error is None, error
    assert packet.temperatureC == 21.5
    assert packet.conditionsText == "Sunny"
    assert packet.confidence == "high"


def test_weatherapi_not_configured_and_required_returns_structured_error():
    from backend.core import weather_router, weather_provider

    def scenario():
        packet, error = weather_router.get_weather_truthful("Richmond, VA", require_weatherapi=True)
        assert packet is None
        assert error is not None
        assert error.code == weather_provider.WEATHER_PROVIDER_NOT_CONFIGURED

    _with_weather_env(False, scenario)


def test_weatherapi_not_required_still_fuses_with_whichever_providers_succeeded():
    """
    The everyday NL "what's the weather" path does NOT hard-require
    WeatherAPI (NOAA/Open-Meteo are real, working, keyless providers) —
    the packet's providersUsed must say so honestly (never claim
    WeatherAPI ran when it wasn't even configured).
    """
    from backend.core import weather_router

    fusion = _fake_fusion_result(fusedTemperatureC=18.0, fusedConditionsText="Overcast")
    restore = _patch_geocode_and_fusion(_SAMPLE_GEOCODE, fusion)
    try:
        packet, error = _with_weather_env(False, lambda: weather_router.get_weather_truthful("Richmond, VA", require_weatherapi=False))
    finally:
        restore()

    assert error is None, error
    assert packet.providers_used == ["noaa"], "must report the REAL provider(s) that served the data, never a guess"
    assert "weatherapi" not in packet.providers_used


def test_geocoding_failure_returns_structured_error():
    from backend.core import weather_router

    restore = _patch_geocode_and_fusion(None, _fake_fusion_result())
    try:
        packet, error = weather_router.get_weather_truthful("Nowhereville, Xyzzyland")
    finally:
        restore()

    assert packet is None
    assert error is not None
    assert error.code == weather_router.GEOCODING_FAILED


# ============================================================
# PART 3 — never fabricate weather data
# ============================================================
def test_fusion_failure_returns_structured_error_never_fake_data():
    from backend.core import weather_router
    from backend.core.weather_types import FusionError, ProviderError

    fusion_error = FusionError(
        weather_router.WEATHER_FUSION_FAILED, "All providers failed",
        provider_errors=[ProviderError("noaa", "NOAA_OUT_OF_COVERAGE", "x"), ProviderError("open_meteo", "OPEN_METEO_REQUEST_FAILED", "boom")],
    )
    restore = _patch_geocode_and_fusion(_SAMPLE_GEOCODE, fusion_error)
    try:
        packet, error = weather_router.get_weather_truthful("Nowhereville")
    finally:
        restore()

    assert packet is None, "a failed fetch must never produce a packet with guessed/fake data"
    assert error is not None
    assert error.code == weather_router.WEATHER_FUSION_FAILED


# ============================================================
# PART 4 — weather packets never contain null fields
# ============================================================
def test_weather_packet_never_contains_null_fields():
    from backend.core import weather_router

    restore = _patch_geocode_and_fusion(_SAMPLE_GEOCODE, _fake_fusion_result())
    try:
        packet, error = weather_router.get_weather_truthful("Richmond, VA")
    finally:
        restore()

    assert error is None
    for field in ("location", "lat", "lon", "temperatureC", "conditionsText", "confidence", "samples", "timestamp"):
        value = getattr(packet, field)
        assert value is not None, f"WeatherPacket.{field} must never be null"
    assert len(packet.samples) >= 1, "a successful fusion result must always carry at least one sample"

    d = packet.to_dict()
    for key in ("location", "lat", "lon", "temperatureC", "conditionsText", "confidence", "samples", "providersUsed", "timestamp"):
        assert d.get(key) is not None, f"weather packet dict['{key}'] must never be null"


# ============================================================
# PART 5 — tools disabled during backend disconnect
# ============================================================
def test_weather_chat_intent_skipped_when_connection_unhealthy():
    """
    Batch 3.6: the NL "what's the weather" chat short-circuit
    (WebSocketHandler._answer_weather_intent_directly) must respect the
    same connection-health signal as tool_execute_request — a weather
    packet must never be sent (and weather_router.get_weather_truthful()
    must never even be called) once this connection is known to be down.
    """
    import asyncio
    import json
    from backend.websocket.handlers import WebSocketHandler
    from backend.core import weather_router

    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, raw):
            self.sent.append(json.loads(raw))

    called = {"n": 0}

    def exploding_get_weather_truthful(location, require_weatherapi=False):
        called["n"] += 1
        raise AssertionError("weather_router.get_weather_truthful() must never be called while the connection is unhealthy")

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)
        handler._connection_healthy = False

        original = weather_router.get_weather_truthful
        weather_router.get_weather_truthful = exploding_get_weather_truthful
        try:
            await handler._dispatch({
                "type": "chat_request",
                "payload": {
                    "messages": [{"role": "user", "content": "what is the weather in Richmond VA"}],
                    "conversationId": "weather-disconnect-test",
                    "multiTurn": True,
                },
            })
            await handler.wait_for_turns()
        finally:
            weather_router.get_weather_truthful = original
        return ws.sent

    sent = _result_packets(asyncio.run(scenario()))
    assert called["n"] == 0, "the fusion engine must never actually run while disconnected"
    assert len(sent) == 1, f"expected exactly one packet (a structured error), got {sent}"
    assert sent[0]["type"] == "error"
    assert sent[0]["code"] == "BACKEND_DISCONNECTED"


def test_tool_execution_rejected_when_connection_unhealthy():
    from backend.core import tool_router

    packet, error = tool_router.execute_tool_truthful("weather", {"location": "Richmond"}, connection_healthy=False)

    assert packet is None, "no tool must ever run (or appear to run) while the connection is known to be down"
    assert error is not None
    assert error.code == tool_router.BACKEND_DISCONNECTED


def test_tool_execute_request_rejected_end_to_end_when_connection_unhealthy():
    """
    Same guard, exercised through the real WebSocketHandler._dispatch()
    path for a tool_execute_request packet — not just tool_router in
    isolation — proving ipc_router/tool_registry.execute_tool() is never
    reached at all while the connection is unhealthy.
    """
    import asyncio
    import json
    from backend.websocket.handlers import WebSocketHandler
    from backend.core import tool_registry

    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, raw):
            self.sent.append(json.loads(raw))

    called = {"n": 0}
    tool_registry.register_tool(
        tool_registry.ToolSchema(name="_disconnect_test_tool", description="x", permission=tool_registry.PERMISSION_SAFE),
        lambda: called.__setitem__("n", called["n"] + 1),
    )

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)
        handler._connection_healthy = False
        await handler._dispatch({
            "type": "tool_execute_request",
            "payload": {"tool_name": "_disconnect_test_tool", "args": {}},
        })
        return ws.sent

    try:
        sent = _result_packets(asyncio.run(scenario()))
    finally:
        tool_registry.unregister_tool("_disconnect_test_tool")

    assert called["n"] == 0, "the tool handler must never actually run while disconnected"
    assert len(sent) == 1
    assert sent[0]["type"] == "error"
    assert sent[0]["code"] == "BACKEND_DISCONNECTED"


def test_tool_execution_proceeds_when_connection_healthy():
    from backend.core import tool_registry, tool_router

    tool_registry.register_tool(
        tool_registry.ToolSchema(name="_truth_test_tool", description="x", permission=tool_registry.PERMISSION_SAFE),
        lambda: "ok-value",
    )
    try:
        packet, error = tool_router.execute_tool_truthful("_truth_test_tool", {}, connection_healthy=True)
        assert error is None
        assert packet["ok"] is True
        assert packet["value"] == "ok-value"
    finally:
        tool_registry.unregister_tool("_truth_test_tool")


# ============================================================
# PART 6 — tools return structured errors when required provider misconfigured
# ============================================================
def test_tool_execution_rejected_when_required_provider_not_configured():
    from backend.core import tool_router

    tool_router.TOOL_REQUIRED_PROVIDERS["_needs_provider_tool"] = lambda: False
    try:
        packet, error = tool_router.execute_tool_truthful("_needs_provider_tool", {})
        assert packet is None
        assert error is not None
        assert error.code == tool_router.TOOL_PROVIDER_NOT_CONFIGURED
    finally:
        del tool_router.TOOL_REQUIRED_PROVIDERS["_needs_provider_tool"]


def test_weather_tool_via_registry_surfaces_fusion_failure():
    """
    End-to-end: the registered "weather" tool (backend.core.tool_registry)
    routes through weather_router -> weather_fusion — a total fusion
    failure (every provider unavailable/failed) must surface as a
    structured error inside the tool's own result, which tool_router
    promotes to the outer error/error_code.
    """
    from backend.core import tool_router
    from backend.core.weather_types import FusionError

    restore = _patch_geocode_and_fusion(
        _SAMPLE_GEOCODE, FusionError("WEATHER_FUSION_FAILED", "All providers failed"),
    )
    try:
        packet, router_error = tool_router.execute_tool_truthful("weather", {"location": "Nowhereville"})
    finally:
        restore()

    assert router_error is None  # not a pre-flight rejection — the tool ran
    assert packet["ok"] is False
    assert packet["error_code"] == "WEATHER_FUSION_FAILED"
    assert packet["toolName"] == "weather"
    assert packet["location"] == "cloud"
    assert packet["timestamp"] is not None


# ============================================================
# PART 7 — tool packets never contain null fields (on success)
# ============================================================
def test_tool_packet_never_has_null_toolname_location_or_timestamp():
    from backend.core import tool_registry, tool_router

    tool_registry.register_tool(
        tool_registry.ToolSchema(name="_null_field_test_tool", description="x", permission=tool_registry.PERMISSION_SAFE),
        lambda: {"anything": 1},
    )
    try:
        packet, error = tool_router.execute_tool_truthful("_null_field_test_tool", {})
        assert error is None
        assert packet["toolName"] is not None
        assert packet["location"] is not None
        assert packet["timestamp"] is not None
        # providerName is legitimately None for a tool with no provider
        # concept (this test tool) — only toolName/location/timestamp
        # are unconditionally required.
    finally:
        tool_registry.unregister_tool("_null_field_test_tool")


def test_weather_tool_packet_includes_provider_name_on_success():
    """
    Batch 3.5: weather is a FUSED, multi-provider result — providerName
    reports every provider that actually contributed, joined, not a
    single guessed name (see tool_router._provider_name_for()).
    """
    from backend.core import tool_router

    fusion = _fake_fusion_result()  # samples=[ProviderWeatherSample("noaa", ...)]
    restore = _patch_geocode_and_fusion(_SAMPLE_GEOCODE, fusion)
    try:
        packet, error = _with_weather_env(True, lambda: tool_router.execute_tool_truthful("weather", {"location": "Richmond, VA"}))
    finally:
        restore()

    assert error is None
    assert packet["ok"] is True
    assert packet["providerName"] == "noaa"
    assert packet["toolName"] == "weather"
    assert packet["location"] == "cloud"
    assert packet["timestamp"] is not None


# ============================================================
# PART 8 — diagnostics accurately reflect provider truth
# ============================================================
def test_diagnostics_weather_result_reflects_not_configured():
    from backend import ipc_router

    def scenario():
        result = ipc_router.dispatch({"type": "diagnostics_weather_request", "payload": {}})
        payload = result["payload"]
        assert payload["isWeatherConfigured"] is False
        assert payload["status_text"] == "WeatherAPI: Not configured"
        return payload

    _with_weather_env(False, scenario)


def test_diagnostics_weather_result_reflects_configured():
    from backend import ipc_router

    def scenario():
        payload = ipc_router.dispatch({"type": "diagnostics_weather_request", "payload": {}})["payload"]
        assert payload["isWeatherConfigured"] is True
        assert payload["status_text"] == "WeatherAPI: Configured"

    _with_weather_env(True, scenario)


def test_diagnostics_weather_result_reflects_last_success_and_error():
    from backend import ipc_router
    from backend.core import weather_router

    fusion = _fake_fusion_result(fusedTemperatureC=22.0)
    restore = _patch_geocode_and_fusion(_SAMPLE_GEOCODE, fusion)
    try:
        packet, error = weather_router.get_weather_truthful("Richmond, VA")
        assert error is None
    finally:
        restore()

    result = ipc_router.dispatch({"type": "diagnostics_weather_request", "payload": {}})
    last_success = result["payload"]["last_success"]
    assert last_success is not None
    assert last_success["providersUsed"] == ["noaa"]
    assert last_success["temperatureC"] == 22.0


def test_diagnostics_tools_result_lists_registered_tools_with_location():
    from backend import ipc_router

    result = ipc_router.dispatch({"type": "diagnostics_tools_request", "payload": {}})
    tools = {t["name"]: t for t in result["payload"]["tools"]}
    assert "weather" in tools and "web_search" in tools
    assert tools["weather"]["location"] == "cloud"
    assert tools["web_search"]["location"] == "cloud"


def test_diagnostics_providers_result_includes_weather_module():
    from backend import ipc_router

    def scenario():
        result = ipc_router.dispatch({"type": "diagnostics_providers_request", "payload": {}})
        payload = result["payload"]
        assert "llm_providers" in payload
        assert payload["weather_provider"]["hasApiKey"] is True
        assert payload["weather_provider"]["display_name"] == "WeatherAPI"

    _with_weather_env(True, scenario)


def test_diagnostics_weather_and_tools_report_backend_connected_state():
    from backend import ipc_router
    from backend.core import connection_state

    connection_state.connection_opened()
    try:
        weather_payload = ipc_router.dispatch({"type": "diagnostics_weather_request", "payload": {}})["payload"]
        tools_payload = ipc_router.dispatch({"type": "diagnostics_tools_request", "payload": {}})["payload"]
        assert weather_payload["backend_connected"] is True
        assert tools_payload["backend_connected"] is True
    finally:
        connection_state.connection_closed()


# ============================================================
# PART 9 — Batch 3.6: NL weather routed through the fusion engine, no
# LLM hallucination, no retired "weather" modelId sentinel.
# ============================================================
import asyncio as _asyncio
import json as _json


class _FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(_json.loads(raw))


def _patch_weather_pipeline(p, geocode_result, fusion_result):
    """Wires open_meteo_provider.geocode()/weather_fusion.get_fused_weather()
    to fixed stand-ins so the NL path resolves deterministically, no real
    network calls. `p` is a _Patcher (see weather_fusion_tests.py's
    identical helper) — reused here via manual save/restore since this
    file predates that helper."""
    from backend.core import weather_router, open_meteo_provider

    original_geocode = open_meteo_provider.geocode
    original_fuse = weather_router.weather_fusion.get_fused_weather
    open_meteo_provider.geocode = lambda location: geocode_result
    weather_router.weather_fusion.get_fused_weather = lambda lat, lon, target_elevation=None: fusion_result

    def restore():
        open_meteo_provider.geocode = original_geocode
        weather_router.weather_fusion.get_fused_weather = original_fuse

    return restore


def test_nl_weather_request_with_county_name_only_uses_fusion():
    """"What is the weather in Orange County VA" -> fusion engine, real
    fused temperature/conditions, never the retired "weather" sentinel."""
    from backend.websocket.handlers import WebSocketHandler
    from backend.core import weather_router

    restore = _patch_weather_pipeline(
        None,
        {"lat": 38.2465, "lon": -78.1114, "name": "Orange County", "region": "Virginia", "country": "United States", "elevation": 172.0, "geocodeConfidence": "high"},
        _fake_fusion_result(fusedTemperatureC=18.9, fusedConditionsText="Cloudy"),
    )
    original_last_success = weather_router._last_success

    async def scenario():
        ws = _FakeWebSocket()
        handler = WebSocketHandler(ws)
        await handler._dispatch({
            "type": "chat_request",
            "payload": {
                "messages": [{"role": "user", "content": "What is the weather in Orange County VA"}],
                "conversationId": "county-only-test", "multiTurn": True,
            },
        })
        await handler.wait_for_turns()
        return ws.sent

    try:
        sent = _result_packets(_asyncio.run(scenario()))
    finally:
        restore()

    assert [p["type"] for p in sent] == ["stream_start", "stream_token", "stream_end"]
    assert sent[0]["modelId"] == "weather-fusion"
    assert sent[0]["modelId"] != "weather", "the retired modelId='weather' sentinel must never be used"
    token = sent[1]["token"]
    assert "Cloudy" in token
    assert "°F" in token
    # The fusion engine (weather_router.get_weather_truthful()) must have
    # actually run and recorded a real success — not a guessed reply.
    assert weather_router._last_success is not None
    assert weather_router._last_success["confidence"] == "high"
    weather_router._last_success = original_last_success


def test_nl_weather_request_with_full_city_and_zip_resolves_via_geocoding():
    """A location string with city + county + ZIP ("Orange, Orange
    County VA, 22960") must be passed straight through to the SAME
    geocoder weather_router.py itself uses, and the fused result must
    reflect whatever it resolves to."""
    from backend.websocket.handlers import WebSocketHandler

    geocode_result = {"lat": 38.2465, "lon": -78.1114, "name": "Orange", "region": "Virginia", "country": "United States", "elevation": 172.0, "geocodeConfidence": "high"}
    restore = _patch_weather_pipeline(None, geocode_result, _fake_fusion_result(fusedTemperatureC=18.9, fusedConditionsText="Cloudy"))

    async def scenario():
        ws = _FakeWebSocket()
        handler = WebSocketHandler(ws)
        await handler._dispatch({
            "type": "chat_request",
            "payload": {
                "messages": [{"role": "user", "content": "weather in Orange, Orange County VA, 22960"}],
                "conversationId": "city-zip-test", "multiTurn": True,
            },
        })
        await handler.wait_for_turns()
        return ws.sent

    try:
        sent = _result_packets(_asyncio.run(scenario()))
    finally:
        restore()

    token = sent[1]["token"]
    assert "Orange, Virginia, United States" in token, token
    assert "Cloudy" in token


def test_nl_weather_correction_flow_never_hallucinates():
    """
    User: "what is the weather in Orange County VA" -> fused answer.
    User: "that is incorrect, try again" -> deterministic clarification
    prompt (modelId="system"), NEVER a real model call.
    User: "Orange, VA, 22960" (bare location, no "weather in" prefix)
    -> a fresh fusion lookup, still never an LLM.
    """
    from backend.websocket.handlers import WebSocketHandler

    restore = _patch_weather_pipeline(
        None,
        {"lat": 38.2465, "lon": -78.1114, "name": "Orange", "region": "Virginia", "country": "United States", "elevation": 172.0, "geocodeConfidence": "high"},
        _fake_fusion_result(fusedTemperatureC=18.9, fusedConditionsText="Cloudy"),
    )

    async def scenario():
        ws = _FakeWebSocket()
        handler = WebSocketHandler(ws)

        async def _explode(*_a, **_kw):
            raise AssertionError("_start_inference() must never be called anywhere in the weather correction flow")
        handler._start_inference = _explode

        await handler._dispatch({
            "type": "chat_request",
            "payload": {"messages": [{"role": "user", "content": "what is the weather in Orange County VA"}], "conversationId": "correction-test", "multiTurn": True},
        })
        await handler.wait_for_turns()
        first_turn = _result_packets(ws.sent)
        ws.sent.clear()

        await handler._dispatch({
            "type": "chat_request",
            "payload": {"messages": [{"role": "user", "content": "that is incorrect, try again"}], "conversationId": "correction-test", "multiTurn": True},
        })
        await handler.wait_for_turns()
        second_turn = _result_packets(ws.sent)
        ws.sent.clear()

        await handler._dispatch({
            "type": "chat_request",
            "payload": {"messages": [{"role": "user", "content": "Orange, VA, 22960"}], "conversationId": "correction-test", "multiTurn": True},
        })
        await handler.wait_for_turns()
        third_turn = _result_packets(ws.sent)

        return first_turn, second_turn, third_turn

    try:
        first_turn, second_turn, third_turn = _asyncio.run(scenario())
    finally:
        restore()

    assert first_turn[0]["modelId"] == "weather-fusion"

    assert second_turn[0]["modelId"] == "system", "the clarification prompt must be deterministic, never a real model"
    assert "location" in second_turn[1]["token"].lower()

    assert third_turn[0]["modelId"] == "weather-fusion"
    assert "Cloudy" in third_turn[1]["token"]


def test_nl_weather_still_uses_fusion_when_owm_and_weatherapi_unconfigured():
    """
    Simulated missing OWM/WeatherAPI keys — the fusion engine (and
    therefore the NL path) must still work using NOAA + Open-Meteo, and
    NL routing must still go through fusion, never fall back to
    modelId="weather".
    """
    from backend.websocket.handlers import WebSocketHandler
    from backend.core.weather_types import ProviderWeatherSample

    fusion = _fake_fusion_result(
        fusedTemperatureC=15.0, fusedConditionsText="Overcast", confidence="high",
        samples=[
            ProviderWeatherSample("noaa", 15.0, "Overcast", 2.0, 60.0, 0),
            ProviderWeatherSample("open_meteo", 15.2, "Overcast", 2.1, 58.0, 0.0),
        ],
    )
    restore = _patch_weather_pipeline(
        None,
        {"lat": 38.2465, "lon": -78.1114, "name": "Orange", "region": "Virginia", "country": "United States", "elevation": 172.0, "geocodeConfidence": "high"},
        fusion,
    )

    async def scenario():
        ws = _FakeWebSocket()
        handler = WebSocketHandler(ws)
        await handler._dispatch({
            "type": "chat_request",
            "payload": {"messages": [{"role": "user", "content": "weather in Orange County VA"}], "conversationId": "no-keys-test", "multiTurn": True},
        })
        await handler.wait_for_turns()
        return ws.sent

    try:
        sent = _result_packets(_asyncio.run(scenario()))
    finally:
        restore()

    assert sent[0]["modelId"] == "weather-fusion"
    assert "Overcast" in sent[1]["token"]


def test_no_legacy_weather_model_sentinel_anywhere_in_nl_routing_source():
    """
    Static source-scan guard (Batch 3.6, final enforced version) — a
    literal `modelId == "weather"` / `model_id == "weather"` retired
    sentinel check must never reappear in either live NL entry point,
    not just in the specific scenarios the other tests happen to
    exercise. Scans the actual .py source text, not behavior, so it
    catches a reintroduced dead branch even if nothing currently calls
    it.
    """
    import re
    import backend.websocket.handlers as ws_handlers
    import backend.rest.router as rest_router
    import backend.core.weather_nl as weather_nl_module

    # Strip triple-quoted docstrings/comment blocks first — this module's
    # own docstring literally names the retired pattern as an example of
    # what should NOT be found in *code*, which would otherwise trip the
    # scan on itself.
    docstring_re = re.compile(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'')
    comment_re = re.compile(r'#.*$', re.MULTILINE)
    forbidden = re.compile(r'''(model_id|modelId)\s*==\s*["']weather["']''')

    for module in (ws_handlers, weather_nl_module, rest_router):
        with open(module.__file__, "r", encoding="utf-8") as f:
            source = f.read()
        code_only = comment_re.sub("", docstring_re.sub("", source))
        match = forbidden.search(code_only)
        assert match is None, (
            f"{module.__file__} contains a retired modelId=='weather' check: {match.group(0)!r}"
        )


def test_rest_prepare_chat_turn_routes_weather_through_fusion():
    """
    Same routing, verified through the REST entry point
    (backend/rest/router.py's _prepare_chat_turn()) — structurally
    separate code from the WebSocket path (REST has no persistent
    per-connection state, so its clarification/correction detection
    reads `history` instead — see _prepare_chat_turn's own comment),
    so it needs its own direct check rather than assuming parity with
    the WebSocket tests above.
    """
    from backend.rest.router import _prepare_chat_turn, ChatRequest

    restore = _patch_weather_pipeline(
        None,
        {"lat": 38.2465, "lon": -78.1114, "name": "Orange", "region": "Virginia", "country": "United States", "elevation": 172.0, "geocodeConfidence": "high"},
        _fake_fusion_result(fusedTemperatureC=18.9, fusedConditionsText="Cloudy"),
    )

    async def scenario():
        loop = _asyncio.get_running_loop()
        payload = ChatRequest(message="what is the weather in Orange County VA")
        return await _prepare_chat_turn(payload, loop)

    try:
        result = _asyncio.run(scenario())
    finally:
        restore()

    assert result["model_id"] == "weather-fusion"
    assert result["short_circuit"]["kind"] == "text"
    assert "Cloudy" in result["short_circuit"]["text"]


def test_rest_prepare_chat_turn_correction_flow_reads_state_from_history():
    """
    REST has no persistent connection object — the correction-phrase
    check (backend.core.weather_nl.is_correction_phrase()) must detect
    "last turn was weather" purely from the `history` the client sends
    back, and the bare-location-reply check must detect "awaiting a
    location" from the last assistant message being EXACTLY the
    clarification prompt.
    """
    from backend.rest.router import _prepare_chat_turn, ChatRequest, ChatMessage
    from backend.core import weather_nl

    async def scenario_correction():
        loop = _asyncio.get_running_loop()
        payload = ChatRequest(
            message="that is incorrect, try again",
            history=[
                ChatMessage(role="user", content="weather in Orange County VA"),
                ChatMessage(role="assistant", content="Weather for Orange, Virginia: 66°F, Cloudy, wind 3.6 mph."),
            ],
        )
        return await _prepare_chat_turn(payload, loop)

    async def scenario_bare_location():
        loop = _asyncio.get_running_loop()
        payload = ChatRequest(
            message="Orange, VA, 22960",
            history=[
                ChatMessage(role="user", content="weather in Orange County VA"),
                ChatMessage(role="assistant", content=weather_nl.CLARIFICATION_PROMPT),
            ],
        )
        return await _prepare_chat_turn(payload, loop)

    correction_result = _asyncio.run(scenario_correction())
    assert correction_result["model_id"] == "system"
    assert correction_result["short_circuit"]["text"] == weather_nl.CLARIFICATION_PROMPT

    restore = _patch_weather_pipeline(
        None,
        {"lat": 38.2465, "lon": -78.1114, "name": "Orange", "region": "Virginia", "country": "United States", "elevation": 172.0, "geocodeConfidence": "high"},
        _fake_fusion_result(fusedTemperatureC=18.9, fusedConditionsText="Cloudy"),
    )
    try:
        bare_location_result = _asyncio.run(scenario_bare_location())
    finally:
        restore()

    assert bare_location_result["model_id"] == "weather-fusion"
    assert "Cloudy" in bare_location_result["short_circuit"]["text"]


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
