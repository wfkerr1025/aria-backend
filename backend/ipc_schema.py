# backend/ipc_schema.py

"""
Single source of truth for every WebSocket packet "type" string this
backend sends or accepts.

This is a naming/centralization pass over wire strings that already exist
and already work (see backend/websocket/handlers.py and backend/ipc_router.py
for where each one is actually used) — every value below is copied verbatim
from its existing call site. Nothing here renames a packet type; the goal is
one place that enumerates the whole IPC surface so a typo in a literal
string (e.g. "modle_set_active_result") becomes an ImportError/AttributeError
at import time instead of a silently-unmatched packet type at runtime.

webui/core/ipc_schema.js is the frontend mirror — same names, same values.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Chat / streaming (backend/websocket/handlers.py, backend/core/streaming_engine.py)
# ---------------------------------------------------------------------------
CHAT_REQUEST = "chat_request"
CHAT_RESPONSE = "chat_response"
STREAM_START = "stream_start"
STREAM_TOKEN = "stream_token"
STREAM_END = "stream_end"
STREAM_ERROR = "stream_error"

# Sent once per turn by backend/core/streaming_engine.py, right after
# provider/model resolution completes and before any tokens — tells the
# client which specific model actually served (or will serve) this
# turn, whether local or cloud. Not a stream_* packet itself (no
# requestId-scoped start/token/end sequence of its own), just a single
# announcement. See backend/websocket/handlers.py and
# backend/rest/router.py for where it's consumed.
ACTIVE_MODEL_CHANGED = "active_model_changed"

# ---------------------------------------------------------------------------
# Safety warning / override, context reset, mode/model switch confirmations
# (backend/websocket/handlers.py)
# ---------------------------------------------------------------------------
SAFETY_WARNING = "safety_warning"
LOAD_MODEL_OVERRIDE = "load_model_override"
SWITCH_TO_LIGHTER_MODEL = "switch_to_lighter_model"
CONTEXT_RESET = "context_reset"
CONTEXT_RESET_ACK = "context_reset_ack"
MODE_SET_RESULT = "mode_set_result"
SHUTDOWN = "shutdown"

# ---------------------------------------------------------------------------
# Model compatibility-system IPC surface (backend/ipc_router.py,
# backend/ipc_packet_formats.py)
# ---------------------------------------------------------------------------
MODELS_LIST_REQUEST = "models_list_request"
MODELS_LIST_RESULT = "models_list_result"
MODEL_INSTALL_REQUEST = "model_install_request"
MODEL_INSTALL_RESULT = "model_install_result"
MODEL_UNINSTALL_REQUEST = "model_uninstall_request"
MODEL_UNINSTALL_RESULT = "model_uninstall_result"
MODEL_SET_ACTIVE_REQUEST = "model_set_active_request"
MODEL_SET_ACTIVE_RESULT = "model_set_active_result"
MODEL_SET_FALLBACK_REQUEST = "model_set_fallback_request"
MODEL_SET_FALLBACK_RESULT = "model_set_fallback_result"
MODEL_SET_EMERGENCY_REQUEST = "model_set_emergency_request"
MODEL_SET_EMERGENCY_RESULT = "model_set_emergency_result"
MODEL_REQUIREMENTS_REQUEST = "model_requirements_request"
MODEL_REQUIREMENTS_RESULT = "model_requirements_result"
MODEL_INSTALL_BLOCKED = "model_install_blocked"
MODEL_PERFORMANCE_REQUEST = "model_performance_request"
MODEL_PERFORMANCE_RESULT = "model_performance_result"
DIAGNOSTICS_REQUEST = "diagnostics_request"
DIAGNOSTICS_RESPONSE = "diagnostics_response"

# Additive diagnostics surface (new — this task) — one packet type per
# /v1/diagnostics/* REST route (backend/rest/router.py), backed by the
# exact same backend.core functions on both transports (hardware_snapshot_cache,
# cache_manager, perf_profiler). DIAGNOSTICS_REQUEST/RESPONSE above stay
# exactly as they were (a live resource snapshot) — these are separate,
# more specific queries, not a replacement.
DIAGNOSTICS_HARDWARE_REQUEST = "diagnostics_hardware_request"
DIAGNOSTICS_HARDWARE_RESULT = "diagnostics_hardware_result"
DIAGNOSTICS_CACHE_REQUEST = "diagnostics_cache_request"
DIAGNOSTICS_CACHE_RESULT = "diagnostics_cache_result"
DIAGNOSTICS_PERFORMANCE_REQUEST = "diagnostics_performance_request"
DIAGNOSTICS_PERFORMANCE_RESULT = "diagnostics_performance_result"

# ---------------------------------------------------------------------------
# Phase 2 additive surface — metrics, tools, plugins. Same
# transport-parity convention as the diagnostics_* types above: each has
# a matching GET/POST /v1/* REST route (backend/rest/router.py) backed by
# the exact same backend.core function.
# ---------------------------------------------------------------------------
METRICS_REQUEST = "metrics_request"
METRICS_RESULT = "metrics_result"
TOOLS_LIST_REQUEST = "tools_list_request"
TOOLS_LIST_RESULT = "tools_list_result"
TOOL_EXECUTE_REQUEST = "tool_execute_request"
TOOL_EXECUTE_RESULT = "tool_execute_result"
PLUGINS_LIST_REQUEST = "plugins_list_request"
PLUGINS_LIST_RESULT = "plugins_list_result"
PROVIDERS_UNIFIED_LIST_REQUEST = "providers_unified_list_request"
PROVIDERS_UNIFIED_LIST_RESULT = "providers_unified_list_result"
DIAGNOSTICS_AUTOBALANCE_REQUEST = "diagnostics_autobalance_request"
DIAGNOSTICS_AUTOBALANCE_RESULT = "diagnostics_autobalance_result"
DIAGNOSTICS_ROUTING_REQUEST = "diagnostics_routing_request"
DIAGNOSTICS_ROUTING_RESULT = "diagnostics_routing_result"
DIAGNOSTICS_MODELS_REQUEST = "diagnostics_models_request"
DIAGNOSTICS_MODELS_RESULT = "diagnostics_models_result"

# Batch 3 — weather/tool provider truth (backend.core.weather_provider,
# weather_router, tool_router, connection_state).
DIAGNOSTICS_PROVIDERS_REQUEST = "diagnostics_providers_request"
DIAGNOSTICS_PROVIDERS_RESULT = "diagnostics_providers_result"
DIAGNOSTICS_WEATHER_REQUEST = "diagnostics_weather_request"
DIAGNOSTICS_WEATHER_RESULT = "diagnostics_weather_result"
DIAGNOSTICS_TOOLS_REQUEST = "diagnostics_tools_request"
DIAGNOSTICS_TOOLS_RESULT = "diagnostics_tools_result"

# Batch 4 — backend crash/freeze/restart observability
# (backend.core.backend_watchdog).
DIAGNOSTICS_BACKEND_REQUEST = "diagnostics_backend_request"
DIAGNOSTICS_BACKEND_RESULT = "diagnostics_backend_result"

MODEL_CATALOG_REQUEST = "model_catalog_request"
MODEL_CATALOG_RESULT = "model_catalog_result"
MODE_STATUS_REQUEST = "mode_status_request"
MODE_STATUS_RESULT = "mode_status_result"

# ---------------------------------------------------------------------------
# Warning system (backend.core.warning_manager) — pushed unsolicited by
# backend/websocket/handlers.py, flat (not wrapped in {"type","payload"}),
# same convention as stream_start/stream_token/safety_warning (see
# ipc_packet_formats.py's own docstring for why those specific types are
# the flat exception to the {"type","payload"} envelope everything else
# in this file uses).
# ---------------------------------------------------------------------------
WARNING_EVENT = "warning_event"

# ---------------------------------------------------------------------------
# Provider / module key management (backend/ipc_router.py)
# ---------------------------------------------------------------------------
PROVIDERS_LIST_REQUEST = "providers_list_request"
PROVIDERS_LIST_RESULT = "providers_list_result"
PROVIDER_KEY_SET_REQUEST = "provider_key_set_request"
PROVIDER_KEY_SET_RESULT = "provider_key_set_result"
PROVIDER_KEY_DELETE_REQUEST = "provider_key_delete_request"
PROVIDER_KEY_DELETE_RESULT = "provider_key_delete_result"
# Plugins -- the integrations a user installs, configures and turns on.
# Not to be confused with backend/plugins/unity_csharp.py, which is brief
# text and has no settings, no switch and no page.
PLUGIN_REGISTRY_LIST_REQUEST = "plugin_registry_list_request"
PLUGIN_GET_REQUEST = "plugin_get_request"
PLUGIN_UPDATE_REQUEST = "plugin_update_request"
PLUGIN_REMOVE_REQUEST = "plugin_remove_request"
PLUGIN_TEST_REQUEST = "plugin_test_request"
PLUGIN_DISCOVERY_REQUEST = "plugin_discovery_request"
UNITY_CLI_COMMANDS_REQUEST = "unity_cli_commands_request"
UNITY_CLI_REFRESH_REQUEST = "unity_cli_refresh_request"
UNITY_CLI_COMMAND_REQUEST = "unity_cli_command_request"

MODULES_LIST_REQUEST = "modules_list_request"

# The working directory panel: which project ARIA is in, where it stages,
# and whether the active model can be asked for a structured action.
WORKSPACE_STATUS_REQUEST = "workspace_status_request"
WORKSPACE_STATUS_RESULT = "workspace_status_result"
WORKSPACE_SET_REQUEST = "workspace_set_request"

# The Control Center: several projects at once. Two result shapes only --
# a list and a detail -- because every mutation is interesting for the
# state it produces, so add/remove/set-primary answer with the list and
# commit/discard/rollback answer with the detail. A caller never has to
# ask twice for the result of its own change.
WORKSPACE_LIST_REQUEST = "workspace_list_request"
WORKSPACE_LIST_RESULT = "workspace_list_result"
WORKSPACE_DETAILS_REQUEST = "workspace_details_request"
WORKSPACE_DETAILS_RESULT = "workspace_details_result"
WORKSPACE_ADD_REQUEST = "workspace_add_request"
WORKSPACE_REMOVE_REQUEST = "workspace_remove_request"
WORKSPACE_PRIMARY_REQUEST = "workspace_primary_request"
WORKSPACE_COMMIT_REQUEST = "workspace_commit_request"
WORKSPACE_DISCARD_REQUEST = "workspace_discard_request"
WORKSPACE_ROLLBACK_REQUEST = "workspace_rollback_request"
PLUGIN_REGISTRY_LIST_RESULT = "plugin_registry_list_result"
PLUGIN_GET_RESULT = "plugin_get_result"
PLUGIN_UPDATE_RESULT = "plugin_update_result"
PLUGIN_REMOVE_RESULT = "plugin_remove_result"
PLUGIN_TEST_RESULT = "plugin_test_result"
PLUGIN_DISCOVERY_RESULT = "plugin_discovery_result"
UNITY_CLI_COMMANDS_RESULT = "unity_cli_commands_result"
UNITY_CLI_COMMAND_RESULT = "unity_cli_command_result"
# Streamed while a command runs, many per request, so the terminal
# view shows a long build as it happens rather than at the end.
UNITY_CLI_OUTPUT = "unity_cli_output"

MODULES_LIST_RESULT = "modules_list_result"
MODULE_KEY_SET_REQUEST = "module_key_set_request"
MODULE_KEY_SET_RESULT = "module_key_set_result"
MODULE_KEY_DELETE_REQUEST = "module_key_delete_request"
MODULE_KEY_DELETE_RESULT = "module_key_delete_result"

# ---------------------------------------------------------------------------
# Transport-level QoL additions (new — this task)
# ---------------------------------------------------------------------------
HEARTBEAT = "heartbeat"
HEARTBEAT_ACK = "heartbeat_ack"
BATCH_REQUEST = "batch_request"
BATCH_RESPONSE = "batch_response"

# Connection-status is never sent over the wire by this backend — it's a
# synthetic packet webui/core/bridge.js manufactures locally from Electron
# main-process IPC (see ARIA-Lite Desktop's main.js/preload.js). Listed here
# only so the full IPC surface — wire and synthetic — has one index.
CONNECTION_STATUS = "connection_status"

# ---------------------------------------------------------------------------
# Error (backend/ipc_packet_formats.py — flat shape, not {"type","payload"})
# ---------------------------------------------------------------------------
ERROR = "error"
