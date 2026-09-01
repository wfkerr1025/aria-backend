# backend/ipc_router.py

"""
Central dispatcher for the model compatibility-system IPC surface
(models_list_request, model_install_request, model_uninstall_request,
model_set_active_request, model_set_fallback_request,
model_set_emergency_request, model_requirements_request,
model_performance_request) plus a best-effort diagnostics_request.

This does NOT replace backend/websocket/handlers.py's existing
chat_request handling — that has its own streaming pipeline
(ProviderRouter + StreamingEngine) and is intentionally left alone.
backend/websocket/handlers.py._dispatch() calls dispatch() below only
for the packet types this module actually owns.

Every handler here calls straight into the existing, unmodified
backend.core modules (model_manager, compatibility_checker,
performance_estimator, ...) and returns a fully-formed, JSON-ready
{"type", "payload"} dict via ipc_packet_formats — nothing here
recomputes logic those modules already own.
"""

from __future__ import annotations
from typing import Any, Callable, Dict, Optional

from backend.core.model_manager import (
    list_models,
    install_model,
    uninstall_model,
    set_active_model,
    set_fallback_model,
    set_emergency_model,
    is_model_installed,
)
from backend.core.model_registry import get_model, get_active_model_id, get_fallback_model_id, get_emergency_model_id
from backend.core.model_info import build_model_info
from backend.core.resource_monitor import get_resource_snapshot
from backend.core.compatibility_checker import check_requirements
from backend.core.safety_profiles import select_profile
from backend.core.performance_estimator import estimate_speed
from backend.core import key_manager, module_manager
from backend.core import hardware_snapshot_cache, cache_manager, perf_profiler
from backend.core.model_size_requirements import load_cache as load_requirements_cache
from backend.core import metrics as _metrics
from backend.core import tool_registry as _tool_registry
from backend.core import plugin_registry as _plugin_registry
from backend.core import auto_balancer as _auto_balancer
from backend.core import complexity_router as _complexity_router
from backend.core import model_catalog as _model_catalog
from backend.core import pc_capability_tier as _pc_capability_tier
from backend.core.provider_interface import get_unified_provider, list_unified_providers
from backend.core.mode_manager import ModeManager
from backend.core import self_knowledge
from backend.core import provider_config
from backend.core import model_selector
from backend.core import packet_validation
from backend.core import tool_router
from backend.core import weather_provider
from backend.core import weather_router
from backend.core import weather_fusion
from backend.core import connection_state
from backend.core import backend_watchdog
from backend.llm.providers.provider_registry import get_provider_display_name

from backend import ipc_packet_formats as fmt
from backend import ipc_schema as schema
from backend import ipc_errors
from backend.logger import log as unified_log

from logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Individual handlers — each takes the packet's "payload" dict and
# returns a fully-formed response packet.
# ---------------------------------------------------------------------------

def _handle_models_list(payload: Dict[str, Any]) -> Dict[str, Any]:
    logger.debug("ipc_router: models_list_request")
    models = list_models()
    return fmt.models_list_result(models)


def _handle_model_install(payload: Dict[str, Any]) -> Dict[str, Any]:
    model_id = payload.get("model_id")
    logger.debug(f"ipc_router: model_install_request → model_id={model_id}")

    if not model_id:
        return fmt.error_response("Missing model_id", "model_install_request")

    result = install_model(model_id)
    return fmt.model_install_result(result)


def _handle_model_uninstall(payload: Dict[str, Any]) -> Dict[str, Any]:
    model_id = payload.get("model_id")
    logger.debug(f"ipc_router: model_uninstall_request → model_id={model_id}")

    if not model_id:
        return fmt.error_response("Missing model_id", "model_uninstall_request")

    result = uninstall_model(model_id)
    return fmt.model_uninstall_result(result)


def _handle_model_set_active(payload: Dict[str, Any]) -> Dict[str, Any]:
    model_id = payload.get("model_id")
    logger.debug(f"ipc_router: model_set_active_request → model_id={model_id}")

    if not model_id:
        return fmt.error_response("Missing model_id", "model_set_active_request")

    result = set_active_model(model_id)
    return fmt.model_set_active_result(result)


def _handle_model_set_fallback(payload: Dict[str, Any]) -> Dict[str, Any]:
    model_id = payload.get("model_id")
    logger.debug(f"ipc_router: model_set_fallback_request → model_id={model_id}")

    if not model_id:
        return fmt.error_response("Missing model_id", "model_set_fallback_request")

    result = set_fallback_model(model_id)
    return fmt.model_set_fallback_result(result)


def _handle_model_set_emergency(payload: Dict[str, Any]) -> Dict[str, Any]:
    model_id = payload.get("model_id")
    logger.debug(f"ipc_router: model_set_emergency_request → model_id={model_id}")

    if not model_id:
        return fmt.error_response("Missing model_id", "model_set_emergency_request")

    result = set_emergency_model(model_id)
    return fmt.model_set_emergency_result(result)


