# backend/ipc_errors.py

"""
Standardized error codes, shared across this backend's transports —
originally the WebSocket IPC layer, now also backend/rest/router.py and
backend/rest/auth.py's HTTP surface (see those modules: they wrap the
same codes in a {"error": {"code", "message"}} HTTP shape instead of
build_error()'s WS packet shape, but draw from this one registry so a
given failure means the same thing regardless of which transport a
client used).

build_error() returns a strict superset of the flat error shape
backend/ipc_packet_formats.py.error_response() already sent before this file
existed ({"type": "error", "message": ...}) — it just also sets "code" and,
optionally, "request_type". Every existing consumer of an error packet
(webui/core/bridge.js, webui/components/chat/chat.js's new error-toast
branch, the various *_regression_tests.mjs) only ever reads ".message" /
".type", so adding "code" is additive and cannot break anything already
reading this shape.
"""

from __future__ import annotations
from typing import Any, Dict, Optional

from backend import ipc_schema as schema

# Malformed input at the transport/protocol level, before any handler runs.
MALFORMED_JSON = "MALFORMED_JSON"
MALFORMED_PACKET = "MALFORMED_PACKET"
UNKNOWN_PACKET_TYPE = "UNKNOWN_PACKET_TYPE"

# A recognized packet type with missing/invalid required fields.
MISSING_FIELD = "MISSING_FIELD"
UNKNOWN_MODEL = "UNKNOWN_MODEL"

# A handler ran and raised.
HANDLER_EXCEPTION = "HANDLER_EXCEPTION"
INFERENCE_ERROR = "INFERENCE_ERROR"
DISPATCH_ERROR = "DISPATCH_ERROR"

# HTTP-transport-specific: no WebSocket equivalent exists because the
# WS layer has no per-request auth gate today. Used by
# backend/rest/auth.py's API-key check.
UNAUTHORIZED = "UNAUTHORIZED"

# Fallback for anything not classified above — kept distinct from the
# transport-level codes so a caller can tell "we don't have a specific code
# for this yet" from "this is deliberately generic."
GENERIC_ERROR = "GENERIC_ERROR"

# Routing invariants (backend.core.routing_invariants / routing_guard) —
# a mode/provider/model switch that would produce an impossible
# routing_mode+cloud_provider+active_model_id+location combination.
# NO_CLOUD_PROVIDER is split out from the general
# ROUTING_INVARIANT_VIOLATION because it's the one case with a specific,
# actionable UI response ("add an API key in Settings"), not just "this
# combination is invalid".
ROUTING_INVARIANT_VIOLATION = "ROUTING_INVARIANT_VIOLATION"
NO_CLOUD_PROVIDER = "NO_CLOUD_PROVIDER"

# Batch 2 — model selection (backend.core.model_selector) failures at
# switch time. Distinct from NO_CLOUD_PROVIDER: the provider itself is
# valid/configured, but no concrete model could be resolved for it (no
# default mapping, no override) — see model_selector.select_cloud_model().
CLOUD_MODEL_RESOLUTION_FAILED = "CLOUD_MODEL_RESOLUTION_FAILED"
# The requested local model_id is unknown, or belongs to a non-local
# provider — see model_selector.select_local_model().
LOCAL_MODEL_INVALID = "LOCAL_MODEL_INVALID"

# Batch 3 — weather/tool truth alignment (backend.core.weather_provider,
# weather_router, tool_router). Same convention as the routing codes
# above: each module owns its own plain-string constant of the same
# value (avoiding a core module importing this transport-layer file),
# these exist here for documentation/discoverability and for callers
# that already have `ipc_errors` imported.
WEATHER_PROVIDER_NOT_CONFIGURED = "WEATHER_PROVIDER_NOT_CONFIGURED"
WEATHER_FETCH_FAILED = "WEATHER_FETCH_FAILED"
BACKEND_DISCONNECTED = "BACKEND_DISCONNECTED"
TOOL_PROVIDER_NOT_CONFIGURED = "TOOL_PROVIDER_NOT_CONFIGURED"


def build_error(
    code: str,
    message: str,
    request_type: Optional[str] = None,
) -> Dict[str, Any]:
    packet: Dict[str, Any] = {"type": schema.ERROR, "code": code, "message": message}
    if request_type:
        packet["request_type"] = request_type
    return packet
