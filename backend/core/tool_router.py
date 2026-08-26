# backend/core/tool_router.py

"""
Batch 3 — the single, centralized gate every tool execution goes
through, wrapping backend.core.tool_registry.execute_tool() with the
same truth-alignment rules Batch 1/2 established for LLM routing: never
run (or claim to have run) a tool while the connection to the caller is
known to be down, never run a tool whose required provider isn't
configured, and always return a structured error instead of
fallback/guessed data.

Distinct from backend/llm/tool_router.py, which is an LLM-OUTPUT
classifier (decides whether the model's raw text names a tool call) —
this module sits on the other side, gating actual EXECUTION regardless
of what asked for it (chat intent short-circuit, tool_execute_request,
REST /tools/{name}/execute).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from backend.core import tool_registry
from backend.logger import log as unified_log

from logger import get_logger

logger = get_logger(__name__)

BACKEND_DISCONNECTED = "BACKEND_DISCONNECTED"
TOOL_PROVIDER_NOT_CONFIGURED = "TOOL_PROVIDER_NOT_CONFIGURED"

# Per-tool metadata this router owns:
#   TOOL_LOCATIONS — which "location" a tool's actual work happens in.
#     Both shipped tools (weather, web_search) call a real external HTTP
#     API, so both are "cloud"; a hypothetical future purely local tool
#     (a filesystem operation, say) would be "local". Mirrors the same
#     cloud/local vocabulary Batch 2's active_model_changed uses, so a
#     tool packet and a model packet read consistently side by side.
#   TOOL_REQUIRED_PROVIDERS — tool_name -> a zero-arg callable returning
#     True if that tool's required provider is configured. Neither
#     shipped tool has a HARD requirement today (weather's multi-provider
#     chain — NOAA/Open-Meteo need no key — is resilient by design; see
#     backend.core.weather_router's module docstring for why WeatherAPI
#     is deliberately NOT a hard requirement there). This mapping exists
#     so a future tool with a real hard requirement has one place to
#     declare it, and so this router's gate is genuinely enforced rather
#     than vestigial.
TOOL_LOCATIONS: Dict[str, str] = {
    "weather": "cloud",
    "web_search": "cloud",
}
TOOL_REQUIRED_PROVIDERS: Dict[str, Callable[[], bool]] = {}


@dataclass(frozen=True)
class ToolRouterError:
    code: str
    message: str


# Batch 3 diagnostics — "last successful tool call" / "last error", read
# by ipc_router._handle_diagnostics_tools(). Same shape/reasoning as
# backend.core.weather_router's identical tracker.
_last_success: Optional[Dict[str, Any]] = None
_last_error: Optional[Dict[str, Any]] = None


def get_last_call_history() -> Dict[str, Optional[Dict[str, Any]]]:
    return {"last_success": _last_success, "last_error": _last_error}


def _record_outcome(packet: Optional[Dict[str, Any]], error: Optional[ToolRouterError], tool_name: str) -> None:
    global _last_success, _last_error
    if error is not None:
        _last_error = {"tool_name": tool_name, "code": error.code, "message": error.message, "timestamp": time.time()}
        return
    if packet is not None and packet.get("ok"):
        _last_success = {**packet}
    else:
        _last_error = {
            "tool_name": tool_name,
            "code": (packet or {}).get("error_code"),
            "message": (packet or {}).get("error"),
            "timestamp": time.time(),
        }


def _provider_name_for(tool_name: str, value: Any) -> Optional[str]:
    # The weather tool's handler (tool_registry._weather_handler) now
    # returns a dict built from weather_router.WeatherPacket.to_dict()
    # (or its error shape). Batch 3.5: weather is now a FUSED,
    # multi-provider result (backend.core.weather_fusion) — there is no
    # longer a single "provider" that served it, so this reports every
    # provider that actually contributed a sample, joined, rather than
    # naming just one (which would misrepresent a blend as a single
    # source). Any other tool with a genuine single provider concept
    # would still just have a plain "provider" key here.
    if not isinstance(value, dict):
        return None
    if "providersUsed" in value:
        used = value.get("providersUsed") or []
        return "+".join(used) if used else None
    return value.get("provider")


def execute_tool_truthful(
    tool_name: str,
    args: Optional[Dict[str, Any]] = None,
    connection_healthy: bool = True,
) -> "tuple[Optional[Dict[str, Any]], Optional[ToolRouterError]]":
    """
    Returns (packet, None) on success/attempted-execution or
    (None, ToolRouterError) if the request is rejected outright before
    tool_registry.execute_tool() is ever called (disconnected, or a
    required provider isn't configured).

    `connection_healthy` defaults to True — the REST path has no
    persistent-connection concept to be "disconnected" from (a REST
    request is either received or it isn't); only
    backend/websocket/handlers.py's WebSocketHandler actually tracks
    this (see its _connection_healthy flag, set False when a send over
    this connection has actually failed — the same signal Batch 1's
    heartbeat sender depends on) and passes it through explicitly.

    `packet` always carries toolName/providerName/location/timestamp
    alongside the underlying ToolResult's ok/value/error/error_code (see
    backend.core.tool_registry.ToolResult) — never partial, never null
    where a real value should be.
    """
    if not connection_healthy:
        unified_log("tool_router", "WARNING", "Tool execution rejected — backend connection is down", {
            "tool_name": tool_name,
        })
        error = ToolRouterError(
            BACKEND_DISCONNECTED,
            "Backend connection is down — tool requests are not accepted until it recovers.",
        )
        _record_outcome(None, error, tool_name)
        return None, error

    provider_check = TOOL_REQUIRED_PROVIDERS.get(tool_name)
    if provider_check is not None and not provider_check():
        unified_log("tool_router", "WARNING", "Tool execution rejected — required provider not configured", {
            "tool_name": tool_name,
        })
        error = ToolRouterError(
            TOOL_PROVIDER_NOT_CONFIGURED,
            f"'{tool_name}' requires a provider that isn't configured yet.",
        )
        _record_outcome(None, error, tool_name)
        return None, error

    result = tool_registry.execute_tool(tool_name, args)

    # A tool whose own domain logic failed (e.g. the weather handler's
    # {"ok": False, "error_code": ...} shape — see weather_router.py)
    # still executed successfully at the sandbox level (ToolResult.ok is
    # about "did the handler run without crashing/timing out", not "did
    # it get a useful answer") — surface the more specific inner
    # error_code when the tool's own result says so, rather than losing
    # it behind a generic "it ran fine" outer shell.
    ok = result.ok
    error = result.error
    error_code = result.error_code
    if ok and isinstance(result.value, dict) and result.value.get("ok") is False:
        ok = False
        error = result.value.get("error") or error
        error_code = result.value.get("error_code") or error_code

    packet = {
        "toolName": tool_name,
        "providerName": _provider_name_for(tool_name, result.value),
        "location": TOOL_LOCATIONS.get(tool_name, "local"),
        "timestamp": time.time(),
        "ok": ok,
        "value": result.value,
        "error": error,
        "error_code": error_code,
    }
    unified_log("tool_router", "INFO" if ok else "ERROR", "Tool executed", {
        "tool_name": tool_name, "ok": ok, "error_code": error_code,
    })
    _record_outcome(packet, None, tool_name)
    return packet, None
