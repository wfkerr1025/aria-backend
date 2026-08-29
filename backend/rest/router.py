# backend/rest/router.py

"""
REST API v1 — Phase 1 scaffolding.

This is a genuinely new, versioned HTTP surface (/v1/*), separate from
backend/server.py's existing, already-comprehensive FastAPI app (/health,
/chat, /stream, /providers, /modules, /command, ... on port 5000) —
that app is left completely untouched per this task's constraints. /v1/*
runs as its own process on its own port (see backend/rest/server.py),
and every route here is a thin wrapper that calls straight into existing
backend.core modules — nothing here recomputes or duplicates a data
source that already exists.

Error codes come from backend/ipc_errors.py (the same registry the
WebSocket IPC layer uses — see that module's docstring) rather than a
separate REST-only vocabulary. The wire shape differs from the WebSocket
side's ({"type": "error", ...}) because REST error conventions differ
from this app's WS packet envelope, but the codes themselves are shared:
{"error": {"code": "...", "message": "..."}}.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.core import model_manager
from backend.core.model_manager import list_models
from backend.core.self_knowledge import BACKEND_VERSION, resolve_active_model_and_provider
from backend import ipc_errors
from backend import ipc_schema as schema

from backend.core.model_registry import get_model as _get_model_cfg
from backend.core.resource_monitor import get_resource_snapshot
from backend.core.compatibility_checker import check_requirements
from backend.core.safety_profiles import select_profile
from backend.core.performance_estimator import estimate_speed
from backend.core.model_info import build_model_info
from backend.core import hardware_snapshot_cache, cache_manager, perf_profiler
from backend.core.model_size_requirements import load_cache as _load_requirements_cache
from backend.core import metrics as _metrics
from backend.core import plugin_registry as _plugin_registry
from backend.core import auto_balancer as _auto_balancer
from backend.core.provider_interface import get_unified_provider, list_unified_providers
from backend.core import tool_registry as _tool_registry
from backend.core import routing_guard
from backend.core import tool_router
from backend.core import weather_provider
from backend.core import weather_router
from backend.core import weather_fusion
from backend.core import connection_state
from backend.core import provider_config
from backend.core import backend_watchdog
from backend.core.errors import AriaError
from backend.core import complexity_router as _complexity_router
from backend.core import routing_history as _routing_history
from backend.core import model_catalog as _model_catalog
from backend.core import pc_capability_tier as _pc_capability_tier
from backend.core.model_registry import get_active_model_id as _get_active_model_id, get_fallback_model_id as _get_fallback_model_id, get_emergency_model_id as _get_emergency_model_id
from backend.core.model_metadata import get_model_params as _get_model_params

# ---------------------------------------------------------------------------
# Chat pipeline primitives — the SAME backend.core modules
# backend/websocket/handlers.py and backend/server.py's existing /chat
# each independently orchestrate (this codebase's established pattern:
# every live entry point calls straight into these, rather than funneling
# through one shared "pipeline" function — see _prepare_chat_turn()'s
# docstring below for why a third independent orchestration here follows
# that same precedent instead of introducing a new shared abstraction).
# ---------------------------------------------------------------------------
from backend.core.local_inference_engine import InferenceRequest
from backend.core.streaming_engine import StreamingEngine
from backend.core.model_registry import get_model
from backend.core.lighter_model_engine import LighterModelEngine
from backend.core.mode_manager import ModeManager
from backend.core import key_manager
from backend.core import self_knowledge
from backend.core import weather_nl
from backend.core.conversation_manager import (
    log_history_policy,
    log_intent_detected,
    log_response_optimized,
    log_self_query_resolved,
    log_system_prompt_applied,
    log_tool_routing_decision,
    optimize_response,
)
from backend.core.answer_stream import AnswerStream
from backend.core import turn_status
from backend.chat import supervisor_layer
from backend.config import model_roles
from backend.core.turn_orchestrator import orchestrate_turn
from backend.core.turn_types import (
    KIND_CLARIFY,
    KIND_ERROR,
    KIND_MODEL_SWITCH,
    KIND_SAFETY_WARNING,
    KIND_TEXT,
    SessionState,
    TurnRequest,
)

from backend.rest.auth import require_api_key

from backend.logger import log as unified_log
from logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/v1")

_stream_engine = StreamingEngine()
_lighter_model_engine = LighterModelEngine()

# Process start time for this REST server specifically — this is a
# separate process from backend/ws_server.py, so "uptime" here means
# this server's own, not the WebSocket server's or the app's overall.
_START_TIME = time.monotonic()


def _rest_error(code: str, message: str, status_code: int = 500) -> HTTPException:
    """
    The one error shape every /v1/* route raises through — see this
    module's docstring for why `code` comes from backend/ipc_errors.py
    rather than a new REST-only set of codes.
    """
    logger.error("REST error [%s]: %s", code, message)
    unified_log("rest", "ERROR", message, {"code": code, "status_code": status_code})
    return HTTPException(status_code=status_code, detail={"error": {"code": code, "message": message}})


@router.get("/health")
def get_health() -> Dict[str, Any]:
    """
    GET /v1/health → {"status": "ok", "version": <backend version>, "uptime": <seconds>}

    "version" is backend.core.self_knowledge.BACKEND_VERSION — the same
    constant the chat pipeline's self-knowledge answers already use for
    "what version are you" — not a fourth hardcoded copy.
    """
    return {
        "status": "ok",
        "version": BACKEND_VERSION,
        "uptime": round(time.monotonic() - _START_TIME, 3),
    }


@router.get("/models")
def get_models() -> Dict[str, Any]:
    """
    GET /v1/models → {"models": [...]}

    Calls backend.core.model_manager.list_models() directly — the exact
    same function backend/ipc_router.py's models_list_request handler
    calls for the WebUI's Models page (see
    ipc_router._handle_models_list()) — so this can never drift from
    what the UI shows.
    """
    try:
        models = list_models()
    except Exception as e:
        logger.exception("get_models() failed: %s", e)
        raise _rest_error(ipc_errors.HANDLER_EXCEPTION, str(e), 500)

    return {"models": models}


def _resolve_model_or_404(model_id: str) -> Dict[str, Any]:
    model_cfg = _get_model_cfg(model_id)
    if model_cfg is None:
        raise _rest_error(ipc_errors.UNKNOWN_MODEL, f"Unknown model_id '{model_id}'", 404)
    return model_cfg


@router.get("/models/{model_id}/requirements")
def get_model_requirements(model_id: str) -> Dict[str, Any]:
    """
    GET /v1/models/{model_id}/requirements → {"model_cfg", "compat"}

    Same underlying check_requirements() call as
    ipc_router._handle_model_requirements() — including the same warmed
    requirements cache, so this is never a second, slower path to the
    same answer.
    """
    model_cfg = _resolve_model_or_404(model_id)
    snapshot = get_resource_snapshot()
    compat = check_requirements(snapshot, model_cfg)
    return {"model_cfg": model_cfg, "compat": compat}


@router.get("/models/{model_id}/performance")
def get_model_performance(model_id: str) -> Dict[str, Any]:
    """
    GET /v1/models/{model_id}/performance → {"model_cfg", "projected_speed_toksec"}

    Same estimate_speed() call (and TTL-cached hardware detection) as
    ipc_router._handle_model_performance().
    """
    model_cfg = _resolve_model_or_404(model_id)
    snapshot = get_resource_snapshot()
    profile = select_profile(snapshot, model_cfg)
    speed = estimate_speed(snapshot, model_cfg, profile)
    return {"model_cfg": model_cfg, "projected_speed_toksec": speed}


@router.get("/models/{model_id}/metadata")
def get_model_metadata(model_id: str) -> Dict[str, Any]:
    """
    GET /v1/models/{model_id}/metadata → the unified ModelInfo view
    (params, quant, quant_bytes, quant_difficulty, context, requirements,
    cache_fingerprint, safety_profile) — see backend.core.model_info.
    """
    model_cfg = _resolve_model_or_404(model_id)
    snapshot = get_resource_snapshot()
    return build_model_info(model_cfg, snapshot).to_dict()


@router.get("/resources")
def get_resources() -> Dict[str, Any]:
    """GET /v1/resources → live CPU%/RAM/VRAM usage (backend.core.resource_monitor)."""
    snapshot = get_resource_snapshot()
    return {
        "cpu_usage_pct": snapshot.cpu_usage,
        "ram_used_gb": round(snapshot.ram_used_gb, 2),
        "ram_total_gb": round(snapshot.ram_total_gb, 2),
        "ram_used_pct": round(snapshot.ram_used_pct, 2),
        "vram_used_gb": round(snapshot.vram_used_gb, 2),
        "vram_total_gb": round(snapshot.vram_total_gb, 2),
        "vram_used_pct": round(snapshot.vram_used_pct, 2),
        "unity_running": snapshot.unity_running,
    }


# ---------------------------------------------------------------------------
# Diagnostics (Phase 3) — GET /v1/diagnostics/*, one route per
# backend/ipc_router.py diagnostics_*_request handler, calling the exact
# same backend.core functions so REST and the WebSocket IPC surface can
# never drift from each other on what "hardware"/"cache"/"performance"
# means.
# ---------------------------------------------------------------------------

@router.get("/diagnostics/hardware")
def get_diagnostics_hardware(refresh: bool = False) -> Dict[str, Any]:
    """
    GET /v1/diagnostics/hardware?refresh=true → CPU/RAM/GPU/storage
    classification, plus the overall Tier 0-6 PC-capability rating (see
    backend.core.pc_capability_tier.get_pc_tier()) computed from that
    same snapshot so the two can never disagree.
    """
    data = hardware_snapshot_cache.refresh() if refresh else hardware_snapshot_cache.get_all()
    data = dict(data)
    data["cache_state"] = hardware_snapshot_cache.cache_state()
    data["pc_tier"] = _pc_capability_tier.get_pc_tier(data)
    return data


@router.get("/diagnostics/models")
def get_diagnostics_models() -> Dict[str, Any]:
    """
    GET /v1/diagnostics/models → per-model ModelInfo for the whole
    catalog, without the live compat/speed comparison GET /v1/models
    does — this is "what does the backend know about each model file",
    not "how does it stack up against this machine".

    Also carries the dual-context UI's Diagnostics → Models page block:
    active chat model / system fallback model / last local escalation /
    last cloud escalation / highest available local model (by installed
    param count — see backend.core.model_metadata.get_model_params()).
    """
    snapshot = get_resource_snapshot()
    cfgs = list_models_cfgs()
    infos = [build_model_info(m, snapshot).to_dict() for m in cfgs]

    # Batch 1 stability fix: this used to unconditionally fall back to
    # _get_active_model_id() (the persisted LOCAL default) whenever
    # there was no explicit override, regardless of routing_mode —
    # reporting a real local model_id here even in Cloud Mode, the same
    # "Cloud Mode + nemo-12b-q5" contradiction fixed in ipc_router.py's
    # _handle_mode_status(). Reuses self_knowledge's mode-aware
    # resolution (None in Cloud Mode, since there's no single tracked
    # "cloud model") so this page can never disagree with mode_status_result.
    active_model_id, _ = resolve_active_model_and_provider(ModeManager())
    fallback_model_id = _get_fallback_model_id()
    emergency_model_id = _get_emergency_model_id()

    local_cfgs = [m for m in cfgs if m.get("provider") == "local"]
    highest_local = max(local_cfgs, key=_get_model_params, default=None)

    return {
        "models": infos,
        "active_chat_model_id": active_model_id,
        "system_fallback_model_id": fallback_model_id,
        "emergency_model_id": emergency_model_id,
        "last_local_escalation": _routing_history.get_last_local_escalation(),
        "last_cloud_escalation": _routing_history.get_last_cloud_escalation(),
        "highest_available_local_model_id": highest_local.get("id") if highest_local else None,
    }


def list_models_cfgs() -> List[Dict[str, Any]]:
    # Thin indirection so get_diagnostics_models() reads as "the model
    # configs", not the full list_models() compat/speed bundle it
    # doesn't need — get_all_models() is the same registry function
    # list_models() itself iterates.
    from backend.core.model_registry import get_all_models
    return get_all_models()


# ---------------------------------------------------------------------------
# Batch 3 — weather/tool provider truth. Same REST/IPC parity convention
# as every diagnostics_* route above: computed independently here from
# the same backend.core sources of truth (weather_provider,
# weather_router, tool_router, connection_state, provider_config) rather
# than one transport calling the other — see backend/ipc_router.py's
# _handle_diagnostics_providers()/_handle_diagnostics_weather()/
# _handle_diagnostics_tools() for the IPC-side twins of these three.
# ---------------------------------------------------------------------------

@router.get("/diagnostics/providers")
def get_diagnostics_providers() -> Dict[str, Any]:
    """GET /v1/diagnostics/providers → every LLM provider plus the weather module, in one place."""
    status = weather_provider.get_weather_provider_status()
    return {
        "llm_providers": provider_config.get_provider_registry(),
        "weather_provider": {
            "name": status.weather_provider_name,
            "display_name": status.weather_provider_display_name,
            "enabled": status.enabled,
            "hasApiKey": status.isWeatherConfigured,
        },
    }


@router.get("/diagnostics/weather")
def _build_last_fusion_result(last_success: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Same reasoning as ipc_router.py's identical helper — built straight
    from WeatherPacket.to_dict()'s own `samples`, never re-derived."""
    if not last_success:
        return None
    samples = last_success.get("samples") or []
    return {
        "fusedTemperatureC": last_success.get("temperatureC"),
        "confidence": last_success.get("confidence"),
        "perProvider": [
            {"providerName": s.get("providerName"), "temperatureC": s.get("temperatureC"), "stationDistanceKm": s.get("stationDistanceKm")}
            for s in samples
        ],
    }


def get_diagnostics_weather() -> Dict[str, Any]:
    """GET /v1/diagnostics/weather → WeatherAPI's configuration truth, last real call, connection state,
    and (Batch 3.5) multi-provider fusion truth: fusionEnabled, providersAvailable, lastFusionResult."""
    status = weather_provider.get_weather_provider_status()
    conn = connection_state.get_status()
    history = weather_router.get_last_call_history()
    status_text = (
        f"{status.weather_provider_display_name}: Configured"
        if status.isWeatherConfigured
        else f"{status.weather_provider_display_name}: Not configured"
    )
    return {
        "weather_provider_name": status.weather_provider_name,
        "weather_provider_display_name": status.weather_provider_display_name,
        "isWeatherConfigured": status.isWeatherConfigured,
        "status_text": status_text,
        "last_success": history["last_success"],
        "last_error": history["last_error"],
        "backend_connected": conn.connected,
        "last_heartbeat_at": conn.last_heartbeat_at,
        "fusionEnabled": True,
        "providersAvailable": weather_fusion.providers_availability(),
        "lastFusionResult": _build_last_fusion_result(history["last_success"]),
    }


@router.get("/diagnostics/tools")
def get_diagnostics_tools() -> Dict[str, Any]:
    """GET /v1/diagnostics/tools → every registered tool's schema/location/provider status, last real call."""
    conn = connection_state.get_status()
    history = tool_router.get_last_call_history()

    tools = []
    for schema_dict in _tool_registry.list_tools():
        name = schema_dict["name"]
        provider_check = tool_router.TOOL_REQUIRED_PROVIDERS.get(name)
        tools.append({
            **schema_dict,
            "location": tool_router.TOOL_LOCATIONS.get(name, "local"),
            "provider_configured": provider_check() if provider_check is not None else None,
        })

    return {
        "tools": tools,
        "last_success": history["last_success"],
        "last_error": history["last_error"],
        "backend_connected": conn.connected,
        "last_heartbeat_at": conn.last_heartbeat_at,
    }


@router.get("/diagnostics/backend")
def get_diagnostics_backend() -> Dict[str, Any]:
    """GET /v1/diagnostics/backend → crash/freeze/restart observability (backend.core.backend_watchdog)."""
    return backend_watchdog.watchdog.get_snapshot()


@router.get("/diagnostics/cache")
def get_diagnostics_cache() -> Dict[str, Any]:
    """GET /v1/diagnostics/cache → requirements-cache health (backend.core.cache_manager)."""
    cache = _load_requirements_cache()
    return cache_manager.warm_up_summary(cache)


@router.get("/diagnostics/performance")
def get_diagnostics_performance(threshold_ms: float = perf_profiler.DEFAULT_SLOW_THRESHOLD_MS) -> Dict[str, Any]:
    """GET /v1/diagnostics/performance → instrumented-function timing summary + slow paths."""
    return {
        "summary": perf_profiler.get_summary(),
        "slow_paths": perf_profiler.get_slow_paths(threshold_ms=threshold_ms),
    }


@router.get("/diagnostics/transport")
def get_diagnostics_transport() -> Dict[str, Any]:
    """
    GET /v1/diagnostics/transport → this REST process's own identity/
    uptime. Deliberately doesn't report the separate WebSocket server's
    connection count — that process has no shared-memory link to this
    one (they're two independent OS processes; see backend/rest/server.py's
    module docstring), and fabricating a number here would be worse than
    not reporting one.
    """
    return {
        "rest_api": {"status": "ok", "uptime_seconds": round(time.monotonic() - _START_TIME, 3)},
        "note": "WebSocket server metrics are not available from this process — see backend/ws_server.py directly.",
    }


@router.get("/diagnostics/autobalance")
def get_diagnostics_autobalance() -> Dict[str, Any]:
    """
    GET /v1/diagnostics/autobalance → current CPU, active balance
    tier(s), live adjustments (threads/context/quant/chunk size), and
    recent throttle/restore history — see backend.core.auto_balancer.
    Scoped to local CPU-only inference sessions (any size class — see
    backend.core.auto_balancer.is_eligible()); empty active_sessions is
    the correct/expected response whenever no such session has run
    recently.
    """
    return _auto_balancer.diagnostics_snapshot()


@router.get("/diagnostics/routing")
def get_diagnostics_routing() -> Dict[str, Any]:
    """
    GET /v1/diagnostics/routing → recent local-model routing decisions
    (complexity, context length, tool use, selected model, reasoning
    tier) — see backend.core.complexity_router.routing_diagnostics_snapshot().
    Powers the webui's collapsible Routing Log panel.
    """
    return _complexity_router.routing_diagnostics_snapshot()


@router.get("/models/catalog")
def get_models_catalog() -> Dict[str, Any]:
    """
    GET /v1/models/catalog → not-yet-downloadable large local models
    (20B/30B/70B/120B/140B), each annotated with whether this machine's
    current pc_capability_tier can support it — see
    backend.core.model_catalog.list_catalog_entries().
    """
    return {"entries": _model_catalog.list_catalog_entries()}


@router.get("/diagnostics/streaming")
def get_diagnostics_streaming() -> Dict[str, Any]:
    """GET /v1/diagnostics/streaming → StreamingEngine capability/identity info."""
    return {
        "engine": type(_stream_engine).__name__,
        "mode": _stream_engine.mode_manager.get_mode(),
    }


# ---------------------------------------------------------------------------
# Metrics (Phase 2, item 6) — local-only, no network call anywhere in
# backend.core.metrics; see that module's docstring.
# ---------------------------------------------------------------------------

@router.get("/metrics")
def get_metrics() -> Dict[str, Any]:
    """GET /v1/metrics → tokens/sec, latency, throughput, memory, CPU/GPU utilization."""
    return _metrics.get_metrics_snapshot()


# ---------------------------------------------------------------------------
# Providers (Phase 2, item 1) — the unified ProviderInterface adapter
# over whatever backend.llm.providers.provider_registry already has
# registered; see backend/core/provider_interface.py.
# ---------------------------------------------------------------------------

@router.get("/providers/unified")
def get_unified_providers_list() -> Dict[str, Any]:
    """GET /v1/providers/unified → every provider_registry entry's unified metadata/diagnostics/requirements."""
    result = []
    for name in list_unified_providers():
        adapter = get_unified_provider(name)
        if adapter is None:
            continue
        result.append({
            "metadata": adapter.metadata(),
            "diagnostics": adapter.diagnostics(),
            "requirements": adapter.requirements(),
        })
    return {"providers": result}


@router.get("/providers/unified/{provider_name}")
def get_unified_provider_detail(provider_name: str) -> Dict[str, Any]:
    """GET /v1/providers/unified/{name} → one provider's unified metadata/diagnostics/requirements."""
    adapter = get_unified_provider(provider_name)
    if adapter is None:
        raise _rest_error(ipc_errors.GENERIC_ERROR, f"Unknown provider: {provider_name}", 404)
    return {
        "metadata": adapter.metadata(),
        "diagnostics": adapter.diagnostics(),
        "requirements": adapter.requirements(),
    }


# ---------------------------------------------------------------------------
# Tools (Phase 2, item 2) — backend.core.tool_registry.
# ---------------------------------------------------------------------------

@router.get("/tools")
def get_tools_list() -> Dict[str, Any]:
    """GET /v1/tools → every registered tool's schema."""
    return {"tools": _tool_registry.list_tools()}


class ToolExecuteRequest(BaseModel):
    args: Dict[str, Any] = {}


@router.post("/tools/{tool_name}/execute", dependencies=[Depends(require_api_key)])
def post_tool_execute(tool_name: str, payload: ToolExecuteRequest) -> Dict[str, Any]:
    """
    POST /v1/tools/{tool_name}/execute → run a registered tool through
    the sandboxed execution path (backend.core.tool_registry.execute_tool()).
    Requires X-ARIA-API-Key — tool execution can perform real network
    I/O (the built-in weather/search tools do), so this is gated the
    same way POST /v1/chat is.
    """
    # Batch 3: routed through backend.core.tool_router — REST has no
    # persistent-connection concept to be "disconnected" from (a request
    # is either received or it isn't), so connection_healthy stays at
    # its default True; tool_router still owns the required-provider
    # gate and the toolName/providerName/location/timestamp envelope.
    packet, router_error = tool_router.execute_tool_truthful(tool_name, payload.args)
    if router_error is not None:
        raise _rest_error(router_error.code, router_error.message, 503)
    if not packet["ok"]:
        status = 404 if packet["error_code"] == "TOOL_NOT_FOUND" else 400
        raise _rest_error(packet["error_code"] or ipc_errors.GENERIC_ERROR, packet["error"] or "Tool execution failed", status)
    return {"ok": True, "value": packet["value"], **{k: packet[k] for k in ("toolName", "providerName", "location", "timestamp")}}


# ---------------------------------------------------------------------------
# Plugins (Phase 2, item 3) — backend.core.plugin_registry.
# ---------------------------------------------------------------------------

@router.get("/plugins")
def get_plugins_list() -> Dict[str, Any]:
    """GET /v1/plugins → every loaded plugin's identity + which tools it registered."""
    return {"plugins": _plugin_registry.list_plugins(), "failed": _plugin_registry.list_failed_plugins()}


@router.get("/diagnostics/plugins")
def get_diagnostics_plugins() -> Dict[str, Any]:
    """GET /v1/diagnostics/plugins → aggregated get_diagnostics() from every loaded plugin."""
    return _plugin_registry.get_plugin_diagnostics()


# ---------------------------------------------------------------------------
# Chat + streaming (Phase 2)
#
# Request/response shapes, error codes, and short-circuit behavior are
# deliberately kept identical in spirit to backend/websocket/handlers.py's
# chat_request handling — "same model IDs, same tool invocation
# semantics, same error codes" per this phase's requirement — while using
# REST-idiomatic field names (snake_case, an explicit model_id field)
# rather than copying the WS wire format verbatim.
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    # Explicit model_id always wins (same precedence as WS/legacy REST) —
    # see _prepare_chat_turn(). Omit to let mode-aware resolution
    # (Auto/Local/Cloud, via ModeManager) pick, exactly as the WebSocket
    # path does for a chat_request with no modelId.
    model_id: Optional[str] = None
    history: Optional[List[ChatMessage]] = None
    multi_turn: bool = True
    # Same semantics as chat_request's skipSafetyCheck (backend/websocket/
    # handlers.py) and backend/server.py's ChatRequest.skip_safety_check —
    # "Do Not Show This Message Anymore This Session" from a client that
    # already saw and dismissed a safety_warning.
    skip_safety_check: bool = False


def _session_from_history(history: List[dict], mode_manager) -> SessionState:
    """The session snapshot, reconstructed from what the client sent back.

    This is the one place REST genuinely differs from the WebSocket path,
    and it is why the orchestrator takes a SessionState rather than
    reaching for a connection: a WebSocketHandler *is* the session, and a
    REST request has none, so the same state has to be recovered from
    `history` on every call.

    Two markers carry it, both of them text the backend itself wrote:
    the last assistant message being exactly the clarification prompt
    means a location reply is expected, and the "Weather for" prefix
    (weather_nl.format_weather_reply's own) means the previous turn was a
    weather answer and a correction phrase should reopen the window.

    connection_healthy is fixed True: REST has nothing to be disconnected
    from -- a request is either received or it is not. override_model_id
    stays None because "Proceed Anyway" is a per-connection grant with no
    REST equivalent; a REST client re-sends skip_safety_check instead.
    """
    last_assistant_text = next(
        (m.get("content", "") for m in reversed(history) if m.get("role") == "assistant"),
        "",
    ).strip()
    awaiting = last_assistant_text == weather_nl.CLARIFICATION_PROMPT

    return SessionState(
        mode=mode_manager.get_mode(),
        explicit_model_override=mode_manager.get_explicit_model_override(),
        awaiting_weather_location=awaiting,
        last_turn_was_weather=awaiting or last_assistant_text.startswith("Weather for"),
        connection_healthy=True,
        override_model_id=None,
    )


def _emit_turn_telemetry(telemetry: List[dict], conversation_id: Optional[str]) -> None:
    """Write the log lines the orchestrator described.

    Same mapping backend/websocket/handlers.py performs, with "rest" as
    the component -- the orchestrator names events, the transport decides
    what writing one means.
    """
    for record in telemetry or []:
        event = record.get("event")
        if event == "intent_detected":
            log_intent_detected("rest", record["intent"], conversation_id)
        elif event == "self_query_resolved":
            log_self_query_resolved("rest", record["intent"], record["snapshot"], conversation_id)
        elif event == "history_policy":
            log_history_policy("rest", record, conversation_id)
            log_system_prompt_applied("rest", conversation_id)
        elif event == "tool_routing_decision":
            log_tool_routing_decision("rest", record["decision"], conversation_id)
        else:
            unified_log("rest", "INFO", event, {
                k: v for k, v in record.items() if k not in ("event", "snapshot")
            } | {"conversation_id": conversation_id})


def _apply_model_switch(resolved: Dict[str, Any], mode_manager) -> Dict[str, Any]:
    """Perform a conversational model/mode/provider switch, REST-side.

    This is new for REST: _prepare_chat_turn used to ignore
    INTENT_MODEL_SWITCH outright, so "switch to mistral" reached a model
    as ordinary chat and nothing changed. The orchestrator resolves it for
    both transports now.

    The validation is not reimplemented here -- routing_guard is the same
    module backend/websocket/handlers.py calls, so a switch REST accepts is
    one the WebSocket path would accept and vice versa. What differs is
    only the announcement: the WebSocket path emits model_set_active_result
    / mode_set_result packets for a live UI, and REST returns text, because
    an HTTP caller has no socket to receive a packet on.
    """
    kind = resolved.get("kind")

    if kind == "model":
        model_id = resolved["model_id"]
        set_result = model_manager.set_active_model(model_id)
        if not set_result.get("ok"):
            return {"kind": "text", "model_id": "system",
                    "text": set_result.get("reason") or f"Could not switch to '{model_id}'."}
        switch = routing_guard.attempt_model_override_switch(mode_manager, model_id)
        if not switch.ok:
            return {"kind": "text", "model_id": "system",
                    "text": switch.error_message or f"Could not switch to '{model_id}'."}
        unified_log("rest", "INFO", "model_switch_intent: model", {"model_id": model_id})
        return {"kind": "text", "model_id": "system",
                "text": f"Switched to '{model_id}'. I'll keep using it until you tell me otherwise."}

    if kind in ("mode", "provider"):
        mode = "cloud" if kind == "provider" else resolved["mode"]
        provider = resolved.get("provider")
        switch = routing_guard.attempt_mode_switch(mode_manager, mode, provider=provider) \
            if provider else routing_guard.attempt_mode_switch(mode_manager, mode)
        if not switch.ok:
            return {"kind": "text", "model_id": "system",
                    "text": switch.error_message or f"Could not switch to {mode} mode."}
        unified_log("rest", "INFO", f"model_switch_intent: {kind}", {
            "mode": mode, "cloud_provider": switch.cloud_provider,
        })
        if mode == "automatic":
            text = ("Switched to automatic model selection — I'll pick local or cloud "
                    "per message based on what it needs.")
        elif mode == "local":
            text = (f"Switched to Local Mode, using {switch.display_name}. "
                    "I'll stay on local models until you tell me otherwise.")
        else:
            text = (f"Switched to Cloud Mode, using {switch.display_name} via "
                    f"{switch.cloud_provider}. I'll keep using it until you tell me otherwise.")
        return {"kind": "text", "model_id": "system", "text": text}

    logger.error("_apply_model_switch — unknown resolved kind: %r", resolved)
    return {"kind": "text", "model_id": "system", "text": "I couldn't work out what to switch to."}


async def _prepare_chat_turn(payload: ChatRequest, loop: asyncio.AbstractEventLoop) -> Dict[str, Any]:
    """
    Shared resolution step for POST /v1/chat and POST /v1/chat/stream --
    both need identical model/intent/safety resolution and differ only in
    how they consume the result (collect the full reply vs. stream tokens
    as they arrive), so this runs once and both routes branch on its
    "short_circuit" field.

    This used to be, in its own words, "a third independent orchestration
    of the same backend.core primitives ... because no such module exists
    today". That module exists now: backend/core/turn_orchestrator.py,
    which the WebSocket path already calls. Every decision that used to
    be made inline here -- the weather continuation window, the
    self-knowledge short-circuit, model precedence, absolute mode
    separation, the safety gate, history policy, the reasoning core --
    is made there, once, for both transports.

    What is left is what only REST can do: rebuild the session from
    `history` (see _session_from_history), write the log lines, apply the
    Cloud-Mode provider gate the WebSocket path has never had, and map a
    TurnResult onto the dict shape both routes already consume.

    orchestrate_turn is synchronous and some of what it calls blocks (a
    weather lookup, a web search, retrieval), so it runs through
    run_in_executor -- one wrapper at one call site, exactly as
    handlers.py does it.

    Returns a dict:
      {"short_circuit": {...} | None, "inference_request": InferenceRequest | None,
       "notices": [ {id, level, message, model_id}, ... ],
       "model_id": str | None, "conversation_id": str | None}

    short_circuit, when set, is one of:
      {"kind": "text", "model_id": <sentinel>, "text": <str>}          -- self-knowledge / weather / clarification
      {"kind": "safety_warning", "payload": {...}}                     -- safety gate tripped

    One deliberate behaviour change comes with the wiring: this used to
    ignore natural-language model switches ("switch to X" as a chat
    message), which fell through to ordinary chat. The orchestrator
    handles that intent, so REST now answers it and applies the pin --
    see the session_updates block below for why applying it is the only
    coherent option once the answer is being sent.
    """
    conversation_id = payload.session_id
    history = [m.dict() for m in (payload.history or [])]
    messages = history + [{"role": "user", "content": payload.message}]

    mode_manager = ModeManager()
    request = TurnRequest(
        messages=messages,
        latest_user_text=payload.message,
        conversation_id=conversation_id or "",
        multi_turn=payload.multi_turn,
        requested_model_id=payload.model_id,
        skip_safety_check=payload.skip_safety_check,
        session=_session_from_history(history, mode_manager),
    )

    result = await loop.run_in_executor(None, functools.partial(
        orchestrate_turn,
        request,
        mode_manager=mode_manager,
        suggester=_lighter_model_engine,
    ))

    _emit_turn_telemetry(result.telemetry, conversation_id)

    # REST holds no per-connection session, so session_updates have
    # nowhere to land here -- the weather continuation flags are
    # re-derived from `history` on the next request instead of being
    # stored. A model switch is not one of these: it is persistent state,
    # and _apply_model_switch below writes it through ModeManager.

    # A notice accompanies the turn rather than replacing it -- today,
    # the chat capability gate saying it moved this turn onto a model that
    # can follow the protocol. REST has no banner to push it to, so it
    # rides on the turn and is logged: an HTTP caller that gets an answer
    # from a model other than the one it named should be able to find out
    # why without reading the server's mind.
    for notice in result.notices:
        unified_log("rest", "INFO", notice.get("message", ""), dict(notice))

    def turn(short_circuit, model_id, inference_request=None):
        return {
            "short_circuit": short_circuit,
            "inference_request": inference_request,
            "model_id": model_id,
            "conversation_id": conversation_id,
            "notices": list(result.notices),
        }

    if result.kind == KIND_SAFETY_WARNING:
        # The orchestrator's packet carries a "type" key for the
        # WebSocket envelope; REST's routes add their own, and the SSE
        # branch sends this dict as the event body verbatim, so it is
        # dropped here rather than duplicated on the wire.
        payload_out = {k: v for k, v in result.warning.items() if k != "type"}
        return turn({"kind": "safety_warning", "payload": payload_out}, result.model_id)

    if result.kind == KIND_MODEL_SWITCH:
        return turn(_apply_model_switch(result.metadata["model_switch"], mode_manager),
                    "system")

    if result.kind == KIND_CLARIFY:
        # A fixed prompt, not a generated one -- "system" is the model_id
        # both transports have always used to mark that distinction.
        return turn({"kind": "text", "model_id": "system", "text": result.text}, "system")

    if result.kind == KIND_TEXT:
        return turn({"kind": "text", "model_id": result.model_id, "text": result.text},
                    result.model_id)

    if result.kind == KIND_ERROR:
        # Unreachable today: the only KIND_ERROR the orchestrator returns
        # is for an unhealthy connection, and connection_healthy is fixed
        # True above. Mapped anyway so a future source of KIND_ERROR
        # surfaces as an error rather than silently as an empty reply.
        raise _rest_error(tool_router.BACKEND_DISCONNECTED, result.text, 503)

    # KIND_INFERENCE.
    if result.model_id and get_model(result.model_id) is None:
        # REST-only: the WebSocket path lets ProviderRouter refuse an
        # unknown model, but an HTTP caller gets a 400 with the shared
        # error code instead of a stream that fails halfway.
        logger.warning("chat() referenced unknown model_id: %s", result.model_id)
        unified_log("rest", "ERROR", f"Unknown model_id: {result.model_id}", {"model_id": result.model_id})
        raise _rest_error(ipc_errors.UNKNOWN_MODEL, f"Unknown model_id '{result.model_id}'", 400)

    return turn(None, result.model_id, result.inference_request)


@router.post("/chat", dependencies=[Depends(require_api_key)])
async def post_chat(payload: ChatRequest) -> Dict[str, Any]:
    """
    POST /v1/chat → synchronous (non-streaming) completion.

    Request:  {"message": str, "session_id": str?, "model_id": str?,
               "history": [{"role","content"}]?, "multi_turn": bool = true,
               "skip_safety_check": bool = false}
    Response: {"reply": str, "model_id": str, "timestamp": float}
              — or, if the safety gate trips:
              {"type": "safety_warning", "model_id", "severity", "message",
               "projected": {"cpu","ram","vram"}, "suggestions": [...], "timestamp"}

    Requires X-ARIA-API-Key (see backend/rest/auth.py).
    """
    loop = asyncio.get_running_loop()
    turn = await _prepare_chat_turn(payload, loop)

    short_circuit = turn["short_circuit"]
    if short_circuit is not None:
        if short_circuit["kind"] == "safety_warning":
            return {**short_circuit["payload"], "type": "safety_warning", "timestamp": time.time()}
        return {"reply": short_circuit["text"], "model_id": short_circuit["model_id"], "timestamp": time.time()}

    packets: List[dict] = []
    try:
        # stream_engine.stream() blocks on real inference I/O/CPU work —
        # run off the event loop, same fix as backend/server.py's /chat
        # and backend/websocket/handlers.py's _stream_inference().
        await loop.run_in_executor(None, _stream_engine.stream, turn["inference_request"], packets.append)
    except Exception as e:
        logger.exception("post_chat() failed: %s", e)
        unified_log("rest", "ERROR", f"post_chat() exception: {e}", {"model_id": turn["model_id"]})
        raise _rest_error(ipc_errors.INFERENCE_ERROR, str(e), 500)

    error_packet = next((p for p in packets if p.get("type") == schema.STREAM_ERROR), None)
    if error_packet:
        unified_log("rest", "ERROR", f"post_chat() stream_error: {error_packet.get('message')}", {
            "model_id": turn["model_id"],
        })
        raise _rest_error(ipc_errors.INFERENCE_ERROR, error_packet.get("message") or "Streaming error", 500)

    raw_reply = "".join(p.get("token", "") for p in packets if p.get("type") == schema.STREAM_TOKEN)

    # Same filter the WebSocket path applies token by token. This route
    # buffers the whole reply before responding, so one pass over the
    # finished text is equivalent -- and optimize_response still runs
    # after it, exactly as before.
    _answer = AnswerStream()
    raw_reply = _answer.push(raw_reply) + _answer.finish()
    if any(_answer.stats.values()):
        unified_log("rest", "INFO", "answer_stream filtered model output", {
            **_answer.stats, "conversation_id": turn["conversation_id"],
        })

    reply, optimize_info = optimize_response(raw_reply)
    log_response_optimized("rest", optimize_info, turn["conversation_id"])

    # The full supervision pass, which only this route can run honestly:
    # /chat buffers the whole reply before responding, so the user has
    # not seen the unsupervised text and there is something left to fix.
    # The WebSocket path has already streamed its tokens by this point
    # and gets the deterministic half only -- see
    # backend/chat/supervisor_layer.py.
    if supervisor_layer.needs_supervision(turn["model_id"]):
        supervised = supervisor_layer.supervise_chat_output(
            reply,
            {"role": model_roles.describe_role(turn["model_id"])},
            generate=supervisor_layer.supervisor_generator(
                _stream_engine.mode_manager.get_mode()),
        )
        if supervised.changed:
            unified_log("rest", "INFO", "supervisor revised the reply", {
                "model_id": turn["model_id"], "repairs": supervised.repairs,
                "supervised": supervised.supervised, "rejected": supervised.rejected,
                "conversation_id": turn["conversation_id"],
            })
        reply = supervised.text

    unified_log("rest", "INFO", "Outgoing chat_response", {
        "model_id": turn["model_id"], "reply_len": len(reply),
    })
    return {"reply": reply, "model_id": turn["model_id"], "timestamp": time.time()}


@router.post("/chat/stream", dependencies=[Depends(require_api_key)])
async def post_chat_stream(payload: ChatRequest) -> StreamingResponse:
    """
    POST /v1/chat/stream → Server-Sent Events.

    Same request shape as POST /v1/chat. Each event's `event:` name is
    one of backend/ipc_schema.py's packet-type constants
    (stream_start/stream_token/stream_end/stream_error), and `data:` is
    that same packet shape StreamingEngine already emits over the
    WebSocket path ({"type","modelId","requestId",...}) — this reuses
    the existing streaming engine's actual packet objects verbatim
    rather than inventing a parallel SSE-only schema, so "consistent
    with WebSocket IPC semantics" holds literally, not just in spirit.
    A safety_warning short-circuit is sent as its own `event:
    safety_warning`, exactly mirroring the WS side never wrapping a
    safety_warning in a stream_start/stream_token/stream_end sequence.

    Requires X-ARIA-API-Key (see backend/rest/auth.py).
    """
    loop = asyncio.get_running_loop()
    turn = await _prepare_chat_turn(payload, loop)

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    async def event_generator():
        short_circuit = turn["short_circuit"]

        if short_circuit is not None:
            if short_circuit["kind"] == "safety_warning":
                yield sse("safety_warning", short_circuit["payload"])
                return

            # "text" short-circuit (self-knowledge / tool answer) — same
            # stream_start/stream_token/stream_end sequence handlers.py's
            # _answer_self_query_directly()/_answer_tool_query_directly()
            # send over the WS path, so chat.js-style clients need zero
            # special-casing to render it.
            request_id = id(short_circuit)
            model_id = short_circuit["model_id"]
            yield sse(schema.STREAM_START, {"type": schema.STREAM_START, "modelId": model_id, "requestId": request_id})
            yield sse(schema.STREAM_TOKEN, {"type": schema.STREAM_TOKEN, "modelId": model_id, "requestId": request_id, "token": short_circuit["text"]})
            yield sse(schema.STREAM_END, {"type": schema.STREAM_END, "modelId": model_id, "requestId": request_id})
            return

        # Real inference — same run_in_executor + asyncio.Queue bridge as
        # backend/server.py's existing /stream endpoint, so a blocking
        # provider call never freezes this ASGI worker's event loop.
        queue: asyncio.Queue = asyncio.Queue()
        _DONE = object()

        def on_packet(packet: dict):
            loop.call_soon_threadsafe(queue.put_nowait, packet)

        async def run_stream():
            try:
                await loop.run_in_executor(None, _stream_engine.stream, turn["inference_request"], on_packet)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _DONE)

        stream_task = asyncio.create_task(run_stream())

        try:
            while True:
                packet = await queue.get()
                if packet is _DONE:
                    break
                yield sse(packet.get("type", "message"), packet)
            await stream_task
        except Exception as e:
            logger.exception("post_chat_stream() event_generator failed: %s", e)
            unified_log("rest", "ERROR", f"post_chat_stream() exception: {e}", {"model_id": turn["model_id"]})
            yield sse(schema.STREAM_ERROR, {"type": schema.STREAM_ERROR, "message": str(e)})

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# VERSIONING — how /v2 would coexist alongside /v1 later
#
# This router is mounted under a fixed "/v1" prefix (see `router =
# APIRouter(prefix="/v1")` above) and included once, in
# backend/rest/server.py. A future /v2 should NOT edit these routes in
# place — it should live in a sibling module (e.g. backend/rest/router_v2.py)
# with its own `APIRouter(prefix="/v2")`, included alongside this one in
# server.py (`app.include_router(v1_router); app.include_router(v2_router)`),
# so both versions serve simultaneously and existing /v1 clients never
# see a breaking change. Candidates for a genuine /v2 rather than an
# in-place /v1 change:
#   - A different auth scheme (e.g. per-client issued tokens instead of
#     one shared key — see backend/rest/auth.py's Phase 2 limitations).
#   - A different streaming transport (e.g. WebSocket-over-HTTP/upgrade,
#     or chunked JSON instead of SSE) if a client ecosystem needs it.
#   - Request/response shape changes that aren't backward compatible
#     (e.g. restructuring "suggestions" or "projected" in the safety_warning
#     shape) — additive fields can still go straight into /v1 without a
#     version bump, per normal REST practice.
# ---------------------------------------------------------------------------

# TODO (Phase 3+): natural-language mode/model-switch intent handling
# (INTENT_MODEL_SWITCH — "switch to X" as a chat message), matching
# backend/websocket/handlers.py's _handle_model_switch_directly() and
# backend/server.py's _apply_model_switch(). Deliberately out of scope
# for Phase 2 — see _prepare_chat_turn()'s docstring.
#
# TODO (Phase 3+): wire backend/rest/server.py into
# AriaLauncher/BackendManager.cs as a first-class managed process (see
# that file's RestApiManager-style hook once added), rather than only
# being startable by hand.
#
# TODO (Phase 3+): decide the long-term relationship between this router
# and backend/server.py's existing /chat, /stream, /providers, /modules,
# /command routes on port 5000 — right now they're two independent,
# unmodified surfaces with overlapping purpose. See backend/rest/server.py's
# module docstring for the current thinking.
