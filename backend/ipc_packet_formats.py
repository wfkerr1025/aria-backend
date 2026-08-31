# backend/ipc_packet_formats.py

"""
Packet schemas for the model compatibility-system IPC surface.

Every packet on this WebSocket connection is JSON shaped as
{"type": <str>, "payload": <dict>} — that's what webui/core/bridge.js
sends (Bridge.send(type, payload) → {type, payload}) and what it
dispatches to listeners as (`packet.payload?.foo` throughout
model_requirements.js, model_performance.js and pages/models/models.js).
These builders exist so ipc_router.py never hand-assembles that
envelope inline — one place defines the wire shape for each result
type.

The one exception is `error_response`, which intentionally stays flat
({"type": "error", "message": ...}) to match the shape already used
elsewhere in backend/websocket/handlers.py for protocol-level errors.

Packet-type strings are pulled from backend/ipc_schema.py (see that
module's docstring) rather than hand-typed here — same wire values as
before, just centralized so a typo can't silently mismatch webui's
expectations.
"""

from __future__ import annotations
from typing import Any, Dict, List, Optional

from backend import ipc_schema as schema
from backend import ipc_errors


def _packet(ptype: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": ptype, "payload": payload}


# ---------------------------------------------------------------------------
# models_list_result
#   payload: { "models": [ {model_cfg, installed, active, compat,
#                            projected_speed_toksec}, ... ] }
#   (exactly what backend.core.model_manager.list_models() returns)
# ---------------------------------------------------------------------------

def models_list_result(models: List[Dict[str, Any]]) -> Dict[str, Any]:
    return _packet(schema.MODELS_LIST_RESULT, {"models": models})


# ---------------------------------------------------------------------------
# model_install_result / model_uninstall_result / model_set_active_result
#   payload: whatever backend.core.model_manager's install_model() /
#   uninstall_model() / set_active_model() already returned — those
#   dicts (ok, reason, compat, model_id, removed, ...) are passed
#   straight through as the payload so no field gets dropped or
#   renamed on the way out.
# ---------------------------------------------------------------------------

def model_install_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.MODEL_INSTALL_RESULT, result)


def model_uninstall_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.MODEL_UNINSTALL_RESULT, result)


def model_set_active_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.MODEL_SET_ACTIVE_RESULT, result)


def model_set_fallback_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.MODEL_SET_FALLBACK_RESULT, result)


def model_set_emergency_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.MODEL_SET_EMERGENCY_RESULT, result)


# ---------------------------------------------------------------------------
# model_requirements_result
#   payload: { "model_cfg": {...}, "compat": {...}, "installed": bool }
#   compat is backend.core.compatibility_checker.check_requirements()'s
#   return dict. installed tells model_requirements.js whether this
#   response should also open the pre-install popup (false) or just
#   refresh the inline panel for an already-installed model (true).
# ---------------------------------------------------------------------------

def model_requirements_result(model_cfg: Dict[str, Any], compat: Dict[str, Any], installed: bool = False) -> Dict[str, Any]:
    return _packet(schema.MODEL_REQUIREMENTS_RESULT, {"model_cfg": model_cfg, "compat": compat, "installed": installed})


# ---------------------------------------------------------------------------
# model_install_blocked
#   payload: { "model_cfg": {...}, "compat": {...} }
#   Sent instead of model_requirements_result when a not-yet-installed
#   model fails compat["meets_minimum"] — model_requirements.js renders
#   this as the blocking pre-install popup (Install disabled).
# ---------------------------------------------------------------------------

def model_install_blocked(model_cfg: Dict[str, Any], compat: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.MODEL_INSTALL_BLOCKED, {"model_cfg": model_cfg, "compat": compat})


# ---------------------------------------------------------------------------
# model_performance_result
#   payload: { "model_cfg": {...}, "projected_speed_toksec": float }
# ---------------------------------------------------------------------------

def model_performance_result(model_cfg: Dict[str, Any], projected_speed_toksec: float) -> Dict[str, Any]:
    return _packet(schema.MODEL_PERFORMANCE_RESULT, {
        "model_cfg": model_cfg,
        "projected_speed_toksec": projected_speed_toksec,
    })


# ---------------------------------------------------------------------------
# providers_list_result
#   payload: { "providers": [ {name, configured}, ... ] }
#   configured is a bool only — never the key value itself.
#   See backend.core.key_manager.list_configured_providers().
# ---------------------------------------------------------------------------

def providers_list_result(providers: List[Dict[str, Any]]) -> Dict[str, Any]:
    return _packet(schema.PROVIDERS_LIST_RESULT, {"providers": providers})


def provider_key_set_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.PROVIDER_KEY_SET_RESULT, result)


def provider_key_delete_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.PROVIDER_KEY_DELETE_RESULT, result)


# ---------------------------------------------------------------------------
# modules_list_result / module_key_set_result / module_key_delete_result
#   Same shape convention as the provider key packets above, for
#   backend.core.module_manager's weather/custom-module keys.
# ---------------------------------------------------------------------------

