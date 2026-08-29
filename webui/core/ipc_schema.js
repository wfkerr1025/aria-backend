// core/ipc_schema.js
//
// Frontend mirror of backend/ipc_schema.py — same names, same values. See
// that module's docstring: this is a naming/centralization pass over wire
// strings that already exist and already work, not a renaming pass.

export const IPC = Object.freeze({
  // Chat / streaming
  CHAT_REQUEST: "chat_request",
  CHAT_RESPONSE: "chat_response",
  STREAM_START: "stream_start",
  STREAM_TOKEN: "stream_token",
  STREAM_END: "stream_end",
  STREAM_ERROR: "stream_error",
  // Sent once per turn by backend/core/streaming_engine.py, right after
  // provider/model resolution, before any tokens — announces which
  // specific model (local or cloud) actually serves this turn. Not
  // part of the stream_start/token/end sequence itself.
  ACTIVE_MODEL_CHANGED: "active_model_changed",

  // Safety warning / override, context reset, mode/model switch confirmations
  SAFETY_WARNING: "safety_warning",
  LOAD_MODEL_OVERRIDE: "load_model_override",
  SWITCH_TO_LIGHTER_MODEL: "switch_to_lighter_model",
  CONTEXT_RESET: "context_reset",
  CONTEXT_RESET_ACK: "context_reset_ack",
  MODE_SET_RESULT: "mode_set_result",
  SHUTDOWN: "shutdown",

  // Model compatibility-system IPC surface
  MODELS_LIST_REQUEST: "models_list_request",
  MODELS_LIST_RESULT: "models_list_result",
  MODEL_INSTALL_REQUEST: "model_install_request",
  MODEL_INSTALL_RESULT: "model_install_result",
  MODEL_UNINSTALL_REQUEST: "model_uninstall_request",
  MODEL_UNINSTALL_RESULT: "model_uninstall_result",
  MODEL_SET_ACTIVE_REQUEST: "model_set_active_request",
  MODEL_SET_ACTIVE_RESULT: "model_set_active_result",
  MODEL_SET_FALLBACK_REQUEST: "model_set_fallback_request",
  MODEL_SET_FALLBACK_RESULT: "model_set_fallback_result",
  MODEL_SET_EMERGENCY_REQUEST: "model_set_emergency_request",
  MODEL_SET_EMERGENCY_RESULT: "model_set_emergency_result",
  MODEL_REQUIREMENTS_REQUEST: "model_requirements_request",
  MODEL_REQUIREMENTS_RESULT: "model_requirements_result",
  MODEL_INSTALL_BLOCKED: "model_install_blocked",
  MODEL_PERFORMANCE_REQUEST: "model_performance_request",
  MODEL_PERFORMANCE_RESULT: "model_performance_result",
  DIAGNOSTICS_REQUEST: "diagnostics_request",
  DIAGNOSTICS_RESPONSE: "diagnostics_response",
  DIAGNOSTICS_AUTOBALANCE_REQUEST: "diagnostics_autobalance_request",
  DIAGNOSTICS_AUTOBALANCE_RESULT: "diagnostics_autobalance_result",
  DIAGNOSTICS_ROUTING_REQUEST: "diagnostics_routing_request",
  DIAGNOSTICS_ROUTING_RESULT: "diagnostics_routing_result",
  DIAGNOSTICS_MODELS_REQUEST: "diagnostics_models_request",
  DIAGNOSTICS_MODELS_RESULT: "diagnostics_models_result",
  // Batch 3 — weather/tool provider truth.
  DIAGNOSTICS_PROVIDERS_REQUEST: "diagnostics_providers_request",
  DIAGNOSTICS_PROVIDERS_RESULT: "diagnostics_providers_result",
  DIAGNOSTICS_WEATHER_REQUEST: "diagnostics_weather_request",
  DIAGNOSTICS_WEATHER_RESULT: "diagnostics_weather_result",
  DIAGNOSTICS_TOOLS_REQUEST: "diagnostics_tools_request",
  DIAGNOSTICS_TOOLS_RESULT: "diagnostics_tools_result",
  // Batch 4 — backend crash/freeze/restart observability.
  DIAGNOSTICS_BACKEND_REQUEST: "diagnostics_backend_request",
  DIAGNOSTICS_BACKEND_RESULT: "diagnostics_backend_result",
  MODEL_CATALOG_REQUEST: "model_catalog_request",
  MODEL_CATALOG_RESULT: "model_catalog_result",
  MODE_STATUS_REQUEST: "mode_status_request",
  MODE_STATUS_RESULT: "mode_status_result",

  // Warning system (backend.core.warning_manager) — pushed unsolicited,
  // flat shape (same convention as STREAM_START/SAFETY_WARNING above).
  WARNING_EVENT: "warning_event",

  // Provider / module key management
  PROVIDERS_LIST_REQUEST: "providers_list_request",
  PROVIDERS_LIST_RESULT: "providers_list_result",
  PROVIDER_KEY_SET_REQUEST: "provider_key_set_request",
  PROVIDER_KEY_SET_RESULT: "provider_key_set_result",
  PROVIDER_KEY_DELETE_REQUEST: "provider_key_delete_request",
  PROVIDER_KEY_DELETE_RESULT: "provider_key_delete_result",
  MODULES_LIST_REQUEST: "modules_list_request",

  // The working-directory panel. One result shape for both
  // requests: a change is only interesting because of the state
  // it produces.
  WORKSPACE_STATUS_REQUEST: "workspace_status_request",
  WORKSPACE_STATUS_RESULT: "workspace_status_result",
  WORKSPACE_SET_REQUEST: "workspace_set_request",
  MODULES_LIST_RESULT: "modules_list_result",
  MODULE_KEY_SET_REQUEST: "module_key_set_request",
  MODULE_KEY_SET_RESULT: "module_key_set_result",
  MODULE_KEY_DELETE_REQUEST: "module_key_delete_request",
  MODULE_KEY_DELETE_RESULT: "module_key_delete_result",

  // Transport-level QoL additions
  HEARTBEAT: "heartbeat",
  HEARTBEAT_ACK: "heartbeat_ack",
  BATCH_REQUEST: "batch_request",
  BATCH_RESPONSE: "batch_response",

  // Synthetic — never sent by the backend. Manufactured locally by
  // core/bridge.js from Electron main-process connection-state IPC (see
  // ARIA-Lite Desktop's main.js/preload.js) and re-dispatched through the
  // same "backend-packet" pipe as real backend packets.
  CONNECTION_STATUS: "connection_status",

  // Error (flat shape, not {"type","payload"})
  ERROR: "error",
});