def _handle_model_requirements(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Doubles as the pre-install gate: webui/components/model_requirements
    /model_requirements.js sends the exact same model_requirements_request
    both when a model's Details are viewed and when Install is clicked
    (there's no separate "gate" request type). So for a model that isn't
    installed yet, a failed meets_minimum check here means the *install*
    is blocked, not just that the requirements panel has bad news to
    render — hence model_install_blocked instead of the normal result.
    An already-installed model is never blocked (there's nothing to
    install), so it always gets the plain result.
    """
    model_id = payload.get("model_id")
    logger.debug(f"ipc_router: model_requirements_request → model_id={model_id}")

    if not model_id:
        return fmt.error_response("Missing model_id", "model_requirements_request")

    model_cfg = get_model(model_id)
    if model_cfg is None:
        return fmt.error_response(f"Unknown model ID: {model_id}", "model_requirements_request")

    snapshot = get_resource_snapshot()
    compat = check_requirements(snapshot, model_cfg)
    installed = is_model_installed(model_cfg)

    if not installed and not compat.get("meets_minimum"):
        logger.debug(f"ipc_router: model_requirements_request → model_id={model_id} install blocked")
        return fmt.model_install_blocked(model_cfg, compat)

    return fmt.model_requirements_result(model_cfg, compat, installed)


def _handle_model_performance(payload: Dict[str, Any]) -> Dict[str, Any]:
    model_id = payload.get("model_id")
    logger.debug(f"ipc_router: model_performance_request → model_id={model_id}")

    if not model_id:
        return fmt.error_response("Missing model_id", "model_performance_request")

    model_cfg = get_model(model_id)
    if model_cfg is None:
        return fmt.error_response(f"Unknown model ID: {model_id}", "model_performance_request")

    snapshot = get_resource_snapshot()
    profile = select_profile(snapshot, model_cfg)
    speed = estimate_speed(snapshot, model_cfg, profile)
    return fmt.model_performance_result(model_cfg, speed)


def _handle_diagnostics(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Best-effort diagnostics collection.

    The original diagnostics engine (backend/diagnostics/*) is absent
    from this working tree — it shows as deleted in git status, not
    something this task removed. Rather than guess at reconstructing
    that subsystem, this returns a live resource snapshot (the same
    data backing the safety/compatibility system) so the packet type
    still resolves to something real instead of an error.
    """
    logger.debug("ipc_router: diagnostics_request")

    snapshot = get_resource_snapshot()
    data = {
        "cpu_usage_pct": snapshot.cpu_usage,
        "ram_used_gb": round(snapshot.ram_used_gb, 2),
        "ram_total_gb": round(snapshot.ram_total_gb, 2),
        "vram_used_gb": round(snapshot.vram_used_gb, 2),
        "vram_total_gb": round(snapshot.vram_total_gb, 2),
        "unity_running": snapshot.unity_running,
    }
    return fmt.diagnostics_response(data)


def _handle_diagnostics_hardware(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Real-hardware classification (CPU/RAM/GPU/storage), sourced from the
    same TTL-cached hardware_snapshot_cache the model-compat pipeline
    uses. `payload.refresh: true` force-invalidates the cache first —
    the manual "re-detect my hardware" escape hatch for the rare case a
    GPU/drive actually changed mid-session.
    """
    logger.debug("ipc_router: diagnostics_hardware_request")
    if payload.get("refresh"):
        data = hardware_snapshot_cache.refresh()
    else:
        data = hardware_snapshot_cache.get_all()
    data = dict(data)
    data["cache_state"] = hardware_snapshot_cache.cache_state()
    data["pc_tier"] = _pc_capability_tier.get_pc_tier(data)
    return fmt.diagnostics_hardware_result(data)


def _handle_diagnostics_cache(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Requirements-cache health — see backend.core.cache_manager.warm_up_summary()."""
    logger.debug("ipc_router: diagnostics_cache_request")
    cache = load_requirements_cache()
    return fmt.diagnostics_cache_result(cache_manager.warm_up_summary(cache))


def _handle_diagnostics_performance(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Instrumented-function timing summary — see backend.core.perf_profiler.
    `payload.threshold_ms` overrides the default slow-path cutoff.
    """
    logger.debug("ipc_router: diagnostics_performance_request")
    threshold = payload.get("threshold_ms", perf_profiler.DEFAULT_SLOW_THRESHOLD_MS)
    return fmt.diagnostics_performance_result({
        "summary": perf_profiler.get_summary(),
        "slow_paths": perf_profiler.get_slow_paths(threshold_ms=threshold),
    })


def _handle_metrics(payload: Dict[str, Any]) -> Dict[str, Any]:
    logger.debug("ipc_router: metrics_request")
    return fmt.metrics_result(_metrics.get_metrics_snapshot())


def _handle_tools_list(payload: Dict[str, Any]) -> Dict[str, Any]:
    logger.debug("ipc_router: tools_list_request")
    return fmt.tools_list_result(_tool_registry.list_tools())


def _handle_tool_execute(payload: Dict[str, Any]) -> Dict[str, Any]:
    tool_name = payload.get("tool_name")
    args = payload.get("args") or {}
    logger.debug(f"ipc_router: tool_execute_request → tool_name={tool_name}")

    if not tool_name:
        return fmt.error_response("Missing tool_name", schema.TOOL_EXECUTE_REQUEST)

    # Batch 3: routed through backend.core.tool_router rather than
    # calling tool_registry.execute_tool() directly — the connection-
    # disconnect gate itself already ran one layer up, in
    # backend/websocket/handlers.py._dispatch() (before this function is
    # ever reached — see its own comment for why that check lives there
    # and not here), so this call always passes connection_healthy=True;
    # tool_router still owns the required-provider gate and the
    # toolName/providerName/location/timestamp envelope every tool
    # result must carry.
    packet, router_error = tool_router.execute_tool_truthful(tool_name, args)
    if router_error is not None:
        return fmt.error_response(router_error.message, schema.TOOL_EXECUTE_REQUEST, router_error.code)
    return fmt.tool_execute_result(packet)


def _handle_plugins_list(payload: Dict[str, Any]) -> Dict[str, Any]:
    logger.debug("ipc_router: plugins_list_request")
    return fmt.plugins_list_result(_plugin_registry.list_plugins(), _plugin_registry.list_failed_plugins())


def _handle_diagnostics_autobalance(payload: Dict[str, Any]) -> Dict[str, Any]:
    logger.debug("ipc_router: diagnostics_autobalance_request")
    return fmt.diagnostics_autobalance_result(_auto_balancer.diagnostics_snapshot())


def _handle_diagnostics_routing(payload: Dict[str, Any]) -> Dict[str, Any]:
    logger.debug("ipc_router: diagnostics_routing_request")
    return fmt.diagnostics_routing_result(_complexity_router.routing_diagnostics_snapshot())


def _handle_diagnostics_models(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Dual-context UI's Models-page Diagnostics block — same fields as
    GET /v1/diagnostics/models (backend/rest/router.py), computed
    independently here per this codebase's established REST/IPC
    convention (see that module's docstring) rather than one calling
    the other.
    """
    logger.debug("ipc_router: diagnostics_models_request")
    from backend.core.model_registry import get_all_models
    from backend.core import routing_history as _routing_history
    from backend.core.model_metadata import get_model_params as _get_model_params

    snapshot = get_resource_snapshot()
    cfgs = get_all_models()
    infos = [build_model_info(m, snapshot).to_dict() for m in cfgs]

    local_cfgs = [m for m in cfgs if m.get("provider") == "local"]
    highest_local = max(local_cfgs, key=_get_model_params, default=None)

    # Batch 1 stability fix — same mode-aware resolution as
    # _handle_mode_status() (see its own comment): this used to fall
    # back to the persisted LOCAL default regardless of routing_mode,
    # so Cloud Mode reported a real local model_id as "Active Chat
    # Model" here too.
    active_chat_model_id, _ = self_knowledge.resolve_active_model_and_provider(ModeManager())

    return fmt.diagnostics_models_result({
        "models": infos,
        "active_chat_model_id": active_chat_model_id,
        "system_fallback_model_id": get_fallback_model_id(),
        "emergency_model_id": get_emergency_model_id(),
        "last_local_escalation": _routing_history.get_last_local_escalation(),
        "last_cloud_escalation": _routing_history.get_last_cloud_escalation(),
        "highest_available_local_model_id": highest_local.get("id") if highest_local else None,
    })


def _handle_diagnostics_providers(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Batch 3 — consolidated provider truth: every LLM provider
    (backend.core.provider_config — Batch 1's single source of truth)
    plus the weather module (backend.core.weather_provider), so a
    diagnostics consumer never has to reconcile two separate registries
    to answer "what's configured right now".
    """
    logger.debug("ipc_router: diagnostics_providers_request")
    weather_status = weather_provider.get_weather_provider_status()
    return fmt.diagnostics_providers_result({
        "llm_providers": provider_config.get_provider_registry(),
        "weather_provider": {
            "name": weather_status.weather_provider_name,
            "display_name": weather_status.weather_provider_display_name,
            "enabled": weather_status.enabled,
            "hasApiKey": weather_status.isWeatherConfigured,
        },
    })


def _build_last_fusion_result(last_success: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Batch 3.5 — per-provider temperatures/distances for the last
    successful fusion, built straight from weather_router.WeatherPacket.
    to_dict()'s own `samples` list (never independently re-derived, so
    it can never claim a provider was used that the fusion result
    itself doesn't actually list).
    """
    if not last_success:
        return None
    samples = last_success.get("samples") or []
    return {
        "fusedTemperatureC": last_success.get("temperatureC"),
        "confidence": last_success.get("confidence"),
        "perProvider": [
            {
                "providerName": s.get("providerName"),
                "temperatureC": s.get("temperatureC"),
                "stationDistanceKm": s.get("stationDistanceKm"),
            }
            for s in samples
        ],
    }


def _handle_diagnostics_weather(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Batch 3 — "WeatherAPI: Not configured" (or its configured
    equivalent), plus the last real success/error
    backend.core.weather_router actually recorded, plus whether the
    backend currently has a live connection at all (from heartbeat —
    backend.core.connection_state). Never a guess: every field here
    comes from something that already happened.

    Batch 3.5 — also reports the multi-provider fusion engine's own
    truth: fusionEnabled (always true — weather_router.py no longer has
    a non-fusion path), providersAvailable (per-provider configured/
    reachable-in-principle status — backend.core.weather_fusion.
    providers_availability(), NEVER "was actually used just now" — a
    provider marked available here can still have failed or been
    skipped on the last real call; see lastFusionResult.perProvider for
    which ones actually contributed), and lastFusionResult (the last
    successful blend's per-provider breakdown).
    """
    logger.debug("ipc_router: diagnostics_weather_request")
    status = weather_provider.get_weather_provider_status()
    conn = connection_state.get_status()
    history = weather_router.get_last_call_history()

    status_text = (
        f"{status.weather_provider_display_name}: Configured"
        if status.isWeatherConfigured
        else f"{status.weather_provider_display_name}: Not configured"
    )

    return fmt.diagnostics_weather_result({
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
    })


def _handle_diagnostics_tools(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Batch 3 — every registered tool's schema alongside whether its
    required provider (if any) is configured, plus the last real
    success/error backend.core.tool_router actually recorded, plus
    backend connection truth (same source as diagnostics_weather_result).
    """
    logger.debug("ipc_router: diagnostics_tools_request")
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

    return fmt.diagnostics_tools_result({
        "tools": tools,
        "last_success": history["last_success"],
        "last_error": history["last_error"],
        "backend_connected": conn.connected,
        "last_heartbeat_at": conn.last_heartbeat_at,
    })


def _handle_diagnostics_backend(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Batch 4 — backend crash/freeze/restart observability
    (backend.core.backend_watchdog). Pushed unsolicited to every newly
    (re)connected client (see backend/websocket/handlers.py's handle())
    in addition to being requestable on demand here.
    """
    logger.debug("ipc_router: diagnostics_backend_request")
    return fmt.diagnostics_backend_result(backend_watchdog.watchdog.get_snapshot())


def _handle_model_catalog(payload: Dict[str, Any]) -> Dict[str, Any]:
    logger.debug("ipc_router: model_catalog_request")
    return fmt.model_catalog_result(_model_catalog.list_catalog_entries())


def _handle_providers_unified_list(payload: Dict[str, Any]) -> Dict[str, Any]:
    logger.debug("ipc_router: providers_unified_list_request")
    result = []
    for name in list_unified_providers():
        adapter = get_unified_provider(name)
        if adapter is None:
            continue
        result.append({"metadata": adapter.metadata(), "diagnostics": adapter.diagnostics(), "requirements": adapter.requirements()})
    return fmt.providers_unified_list_result(result)


# ---------------------------------------------------------------------------
# Provider / module key management (backend.core.key_manager,
# backend.core.module_manager) — see Part 4/5 of the settings-UI work.
# Never returns a key value in any response; only "configured": bool.
# ---------------------------------------------------------------------------

def _handle_mode_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    logger.debug("ipc_router: mode_status_request")
    mm = ModeManager()

    cloud_provider = mm.get_cloud_provider()
    # FIXED (Batch 1 stability fix): this used to unconditionally fall
    # back to get_active_model_id() — the persisted LOCAL default —
    # whenever there was no explicit override, regardless of
    # routing_mode. In Cloud Mode that reported a real local model_id
    # (e.g. "nemo-12b-q5") alongside routing_mode="cloud": the exact
    # "Cloud Mode + nemo-12b-q5" contradiction. self_knowledge.
    # resolve_active_model_and_provider() already solved this correctly
    # for the self-query path (mode-aware: None in Cloud Mode, since
    # there is no single tracked "cloud model" — the provider picks its
    # own model per request); reused here so mode_status_result and
    # self-query answers can never disagree.
    active_model_id, _resolved_provider = self_knowledge.resolve_active_model_and_provider(mm)
    active_model_cfg = get_model(active_model_id) if active_model_id else None
    active_model_display_name = active_model_cfg.get("name") if active_model_cfg else None
    if active_model_display_name is None and active_model_id and _resolved_provider:
        # Batch 2: active_model_id may be a cloud model id
        # (backend.core.model_selector, e.g. "gpt-4") — unknown to
        # model_registry (a local-only registry) — so fall back to the
        # selector for its display name instead of leaving it null.
        selection = model_selector.select_cloud_model(_resolved_provider)
        if isinstance(selection, model_selector.ModelInfo) and selection.model_id == active_model_id:
            active_model_display_name = selection.display_name
    location = {"cloud": "cloud", "local": "local"}.get(mm.get_mode())

    # "System Model" in the dual-context UI spec — the registry's
    # fallback role, distinct from whatever's actually serving the
    # current turn (active_model_id above, or active_model_changed's
    # per-turn value). Additive fields: existing consumers (models.js's
    # mode bar) that only read the pre-existing keys are unaffected.
    fallback_model_id = get_fallback_model_id()
    fallback_model_cfg = get_model(fallback_model_id) if fallback_model_id else None

    # "Emergency Model" — the registry's last-resort role (see
    # backend.core.complexity_router's own fallback chain and
    # backend.core.model_registry.get_emergency_model_id()). Additive
    # field, same reasoning as fallback_model_id above.
    emergency_model_id = get_emergency_model_id()
    emergency_model_cfg = get_model(emergency_model_id) if emergency_model_id else None

    status = {
        "routing_mode": mm.get_mode(),
        "cloud_provider": cloud_provider,
        # Display-name fields — "OpenAI" and "Mistral Nemo 12B Instruct
        # (Q5_K_M)", not "openai"/"nemo-12b-q5" — so the frontend never
        # needs its own copy of this formatting (single source of truth:
        # backend.llm.providers.provider_registry.PROVIDER_DISPLAY_NAMES).
        "cloud_provider_display_name": get_provider_display_name(cloud_provider),
        "explicit_model_override": mm.get_explicit_model_override(),
        # FIXED: this used to call get_active_model_id() again here,
        # ignoring the override precedence already resolved into
        # `active_model_id` above (the one active_model_display_name
        # was already built from) — so a "switch to X" pin showed the
        # right NAME but the wrong ID, and any consumer keying off the
        # id field alone (or falling back to it when display_name is
        # absent) would silently show the pre-override default/fallback
        # model instead of what's actually pinned.
        "active_model_id": active_model_id,
        "active_model_display_name": active_model_display_name,
        "fallback_model_id": fallback_model_id,
        "fallback_model_display_name": fallback_model_cfg.get("name") if fallback_model_cfg else None,
        "emergency_model_id": emergency_model_id,
        "emergency_model_display_name": emergency_model_cfg.get("name") if emergency_model_cfg else None,
        # Additive — parallels active_model_changed's own "location"
        # field ("local"/"cloud"); None under Automatic Model Routing,
        # which has no single fixed location (see routing_invariants.py).
        "location": location,
    }

    # Batch 1 stability fix — validate before this ever reaches the
    # wire. Should never actually fire (ModeManager self-heals invalid
    # persisted state on load — see mode_manager.py — and routing_guard
    # is the only path that can change it afterward), but a malformed/
    # contradictory mode_status_result is exactly the kind of packet
    # that has crashed frontend consumers before, so this is real
    # defense-in-depth, not decoration.
    validation_error = packet_validation.validate_mode_status_payload(status)
    if validation_error:
        logger.error(f"ipc_router: mode_status_result failed validation — {validation_error}")
        unified_log("ipc_router", "ERROR", "mode_status_result blocked by invariant validation", {
            "error": validation_error, "status": status,
        })
        return fmt.error_response(validation_error, schema.MODE_STATUS_REQUEST, ipc_errors.ROUTING_INVARIANT_VIOLATION)

    return fmt.mode_status_result(status)


def _handle_providers_list(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Batch 1 stability fix — reflects backend.core.provider_config's
    registry (backend/config/provider_config.json), the single source of
    truth for provider identity/enablement/metadata, rather than reading
    key_manager directly. "configured" is kept as its own field (not
    renamed to "hasApiKey") for backward compatibility with existing
    frontend consumers (webui/pages/models/models.js); enabled/metadata
    are additive.
    """
    logger.debug("ipc_router: providers_list_request")
    providers = [
        {
            "name": entry["name"],
            "configured": entry["hasApiKey"],
            "enabled": entry["enabled"],
            "metadata": entry["metadata"],
        }
        for entry in provider_config.get_provider_registry()
    ]
    return fmt.providers_list_result(providers)


def _handle_provider_key_set(payload: Dict[str, Any]) -> Dict[str, Any]:
    provider = payload.get("provider")
    api_key = payload.get("api_key")
    logger.debug(f"ipc_router: provider_key_set_request → provider={provider}")

    if not provider or not api_key:
        return fmt.error_response("Missing provider or api_key", "provider_key_set_request")

    try:
        ok = key_manager.set_provider_key(provider, api_key)
    except Exception as e:
        # If the OS keyring backend itself is unavailable/broken (locked
        # Credential Manager, missing libsecret, etc.) this must still
        # come back as a provider_key_set_result the frontend's existing
        # PROVIDER_KEY_SET_RESULT listener recognizes — a generic "error"
        # packet from the outer dispatch() handler would silently fail to
        # update the Save button's toast/badge (see webui/pages/models/
        # models.js's handleKeyActionResult()), reading as "did nothing".
        logger.exception(f"ipc_router: set_provider_key raised for provider={provider}: {e}")
        return fmt.provider_key_set_result({"ok": False, "reason": f"Could not save key: {e}"})

    if not ok:
        return fmt.provider_key_set_result({"ok": False, "reason": f"Unknown provider or empty key: {provider}"})
    return fmt.provider_key_set_result({"ok": True, "provider": provider})


def _handle_provider_key_delete(payload: Dict[str, Any]) -> Dict[str, Any]:
    provider = payload.get("provider")
    logger.debug(f"ipc_router: provider_key_delete_request → provider={provider}")

    if not provider:
        return fmt.error_response("Missing provider", "provider_key_delete_request")

    ok = key_manager.delete_provider_key(provider)
    if not ok:
        return fmt.provider_key_delete_result({"ok": False, "reason": f"Unknown provider: {provider}"})
    return fmt.provider_key_delete_result({"ok": True, "provider": provider})


def _workspace_error(error, request_type):
    """A refusal the user sees, rather than a silent no-op.

    Every handler here can be told to act on a workspace that does not
    exist or a directory that is not one. Answering with the unchanged
    list would look exactly like success.
    """
    return fmt.error_response(str(error), request_type)


def _handle_workspace_list(payload: Dict[str, Any]) -> Dict[str, Any]:
    logger.debug("ipc_router: workspace_list_request")
    from backend.core import workspace_manager

    workspace_manager.ensure_default_workspace()
    return fmt.workspace_list_result(workspace_manager.get_workspace_list())


def _handle_workspace_details(payload: Dict[str, Any]) -> Dict[str, Any]:
    from backend.core import workspace_manager

    workspace_id = payload.get("id")
    logger.debug("ipc_router: workspace_details_request -> %r", workspace_id)
    try:
        return fmt.workspace_details_result(
            workspace_manager.get_workspace_details(workspace_id))
    except workspace_manager.WorkspaceError as error:
        return _workspace_error(error, schema.WORKSPACE_DETAILS_REQUEST)


def _handle_workspace_add(payload: Dict[str, Any]) -> Dict[str, Any]:
    from backend.core import workspace_manager

    path = payload.get("path")
    logger.info("ipc_router: workspace_add_request -> %r", path)
    try:
        workspace_manager.add_workspace(path, payload.get("name"))
    except workspace_manager.WorkspaceError as error:
        return _workspace_error(error, schema.WORKSPACE_ADD_REQUEST)

    return fmt.workspace_list_result(workspace_manager.get_workspace_list())


def _handle_workspace_remove(payload: Dict[str, Any]) -> Dict[str, Any]:
    from backend.core import workspace_manager

    workspace_id = payload.get("id")
    logger.info("ipc_router: workspace_remove_request -> %r", workspace_id)

    # Bookkeeping, never a delete: the project's files and its staged
    # changes are left exactly where they are.
    workspace_manager.remove_workspace(workspace_id)
    return fmt.workspace_list_result(workspace_manager.get_workspace_list())


def _handle_workspace_primary(payload: Dict[str, Any]) -> Dict[str, Any]:
    from backend.core import workspace_manager

    workspace_id = payload.get("id")
    logger.info("ipc_router: workspace_primary_request -> %r", workspace_id)
    try:
        workspace_manager.set_primary_workspace(workspace_id)
    except workspace_manager.WorkspaceError as error:
        return _workspace_error(error, schema.WORKSPACE_PRIMARY_REQUEST)

    return fmt.workspace_list_result(workspace_manager.get_workspace_list())


def _workspace_action(payload, request_type, action):
    """Commit, discard or rollback, then report the workspace's new state.

    The consent text travels with the request and is checked inside
    ghost_workspace, not here: one negation table, one place that decides
    whether the user asked. A refusal comes back as a normal result whose
    report says "refused", because being told no is an outcome rather
    than an error.
    """
    from backend.core import workspace_manager

    workspace_id = payload.get("id")
    files = payload.get("files") or None

    try:
        report = action(workspace_manager, workspace_id, files)
        details = workspace_manager.get_workspace_details(workspace_id)
    except workspace_manager.WorkspaceError as error:
        return _workspace_error(error, request_type)

    details["report"] = report
    return fmt.workspace_details_result(details)


def _handle_workspace_commit(payload: Dict[str, Any], on_progress=None) -> Dict[str, Any]:
    logger.info("ipc_router: workspace_commit_request -> %r", payload.get("id"))
    return _workspace_action(
        payload, schema.WORKSPACE_COMMIT_REQUEST,
        lambda wm, wid, files: wm.commit_workspace(
            wid, str(payload.get("user_text") or ""), files,
            on_progress=on_progress),
    )


def _handle_workspace_discard(payload: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("ipc_router: workspace_discard_request -> %r", payload.get("id"))
    return _workspace_action(
        payload, schema.WORKSPACE_DISCARD_REQUEST,
        lambda wm, wid, files: wm.discard_workspace(
            wid, str(payload.get("user_text") or ""), files),
    )


def _handle_workspace_rollback(payload: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("ipc_router: workspace_rollback_request -> %r", payload.get("id"))
    return _workspace_action(
        payload, schema.WORKSPACE_ROLLBACK_REQUEST,
        lambda wm, wid, files: wm.rollback_workspace(wid, files),
    )


def _handle_workspace_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    logger.debug("ipc_router: workspace_status_request")
    from backend.core import workspace_manager

    return fmt.workspace_status_result(workspace_manager.describe_workspace())


def _handle_workspace_set(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Point ARIA at a different project.

    Returns the same shape as a status request, so the panel renders one
    response either way and never has to ask twice for the state its own
    change produced.

    A rejected path is an error the user sees, not a silent no-op: the
    whole point of a visible working directory is that it says where
    ARIA actually is.
    """
    path = payload.get("path")
    logger.info("ipc_router: workspace_set_request -> %r", path)

    from backend.core import workspace_manager

    try:
        workspace_manager.set_project_root(path)
    except workspace_manager.WorkspaceError as error:
        return fmt.error_response(str(error), schema.WORKSPACE_SET_REQUEST)

    return fmt.workspace_status_result(workspace_manager.describe_workspace())


# ======================================================
# Plugins
# ======================================================
#
# The integrations a user installs and configures. Every one of these
# reads aria_config/plugins.json through plugin_manager, so the page and
# the backend cannot hold different opinions about what is installed.
#
# Nothing here is reachable by a model. These are UI packets, and a
# plugin's settings are the user's to change.
def _handle_plugin_registry_list(payload: Dict[str, Any]) -> Dict[str, Any]:
    logger.debug("ipc_router: plugin_registry_list_request")
    from backend.plugins import plugin_settings

    return fmt.plugin_registry_list_result(plugin_settings.list_plugins())


def _handle_plugin_get(payload: Dict[str, Any]) -> Dict[str, Any]:
    from backend.plugins import plugin_settings

    plugin_id = str(payload.get("id") or "")
    config_page = str(payload.get("configPage") or "")
    logger.debug("ipc_router: plugin_get_request -> id=%r page=%r",
                 plugin_id, config_page)

    # A page may address itself either way. It knows its own route for
    # certain; it can only guess at an id, and a route is not always an
    # id with the punctuation changed.
    try:
        if plugin_id:
            plugin = plugin_settings.get_plugin(plugin_id)
        else:
            plugin = plugin_settings.get_plugin_by_config_page(config_page)
    except plugin_settings.PluginError as error:
        return fmt.error_response(str(error), schema.PLUGIN_GET_REQUEST)

    # Redacted, because this answer goes to a page. The config page shows
    # whether a key is set, never the key.
    return fmt.plugin_get_result(plugin_settings.redact_secrets(plugin))


def _handle_plugin_update(payload: Dict[str, Any]) -> Dict[str, Any]:
    from backend.plugins import plugin_settings

    plugin_id = str(payload.get("id") or "")
    fields = payload.get("fields")
    logger.debug("ipc_router: plugin_update_request -> %r", plugin_id)

    if not plugin_id:
        return fmt.error_response("Missing plugin id", schema.PLUGIN_UPDATE_REQUEST)
    if not isinstance(fields, dict):
        return fmt.error_response("fields must be an object",
                                  schema.PLUGIN_UPDATE_REQUEST)

    try:
        updated = plugin_settings.update_plugin(plugin_id, fields)
    except plugin_settings.PluginError as error:
        # A validation failure is an answer for the form to show, not an
        # error the page should treat as a broken connection.
        return fmt.error_response(str(error), schema.PLUGIN_UPDATE_REQUEST)
    except Exception as error:  # pragma: no cover - a write fault
        logger.exception("could not update plugin %s", plugin_id)
        return fmt.error_response(f"Could not save: {error}",
                                  schema.PLUGIN_UPDATE_REQUEST)

    # Enabling or disabling the Unity CLI changes which tools a model
    # may call, and a user who has just switched it on should not have
    # to restart ARIA for that to be true.
    try:
        _tool_registry.register_unity_cli_tools()
    except Exception:  # pragma: no cover - a tool refresh is not a save
        logger.exception("could not refresh the Unity CLI tools")

    return fmt.plugin_update_result(updated)


def _handle_plugin_remove(payload: Dict[str, Any]) -> Dict[str, Any]:
    from backend.plugins import plugin_settings

    plugin_id = str(payload.get("id") or "")
    logger.info("ipc_router: plugin_remove_request -> %r", plugin_id)

    if not plugin_id:
        return fmt.error_response("Missing plugin id", schema.PLUGIN_REMOVE_REQUEST)

    try:
        removed = plugin_settings.remove_plugin(plugin_id)
    except Exception as error:  # pragma: no cover - a write fault
        logger.exception("could not remove plugin %s", plugin_id)
        return fmt.error_response(f"Could not remove: {error}",
                                  schema.PLUGIN_REMOVE_REQUEST)

    return fmt.plugin_remove_result(plugin_id, removed)


def _handle_plugin_test(payload: Dict[str, Any]) -> Dict[str, Any]:
    from backend.plugins import plugin_settings

    plugin_id = str(payload.get("id") or "")
    logger.debug("ipc_router: plugin_test_request -> %r", plugin_id)

    if not plugin_id:
        return fmt.error_response("Missing plugin id", schema.PLUGIN_TEST_REQUEST)

    return fmt.plugin_test_result(plugin_id,
                                  plugin_settings.test_plugin_connection(plugin_id))


def _handle_plugin_discovery(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Scan the machine for integrations ARIA could use.

    Runs when the Plugins page opens, and again when the user presses
    Rescan. force=True is the button: it reconsiders plugins the user
    previously removed, which the automatic pass deliberately does not.

    This touches the filesystem outside the project -- it is the one
    thing in this file that does -- and it is read-only: it globs a few
    known install directories and stats what it finds. It never runs a
    program it discovers.
    """
    from backend.plugins import plugin_settings

    force = bool(payload.get("force", False))
    logger.info("ipc_router: plugin_discovery_request (force=%s)", force)

    return fmt.plugin_discovery_result(plugin_settings.discover_plugins(force=force))


def _handle_unity_cli_commands(payload: Dict[str, Any]) -> Dict[str, Any]:
    """List the Unity CLI commands already in the registry.

    Reads only. Listing what is registered must not run the CLI --
    that is what the refresh below is for, and keeping them separate is
    what lets the Plugins page show the command tiles on every visit
    without starting a process each time.
    """
    from backend.plugins import plugin_settings
    from backend.unity import unity_cli_engine as engine

    logger.debug("ipc_router: unity_cli_commands_request")

    # Whether the section should exist at all is a different question
    # from whether it has anything in it, and the page needs both.
    plugin = plugin_settings.load_plugins().get(engine.PLUGIN_ID) or {}
    return fmt.unity_cli_commands_result({
        "commands": plugin_settings.list_commands(engine.PLUGIN_ID),
        "available": bool(plugin.get("enabled")) and not plugin.get("dismissed"),
    })


def _handle_unity_cli_refresh(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Ask the Unity CLI what commands it has, and merge the answer.

    This one does run the CLI, so it happens when a person presses
    Refresh. force=True additionally reconsiders commands they removed,
    exactly as the plugin Rescan button does.
    """
    from backend.plugins import plugin_settings

    force = bool(payload.get("force", False))
    logger.info("ipc_router: unity_cli_refresh_request (force=%s)", force)

    return fmt.unity_cli_commands_result(
        plugin_settings.refresh_unity_cli_commands(force=force))


def _handle_unity_cli_command(payload: Dict[str, Any],
                              on_progress=None) -> Dict[str, Any]:
    """Run one Unity CLI command, streaming its output as it arrives.

    on_progress here carries dicts rather than strings -- whole packets
    for the terminal view. A build's output is not chat commentary and
    must not land in the transcript.

    Nothing about this is reachable by a model. It runs because a
    person pressed Run on a command they had already enabled.
    """
    from backend.unity import unity_cli_engine as engine

    command_id = str(payload.get("id") or "")
    args = payload.get("args") or []
    logger.info("ipc_router: unity_cli_command_request -> %r", command_id)

    if not command_id:
        return fmt.error_response("Missing command id", schema.UNITY_CLI_COMMAND_REQUEST)
    if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        return fmt.error_response("args must be a list of strings",
                                  schema.UNITY_CLI_COMMAND_REQUEST)

    def stream(kind: str, line: str) -> None:
        if on_progress is None:
            return
        try:
            on_progress(fmt.unity_cli_output(command_id, kind, line))
        except Exception:  # pragma: no cover - a viewer is not the job
            logger.debug("could not stream a Unity CLI line", exc_info=True)

    try:
        outcome = engine.run_command(command_id, args, on_output=stream)
    except Exception as error:  # pragma: no cover - the engine catches its own
        logger.exception("running %s failed", command_id)
        outcome = {"success": False, "output": "", "json": None,
                   "error": f"The command could not be run: {error}"}

    return fmt.unity_cli_command_result(command_id, outcome)


def _handle_modules_list(payload: Dict[str, Any]) -> Dict[str, Any]:
    logger.debug("ipc_router: modules_list_request")
    return fmt.modules_list_result(module_manager.list_modules())


def _handle_module_key_set(payload: Dict[str, Any]) -> Dict[str, Any]:
    module_name = payload.get("module")
    api_key = payload.get("api_key")
    logger.debug(f"ipc_router: module_key_set_request → module={module_name}")

    if not module_name or not api_key:
        return fmt.error_response("Missing module or api_key", "module_key_set_request")

    try:
        result = module_manager.set_module_key(module_name, api_key)
    except Exception as e:
        # Same rationale as _handle_provider_key_set() above: a failure
        # writing/encrypting backend/config/module_keys.enc.json (disk
        # permissions, missing master key, etc.) must still come back as
        # a module_key_set_result, not a generic "error" packet.
        logger.exception(f"ipc_router: set_module_key raised for module={module_name}: {e}")
        return fmt.module_key_set_result({"ok": False, "module": module_name, "reason": f"Could not save key: {e}"})

    return fmt.module_key_set_result(result)


def _handle_module_key_delete(payload: Dict[str, Any]) -> Dict[str, Any]:
    module_name = payload.get("module")
    logger.debug(f"ipc_router: module_key_delete_request → module={module_name}")

    if not module_name:
        return fmt.error_response("Missing module", "module_key_delete_request")

    result = module_manager.delete_module_key(module_name)
    return fmt.module_key_delete_result(result)


# ---------------------------------------------------------------------------
# Transport-level QoL handlers — heartbeat keepalive and request batching.
# Added alongside the model-compat handlers above rather than in
# backend/websocket/handlers.py, matching this module's existing role as
# "where new packet types get a handler" (see module docstring).
# ---------------------------------------------------------------------------

def _handle_heartbeat(payload: Dict[str, Any]) -> Dict[str, Any]:
    ts = payload.get("ts")
    logger.debug(f"ipc_router: heartbeat ts={ts}")
    return fmt.heartbeat_ack(ts)


def _handle_batch_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Runs each sub-packet in payload["requests"] through this same
    dispatch() and collects the results in order. Only packet types
    already in HANDLED_TYPES may run this way — chat_request and every
    streaming/session type stay off-limits (they have side effects and
    their own dedicated pipeline in backend/websocket/handlers.py that
    this module deliberately never touches), so a batch containing one
    gets a per-item error entry in its place instead of being executed.
    """
    requests = payload.get("requests")
    if not isinstance(requests, list):
        return fmt.error_response("Missing or invalid 'requests' list", schema.BATCH_REQUEST, ipc_errors.MISSING_FIELD)

    responses = []
    for sub in requests:
        if not isinstance(sub, dict) or not isinstance(sub.get("type"), str):
            responses.append(fmt.error_response("Malformed batch item: expected {'type': str, ...}", code=ipc_errors.MALFORMED_PACKET))
            continue

        sub_type = sub["type"]
        if sub_type not in HANDLED_TYPES:
            responses.append(fmt.error_response(
                f"Packet type not allowed in a batch: {sub_type}", sub_type, ipc_errors.UNKNOWN_PACKET_TYPE,
            ))
            continue

        responses.append(dispatch(sub))

    return fmt.batch_response(responses)


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_HANDLERS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    schema.MODELS_LIST_REQUEST: _handle_models_list,
    schema.MODEL_INSTALL_REQUEST: _handle_model_install,
    schema.MODEL_UNINSTALL_REQUEST: _handle_model_uninstall,
    schema.MODEL_SET_ACTIVE_REQUEST: _handle_model_set_active,
    schema.MODEL_SET_FALLBACK_REQUEST: _handle_model_set_fallback,
    schema.MODEL_SET_EMERGENCY_REQUEST: _handle_model_set_emergency,
    schema.MODEL_REQUIREMENTS_REQUEST: _handle_model_requirements,
    schema.MODEL_PERFORMANCE_REQUEST: _handle_model_performance,
    schema.DIAGNOSTICS_REQUEST: _handle_diagnostics,
    schema.DIAGNOSTICS_HARDWARE_REQUEST: _handle_diagnostics_hardware,
    schema.DIAGNOSTICS_CACHE_REQUEST: _handle_diagnostics_cache,
    schema.DIAGNOSTICS_PERFORMANCE_REQUEST: _handle_diagnostics_performance,
    schema.METRICS_REQUEST: _handle_metrics,
    schema.TOOLS_LIST_REQUEST: _handle_tools_list,
    schema.TOOL_EXECUTE_REQUEST: _handle_tool_execute,
    schema.PLUGINS_LIST_REQUEST: _handle_plugins_list,
    schema.PROVIDERS_UNIFIED_LIST_REQUEST: _handle_providers_unified_list,
    schema.DIAGNOSTICS_AUTOBALANCE_REQUEST: _handle_diagnostics_autobalance,
    schema.DIAGNOSTICS_ROUTING_REQUEST: _handle_diagnostics_routing,
    schema.DIAGNOSTICS_MODELS_REQUEST: _handle_diagnostics_models,
    schema.DIAGNOSTICS_PROVIDERS_REQUEST: _handle_diagnostics_providers,
    schema.DIAGNOSTICS_WEATHER_REQUEST: _handle_diagnostics_weather,
    schema.DIAGNOSTICS_TOOLS_REQUEST: _handle_diagnostics_tools,
    schema.DIAGNOSTICS_BACKEND_REQUEST: _handle_diagnostics_backend,
    schema.MODEL_CATALOG_REQUEST: _handle_model_catalog,
    schema.PROVIDERS_LIST_REQUEST: _handle_providers_list,
    schema.PROVIDER_KEY_SET_REQUEST: _handle_provider_key_set,
    schema.PROVIDER_KEY_DELETE_REQUEST: _handle_provider_key_delete,
    schema.PLUGIN_REGISTRY_LIST_REQUEST: _handle_plugin_registry_list,
    schema.PLUGIN_GET_REQUEST: _handle_plugin_get,
    schema.PLUGIN_UPDATE_REQUEST: _handle_plugin_update,
    schema.PLUGIN_REMOVE_REQUEST: _handle_plugin_remove,
    schema.PLUGIN_TEST_REQUEST: _handle_plugin_test,
    schema.PLUGIN_DISCOVERY_REQUEST: _handle_plugin_discovery,
    schema.UNITY_CLI_COMMANDS_REQUEST: _handle_unity_cli_commands,
    schema.UNITY_CLI_REFRESH_REQUEST: _handle_unity_cli_refresh,
    schema.UNITY_CLI_COMMAND_REQUEST: _handle_unity_cli_command,
    schema.MODULES_LIST_REQUEST: _handle_modules_list,
    schema.WORKSPACE_STATUS_REQUEST: _handle_workspace_status,
    schema.WORKSPACE_SET_REQUEST: _handle_workspace_set,
    schema.WORKSPACE_LIST_REQUEST: _handle_workspace_list,
    schema.WORKSPACE_DETAILS_REQUEST: _handle_workspace_details,
    schema.WORKSPACE_ADD_REQUEST: _handle_workspace_add,
    schema.WORKSPACE_REMOVE_REQUEST: _handle_workspace_remove,
    schema.WORKSPACE_PRIMARY_REQUEST: _handle_workspace_primary,
    schema.WORKSPACE_COMMIT_REQUEST: _handle_workspace_commit,
    schema.WORKSPACE_DISCARD_REQUEST: _handle_workspace_discard,
    schema.WORKSPACE_ROLLBACK_REQUEST: _handle_workspace_rollback,
    schema.MODULE_KEY_SET_REQUEST: _handle_module_key_set,
    schema.MODULE_KEY_DELETE_REQUEST: _handle_module_key_delete,
    schema.MODE_STATUS_REQUEST: _handle_mode_status,
    schema.HEARTBEAT: _handle_heartbeat,
    schema.BATCH_REQUEST: _handle_batch_request,
}

# Packet types this router owns — backend/websocket/handlers.py checks
# membership here before calling dispatch(), so chat_request and
# everything else keeps going through its existing code paths untouched.
HANDLED_TYPES = frozenset(_HANDLERS.keys())


# The handlers that take on_progress. A set rather than a chain of
# comparisons, because it started as one special case and adding the
# second by extending an `if` is how the third gets forgotten.
#
# The two use it differently and both are correct: commit sends
# strings, which become chat commentary, and the Unity CLI sends whole
# packets, which become terminal lines.
_WANTS_PROGRESS = frozenset({
    schema.WORKSPACE_COMMIT_REQUEST,
    schema.UNITY_CLI_COMMAND_REQUEST,
})


def dispatch(packet: Dict[str, Any], on_progress=None) -> Dict[str, Any]:
    """
    Route one parsed JSON packet ({"type", "payload"}) to its handler
    and return a fully-formed, JSON-serializable response packet.

    Defense in depth: backend/websocket/handlers.py._dispatch() already
    validates packet["type"] is a non-empty string before ever calling
    this (a malformed packet like {"type": {"type": "...", ...}} — from
    a client bug passing an object where a string was expected — would
    otherwise reach `_HANDLERS.get(ptype)` with an unhashable dict key
    and raise TypeError). This function is still a public entry point in
    its own right, so it re-validates rather than assuming a well-behaved
    caller.

    `on_progress` is for the one handler that is slow enough to need it.
    Committing runs the whole test suite -- three minutes on this
    project -- and a user watching a dialog for three minutes with no
    output has been given no reason to believe anything is happening.
    Every other handler ignores it.
    """
    if not isinstance(packet, dict):
        logger.error(f"ipc_router.dispatch() — expected an object, got {type(packet).__name__}: {packet!r}")
        return fmt.error_response(f"Malformed packet: expected an object, got {type(packet).__name__}", code=ipc_errors.MALFORMED_PACKET)

    ptype = packet.get("type")

    if not isinstance(ptype, str) or not ptype:
        logger.error(f"ipc_router.dispatch() — 'type' must be a non-empty string, got {ptype!r}")
        return fmt.error_response("Malformed packet: 'type' must be a non-empty string", code=ipc_errors.MALFORMED_PACKET)

    payload = packet.get("payload") or {}

    logger.debug(f"ipc_router.dispatch() → type={ptype}, payload={payload}")

    handler = _HANDLERS.get(ptype)
    if handler is None:
        logger.warning(f"ipc_router: no handler registered for type={ptype}")
        return fmt.error_response(f"Unknown packet type: {ptype}", ptype, ipc_errors.UNKNOWN_PACKET_TYPE)

    try:
        if ptype in _WANTS_PROGRESS:
            return handler(payload, on_progress=on_progress)
        return handler(payload)
    except Exception as e:
        logger.exception(f"ipc_router: handler for '{ptype}' failed: {e}")
        return fmt.error_response(str(e), ptype, ipc_errors.HANDLER_EXCEPTION)