def plugin_registry_list_result(plugins: List[Dict[str, Any]]) -> Dict[str, Any]:
    return _packet(schema.PLUGIN_REGISTRY_LIST_RESULT, {"plugins": plugins})


def _plugin_choices(plugin: Dict[str, Any]) -> Dict[str, Any]:
    """The values this plugin's dropdown fields accept.

    Sent with the plugin so the page's options and the validator's
    accepted set are the same set. A list kept in the page instead would
    drift, and the way it would show is a dropdown whose selection the
    backend then refuses to save.
    """
    from backend.plugins.plugin_settings import FIELD_CHOICES

    table = FIELD_CHOICES.get(str(plugin.get("id") or ""), {})
    return {field: list(values) for field, values in table.items()}


def plugin_get_result(plugin: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.PLUGIN_GET_RESULT,
                   {"plugin": plugin, "choices": _plugin_choices(plugin)})


def plugin_update_result(plugin: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.PLUGIN_UPDATE_RESULT,
                   {"plugin": plugin, "saved": True,
                    "choices": _plugin_choices(plugin)})


def plugin_remove_result(plugin_id: str, removed: bool) -> Dict[str, Any]:
    return _packet(schema.PLUGIN_REMOVE_RESULT, {"id": plugin_id, "removed": removed})


def plugin_test_result(plugin_id: str, outcome: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.PLUGIN_TEST_RESULT, {"id": plugin_id, **outcome})


def plugin_discovery_result(outcome: Dict[str, Any]) -> Dict[str, Any]:
    """What a scan found, and what it did about it.

    "discovered" is the whole list the page should now show -- the task
    names that field, and it saves the page a second round trip. What
    was newly added, and what was passed over and why, ride along so
    the page can say "found Godot" or "you already have Unity" rather
    than silently redrawing.
    """
    return _packet(schema.PLUGIN_DISCOVERY_RESULT, {
        "discovered": outcome.get("discovered") or [],
        "found": outcome.get("found") or [],
        "added": outcome.get("added") or [],
        "skipped": outcome.get("skipped") or [],
        "error": outcome.get("error"),
    })


def modules_list_result(modules: List[Dict[str, Any]]) -> Dict[str, Any]:
    return _packet(schema.MODULES_LIST_RESULT, {"modules": modules})


def module_key_set_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.MODULE_KEY_SET_RESULT, result)


def module_key_delete_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.MODULE_KEY_DELETE_RESULT, result)


# ---------------------------------------------------------------------------
# mode_status_result
#   payload: { routing_mode, cloud_provider, explicit_model_override,
#              active_model_id }
#   See backend.core.mode_manager.ModeManager — powers the Models page's
#   mode/active-model/override indicator chips.
# ---------------------------------------------------------------------------

def mode_status_result(status: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.MODE_STATUS_RESULT, status)


# ---------------------------------------------------------------------------
# diagnostics_response
#   payload: engine-defined dict. See ipc_router._handle_diagnostics()
#   for what's actually populated in this build.
# ---------------------------------------------------------------------------

def diagnostics_response(data: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.DIAGNOSTICS_RESPONSE, data)


# ---------------------------------------------------------------------------
# diagnostics_hardware_result / diagnostics_cache_result /
# diagnostics_performance_result
#   Additive diagnostics surface — same payload shape as the matching
#   GET /v1/diagnostics/* REST route in backend/rest/router.py, since
#   both call the exact same backend.core function.
# ---------------------------------------------------------------------------

def diagnostics_hardware_result(data: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.DIAGNOSTICS_HARDWARE_RESULT, data)


def diagnostics_cache_result(data: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.DIAGNOSTICS_CACHE_RESULT, data)


def diagnostics_performance_result(data: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.DIAGNOSTICS_PERFORMANCE_RESULT, data)


# ---------------------------------------------------------------------------
# Phase 2 additive surface — metrics/tools/plugins/unified-providers.
# ---------------------------------------------------------------------------

def metrics_result(data: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.METRICS_RESULT, data)


def tools_list_result(tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    return _packet(schema.TOOLS_LIST_RESULT, {"tools": tools})


def tool_execute_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.TOOL_EXECUTE_RESULT, result)


def plugins_list_result(plugins: List[Dict[str, Any]], failed: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.PLUGINS_LIST_RESULT, {"plugins": plugins, "failed": failed})


def providers_unified_list_result(providers: List[Dict[str, Any]]) -> Dict[str, Any]:
    return _packet(schema.PROVIDERS_UNIFIED_LIST_RESULT, {"providers": providers})


def diagnostics_autobalance_result(data: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.DIAGNOSTICS_AUTOBALANCE_RESULT, data)


