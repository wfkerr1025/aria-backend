# backend/core/errors.py

"""
Unified error system — Phase 2, item 7.

backend/ipc_errors.py already defines a flat error-code registry shared
by the WebSocket IPC layer and the REST API's HTTP error shape (see that
module's docstring) — this does NOT replace it; ERROR_CATEGORIES below
maps every existing ipc_errors code into one of a small set of
categories, and AriaError is a new exception hierarchy that new Phase 2
code (providers, tools, plugins, the execution pipeline) raises/catches,
translated at the transport boundary via to_error_packet()/to_http_detail()
into the exact same wire shapes ipc_errors.build_error() and
backend/rest/router.py's _rest_error() already produce — so a client
never sees a new, incompatible error shape, just more codes and (new)
optional recovery-suggestion text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend import ipc_errors
from backend import ipc_schema as schema


class ErrorCode:
    """New codes for Phase 2 subsystems, alongside backend.ipc_errors's
    existing transport-level codes (reused, not duplicated, for anything
    that already has one — e.g. UNKNOWN_MODEL, HANDLER_EXCEPTION)."""

    # Re-exported so callers only need `from backend.core.errors import ErrorCode`.
    MALFORMED_JSON = ipc_errors.MALFORMED_JSON
    MALFORMED_PACKET = ipc_errors.MALFORMED_PACKET
    UNKNOWN_PACKET_TYPE = ipc_errors.UNKNOWN_PACKET_TYPE
    MISSING_FIELD = ipc_errors.MISSING_FIELD
    UNKNOWN_MODEL = ipc_errors.UNKNOWN_MODEL
    HANDLER_EXCEPTION = ipc_errors.HANDLER_EXCEPTION
    INFERENCE_ERROR = ipc_errors.INFERENCE_ERROR
    DISPATCH_ERROR = ipc_errors.DISPATCH_ERROR
    UNAUTHORIZED = ipc_errors.UNAUTHORIZED
    GENERIC_ERROR = ipc_errors.GENERIC_ERROR

    # Tool registry (backend.core.tool_registry)
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_INVALID_ARGS = "TOOL_INVALID_ARGS"
    TOOL_PERMISSION_DENIED = "TOOL_PERMISSION_DENIED"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"

    # Provider interface (backend.core.provider_interface)
    PROVIDER_NOT_FOUND = "PROVIDER_NOT_FOUND"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_LOAD_FAILED = "PROVIDER_LOAD_FAILED"

    # Plugin system (backend.core.plugin_registry)
    PLUGIN_LOAD_FAILED = "PLUGIN_LOAD_FAILED"
    PLUGIN_NOT_FOUND = "PLUGIN_NOT_FOUND"
    PLUGIN_MANIFEST_INVALID = "PLUGIN_MANIFEST_INVALID"

    # Execution pipeline (backend.core.execution_pipeline)
    PIPELINE_SAFETY_BLOCKED = "PIPELINE_SAFETY_BLOCKED"
    PIPELINE_NO_PROVIDER = "PIPELINE_NO_PROVIDER"

    # Cache (backend.core.cache_manager / multi_tier_cache)
    CACHE_INTEGRITY_FAILED = "CACHE_INTEGRITY_FAILED"
    CACHE_MIGRATION_FAILED = "CACHE_MIGRATION_FAILED"


# category -> (human label, default recovery suggestion). New codes are
# added here as they're introduced above; anything not listed falls back
# to CATEGORY_UNKNOWN in categorize()/RECOVERY_SUGGESTIONS.get().
CATEGORY_TRANSPORT = "transport"
CATEGORY_VALIDATION = "validation"
CATEGORY_AUTH = "auth"
CATEGORY_PROVIDER = "provider"
CATEGORY_TOOL = "tool"
CATEGORY_PLUGIN = "plugin"
CATEGORY_PIPELINE = "pipeline"
CATEGORY_CACHE = "cache"
CATEGORY_UNKNOWN = "unknown"

_CODE_CATEGORIES: Dict[str, str] = {
    ErrorCode.MALFORMED_JSON: CATEGORY_TRANSPORT,
    ErrorCode.MALFORMED_PACKET: CATEGORY_TRANSPORT,
    ErrorCode.UNKNOWN_PACKET_TYPE: CATEGORY_TRANSPORT,
    ErrorCode.DISPATCH_ERROR: CATEGORY_TRANSPORT,
    ErrorCode.MISSING_FIELD: CATEGORY_VALIDATION,
    ErrorCode.UNKNOWN_MODEL: CATEGORY_VALIDATION,
    ErrorCode.UNAUTHORIZED: CATEGORY_AUTH,
    ErrorCode.HANDLER_EXCEPTION: CATEGORY_UNKNOWN,
    ErrorCode.INFERENCE_ERROR: CATEGORY_PROVIDER,
    ErrorCode.PROVIDER_NOT_FOUND: CATEGORY_PROVIDER,
    ErrorCode.PROVIDER_UNAVAILABLE: CATEGORY_PROVIDER,
    ErrorCode.PROVIDER_LOAD_FAILED: CATEGORY_PROVIDER,
    ErrorCode.TOOL_NOT_FOUND: CATEGORY_TOOL,
    ErrorCode.TOOL_INVALID_ARGS: CATEGORY_TOOL,
    ErrorCode.TOOL_PERMISSION_DENIED: CATEGORY_TOOL,
    ErrorCode.TOOL_TIMEOUT: CATEGORY_TOOL,
    ErrorCode.TOOL_EXECUTION_FAILED: CATEGORY_TOOL,
    ErrorCode.PLUGIN_LOAD_FAILED: CATEGORY_PLUGIN,
    ErrorCode.PLUGIN_NOT_FOUND: CATEGORY_PLUGIN,
    ErrorCode.PLUGIN_MANIFEST_INVALID: CATEGORY_PLUGIN,
    ErrorCode.PIPELINE_SAFETY_BLOCKED: CATEGORY_PIPELINE,
    ErrorCode.PIPELINE_NO_PROVIDER: CATEGORY_PIPELINE,
    ErrorCode.CACHE_INTEGRITY_FAILED: CATEGORY_CACHE,
    ErrorCode.CACHE_MIGRATION_FAILED: CATEGORY_CACHE,
    ErrorCode.GENERIC_ERROR: CATEGORY_UNKNOWN,
}

_RECOVERY_SUGGESTIONS: Dict[str, str] = {
    ErrorCode.UNKNOWN_MODEL: "Check the model_id against GET /v1/models or models_list_request.",
    ErrorCode.UNAUTHORIZED: "Provide a valid X-ARIA-API-Key header.",
    ErrorCode.TOOL_NOT_FOUND: "Call list_tools() to see registered tool names.",
    ErrorCode.TOOL_PERMISSION_DENIED: "Grant the tool's declared permission, or use a different tool.",
    ErrorCode.TOOL_TIMEOUT: "Increase the tool's timeout_seconds, or simplify the request.",
    ErrorCode.PROVIDER_NOT_FOUND: "Call list_unified_providers() to see registered provider names.",
    ErrorCode.PROVIDER_UNAVAILABLE: "Configure an API key for this provider, or switch providers/mode.",
    ErrorCode.PLUGIN_MANIFEST_INVALID: "Check the plugin's manifest.json against the required schema.",
    ErrorCode.PIPELINE_SAFETY_BLOCKED: "Reduce the model size, free up resources, or use allow_override.",
    ErrorCode.PIPELINE_NO_PROVIDER: "Configure a provider for the current mode, or switch modes.",
    ErrorCode.CACHE_INTEGRITY_FAILED: "The cache will self-heal by recomputing on next access; no action needed.",
}


def categorize(code: str) -> str:
    return _CODE_CATEGORIES.get(code, CATEGORY_UNKNOWN)


def recovery_suggestion(code: str) -> Optional[str]:
    return _RECOVERY_SUGGESTIONS.get(code)


@dataclass
class AriaError(Exception):
    """
    Base exception for every Phase 2 subsystem. Carries everything
    to_error_packet()/to_http_detail() need to produce a fully-formed,
    transport-appropriate error response without the caller having to
    know which transport it's headed for.
    """
    code: str
    message: str
    request_type: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        Exception.__init__(self, self.message)

    @property
    def category(self) -> str:
        return categorize(self.code)

    @property
    def suggestion(self) -> Optional[str]:
        return recovery_suggestion(self.code)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "category": self.category,
            "suggestion": self.suggestion,
            "context": self.context,
        }

    def to_error_packet(self) -> Dict[str, Any]:
        """IPC wire shape — a strict superset of ipc_errors.build_error()'s
        {"type": "error", "code", "message", "request_type"?}."""
        packet = ipc_errors.build_error(self.code, self.message, self.request_type)
        packet["category"] = self.category
        if self.suggestion:
            packet["suggestion"] = self.suggestion
        if self.context:
            packet["context"] = self.context
        return packet

    def to_http_detail(self) -> Dict[str, Any]:
        """REST wire shape — matches backend/rest/router.py's existing
        {"error": {"code", "message"}} HTTPException(detail=...) shape,
        with category/suggestion/context added the same additive way."""
        return {"error": self.to_dict()}


class ProviderError(AriaError):
    pass


class ToolError(AriaError):
    pass


class PluginError(AriaError):
    pass


class PipelineError(AriaError):
    pass


class CacheError(AriaError):
    pass