# ---------------------------------------------------------------------------
# diagnostics_routing_result
#   payload: backend.core.complexity_router.routing_diagnostics_snapshot()'s
#   return value — {"recent_decisions": [...]}. Powers the webui's
#   collapsible Routing Log panel.
# ---------------------------------------------------------------------------

def diagnostics_routing_result(data: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.DIAGNOSTICS_ROUTING_RESULT, data)


# ---------------------------------------------------------------------------
# diagnostics_models_result
#   payload: same shape as GET /v1/diagnostics/models (backend/rest/router.py)
#   — {"models": [...], "active_chat_model_id", "system_fallback_model_id",
#   "last_local_escalation", "last_cloud_escalation",
#   "highest_available_local_model_id"}. Powers the Models page's
#   dual-context Diagnostics block.
# ---------------------------------------------------------------------------

def diagnostics_models_result(data: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.DIAGNOSTICS_MODELS_RESULT, data)


# ---------------------------------------------------------------------------
# diagnostics_providers_result / diagnostics_weather_result /
# diagnostics_tools_result — Batch 3. payload shapes are built entirely
# by ipc_router.py's handlers (backend.core.provider_config,
# weather_provider, weather_router, tool_router, connection_state are
# the actual sources of truth); these are thin envelope wrappers, same
# convention as every other diagnostics_*_result above.
# ---------------------------------------------------------------------------

def workspace_list_result(workspaces: list) -> Dict[str, Any]:
    """Every registered project. The answer to every mutation, too."""
    return _packet(schema.WORKSPACE_LIST_RESULT, {"workspaces": workspaces})


def workspace_details_result(data: Dict[str, Any]) -> Dict[str, Any]:
    """One project, with its pending changes and their diffs."""
    return _packet(schema.WORKSPACE_DETAILS_RESULT, data)


def workspace_status_result(data: Dict[str, Any]) -> Dict[str, Any]:
    """What the working-directory panel renders.

    One packet for both the status request and the change request: a
    change is only interesting because of the state it produces, and
    returning that state means the panel never has to ask twice.
    """
    return _packet(schema.WORKSPACE_STATUS_RESULT, data)


def diagnostics_providers_result(data: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.DIAGNOSTICS_PROVIDERS_RESULT, data)


def diagnostics_weather_result(data: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.DIAGNOSTICS_WEATHER_RESULT, data)


def diagnostics_tools_result(data: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.DIAGNOSTICS_TOOLS_RESULT, data)


def diagnostics_backend_result(data: Dict[str, Any]) -> Dict[str, Any]:
    return _packet(schema.DIAGNOSTICS_BACKEND_RESULT, data)


# ---------------------------------------------------------------------------
# model_catalog_result
#   payload: { "entries": [...] } — backend.core.model_catalog.list_catalog_entries().
# ---------------------------------------------------------------------------

def model_catalog_result(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    return _packet(schema.MODEL_CATALOG_RESULT, {"entries": entries})


# ---------------------------------------------------------------------------
# heartbeat_ack
#   payload-less, flat packet: {"type": "heartbeat_ack", "ts": <echoed>}.
#   ts is whatever the client's heartbeat packet sent — this never
#   generates its own timestamp, so the client can measure round-trip
#   time directly against its own clock.
# ---------------------------------------------------------------------------

def heartbeat_ack(ts: Any = None) -> Dict[str, Any]:
    return {"type": schema.HEARTBEAT_ACK, "ts": ts}


# ---------------------------------------------------------------------------
# batch_response
#   payload: { "responses": [ <fully-formed packet>, ... ] }
#   One entry per sub-request in the batch_request, same order.
# ---------------------------------------------------------------------------

def batch_response(responses: List[Dict[str, Any]]) -> Dict[str, Any]:
    return _packet(schema.BATCH_RESPONSE, {"responses": responses})


# ---------------------------------------------------------------------------
# chat_response
#   Documented for completeness only — the live chat pipeline
#   (backend/websocket/handlers.py → ProviderRouter/StreamingEngine)
#   builds and sends its own packets and does not go through this
#   builder or through ipc_router.dispatch(). Provided so a future
#   caller has the schema without duplicating the wire shape logic.
# ---------------------------------------------------------------------------

def chat_response(text: str, **extra: Any) -> Dict[str, Any]:
    payload = {"text": text}
    payload.update(extra)
    return _packet(schema.CHAT_RESPONSE, payload)


# ---------------------------------------------------------------------------
# error
#   Flat on purpose — matches the shape backend/websocket/handlers.py
#   already sends for protocol-level errors (malformed JSON, unknown
#   packet type). `code` defaults to a generic code when the caller
#   doesn't have (or care about) a more specific one from
#   backend/ipc_errors.py — every existing call site that only passes
#   (message, request_type) keeps working unchanged.
# ---------------------------------------------------------------------------

def error_response(message: str, request_type: Optional[str] = None, code: str = ipc_errors.GENERIC_ERROR) -> Dict[str, Any]:
    return ipc_errors.build_error(code, message, request_type)
