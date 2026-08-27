# Diagnostics (REST + IPC)

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

## Classes

### `ChatMessage`

!!! abstract "Usage Documentation"
    [Models](../concepts/models.md)

A base class for creating Pydantic models.

Attributes:
    __class_vars__: The names of the class variables defined on the model.
    __private_attributes__: Metadata about the private attributes of the model.
    __signature__: The synthesized `__init__` [`Signature`][inspect.Signature] of the model.

    __pydantic_complete__: Whether model building is completed, or if there are still undefined fields.
    __pydantic_core_schema__: The core schema of the model.
    __pydantic_custom_init__: Whether the model has a custom `__init__` function.
    __pydantic_decorators__: Metadata containing the decorators defined on the model.
        This replaces `Model.__validators__` and `Model.__root_validators__` from Pydantic V1.
    __pydantic_generic_metadata__: Metadata for generic models; contains data used for a similar purpose to
        __args__, __origin__, __parameters__ in typing-module generics. May eventually be replaced by these.
    __pydantic_parent_namespace__: Parent namespace of the model, used for automatic rebuilding of models.
    __pydantic_post_init__: The name of the post-init method for the model, if defined.
    __pydantic_root_model__: Whether the model is a [`RootModel`][pydantic.root_model.RootModel].
    __pydantic_serializer__: The `pydantic-core` `SchemaSerializer` used to dump instances of the model.
    __pydantic_validator__: The `pydantic-core` `SchemaValidator` used to validate instances of the model.

    __pydantic_fields__: A dictionary of field names and their corresponding [`FieldInfo`][pydantic.fields.FieldInfo] objects.
    __pydantic_computed_fields__: A dictionary of computed field names and their corresponding [`ComputedFieldInfo`][pydantic.fields.ComputedFieldInfo] objects.

    __pydantic_extra__: A dictionary containing extra values, if [`extra`][pydantic.config.ConfigDict.extra]
        is set to `'allow'`.
    __pydantic_fields_set__: The names of fields explicitly set during instantiation.
    __pydantic_private__: Values of private attributes set on the model instance.

- `__init__(self, /, **data: 'Any') -> 'None'`
  Create a new model by parsing and validating input data from keyword arguments.
- `copy(self, *, include: 'AbstractSetIntStr | MappingIntStrAny | None' = None, exclude: 'AbstractSetIntStr | MappingIntStrAny | None' = None, update: 'Dict[str, Any] | None' = None, deep: 'bool' = False) -> 'Self'`
  Returns a copy of the model.
- `dict(self, *, include: 'IncEx | None' = None, exclude: 'IncEx | None' = None, by_alias: 'bool' = False, exclude_unset: 'bool' = False, exclude_defaults: 'bool' = False, exclude_none: 'bool' = False) -> 'Dict[str, Any]'`
- `json(self, *, include: 'IncEx | None' = None, exclude: 'IncEx | None' = None, by_alias: 'bool' = False, exclude_unset: 'bool' = False, exclude_defaults: 'bool' = False, exclude_none: 'bool' = False, encoder: 'Callable[[Any], Any] | None' = PydanticUndefined, models_as_dict: 'bool' = PydanticUndefined, **dumps_kwargs: 'Any') -> 'str'`
- `model_copy(self, *, update: 'Mapping[str, Any] | None' = None, deep: 'bool' = False) -> 'Self'`
  !!! abstract "Usage Documentation"
- `model_dump(self, *, mode: "Literal['json', 'python'] | str" = 'python', include: 'IncEx | None' = None, exclude: 'IncEx | None' = None, context: 'Any | None' = None, by_alias: 'bool | None' = None, exclude_unset: 'bool' = False, exclude_defaults: 'bool' = False, exclude_none: 'bool' = False, exclude_computed_fields: 'bool' = False, round_trip: 'bool' = False, warnings: "bool | Literal['none', 'warn', 'error']" = True, fallback: 'Callable[[Any], Any] | None' = None, serialize_as_any: 'bool' = False) -> 'dict[str, Any]'`
  !!! abstract "Usage Documentation"
- `model_dump_json(self, *, indent: 'int | None' = None, ensure_ascii: 'bool' = False, include: 'IncEx | None' = None, exclude: 'IncEx | None' = None, context: 'Any | None' = None, by_alias: 'bool | None' = None, exclude_unset: 'bool' = False, exclude_defaults: 'bool' = False, exclude_none: 'bool' = False, exclude_computed_fields: 'bool' = False, round_trip: 'bool' = False, warnings: "bool | Literal['none', 'warn', 'error']" = True, fallback: 'Callable[[Any], Any] | None' = None, serialize_as_any: 'bool' = False) -> 'str'`
  !!! abstract "Usage Documentation"
- `model_post_init(self, context: 'Any', /) -> 'None'`
  Override this method to perform additional initialization after `__init__` and `model_construct`.

### `ChatRequest`

!!! abstract "Usage Documentation"
    [Models](../concepts/models.md)

A base class for creating Pydantic models.

Attributes:
    __class_vars__: The names of the class variables defined on the model.
    __private_attributes__: Metadata about the private attributes of the model.
    __signature__: The synthesized `__init__` [`Signature`][inspect.Signature] of the model.

    __pydantic_complete__: Whether model building is completed, or if there are still undefined fields.
    __pydantic_core_schema__: The core schema of the model.
    __pydantic_custom_init__: Whether the model has a custom `__init__` function.
    __pydantic_decorators__: Metadata containing the decorators defined on the model.
        This replaces `Model.__validators__` and `Model.__root_validators__` from Pydantic V1.
    __pydantic_generic_metadata__: Metadata for generic models; contains data used for a similar purpose to
        __args__, __origin__, __parameters__ in typing-module generics. May eventually be replaced by these.
    __pydantic_parent_namespace__: Parent namespace of the model, used for automatic rebuilding of models.
    __pydantic_post_init__: The name of the post-init method for the model, if defined.
    __pydantic_root_model__: Whether the model is a [`RootModel`][pydantic.root_model.RootModel].
    __pydantic_serializer__: The `pydantic-core` `SchemaSerializer` used to dump instances of the model.
    __pydantic_validator__: The `pydantic-core` `SchemaValidator` used to validate instances of the model.

    __pydantic_fields__: A dictionary of field names and their corresponding [`FieldInfo`][pydantic.fields.FieldInfo] objects.
    __pydantic_computed_fields__: A dictionary of computed field names and their corresponding [`ComputedFieldInfo`][pydantic.fields.ComputedFieldInfo] objects.

    __pydantic_extra__: A dictionary containing extra values, if [`extra`][pydantic.config.ConfigDict.extra]
        is set to `'allow'`.
    __pydantic_fields_set__: The names of fields explicitly set during instantiation.
    __pydantic_private__: Values of private attributes set on the model instance.

- `__init__(self, /, **data: 'Any') -> 'None'`
  Create a new model by parsing and validating input data from keyword arguments.
- `copy(self, *, include: 'AbstractSetIntStr | MappingIntStrAny | None' = None, exclude: 'AbstractSetIntStr | MappingIntStrAny | None' = None, update: 'Dict[str, Any] | None' = None, deep: 'bool' = False) -> 'Self'`
  Returns a copy of the model.
- `dict(self, *, include: 'IncEx | None' = None, exclude: 'IncEx | None' = None, by_alias: 'bool' = False, exclude_unset: 'bool' = False, exclude_defaults: 'bool' = False, exclude_none: 'bool' = False) -> 'Dict[str, Any]'`
- `json(self, *, include: 'IncEx | None' = None, exclude: 'IncEx | None' = None, by_alias: 'bool' = False, exclude_unset: 'bool' = False, exclude_defaults: 'bool' = False, exclude_none: 'bool' = False, encoder: 'Callable[[Any], Any] | None' = PydanticUndefined, models_as_dict: 'bool' = PydanticUndefined, **dumps_kwargs: 'Any') -> 'str'`
- `model_copy(self, *, update: 'Mapping[str, Any] | None' = None, deep: 'bool' = False) -> 'Self'`
  !!! abstract "Usage Documentation"
- `model_dump(self, *, mode: "Literal['json', 'python'] | str" = 'python', include: 'IncEx | None' = None, exclude: 'IncEx | None' = None, context: 'Any | None' = None, by_alias: 'bool | None' = None, exclude_unset: 'bool' = False, exclude_defaults: 'bool' = False, exclude_none: 'bool' = False, exclude_computed_fields: 'bool' = False, round_trip: 'bool' = False, warnings: "bool | Literal['none', 'warn', 'error']" = True, fallback: 'Callable[[Any], Any] | None' = None, serialize_as_any: 'bool' = False) -> 'dict[str, Any]'`
  !!! abstract "Usage Documentation"
- `model_dump_json(self, *, indent: 'int | None' = None, ensure_ascii: 'bool' = False, include: 'IncEx | None' = None, exclude: 'IncEx | None' = None, context: 'Any | None' = None, by_alias: 'bool | None' = None, exclude_unset: 'bool' = False, exclude_defaults: 'bool' = False, exclude_none: 'bool' = False, exclude_computed_fields: 'bool' = False, round_trip: 'bool' = False, warnings: "bool | Literal['none', 'warn', 'error']" = True, fallback: 'Callable[[Any], Any] | None' = None, serialize_as_any: 'bool' = False) -> 'str'`
  !!! abstract "Usage Documentation"
- `model_post_init(self, context: 'Any', /) -> 'None'`
  Override this method to perform additional initialization after `__init__` and `model_construct`.

### `ToolExecuteRequest`

!!! abstract "Usage Documentation"
    [Models](../concepts/models.md)

A base class for creating Pydantic models.

Attributes:
    __class_vars__: The names of the class variables defined on the model.
    __private_attributes__: Metadata about the private attributes of the model.
    __signature__: The synthesized `__init__` [`Signature`][inspect.Signature] of the model.

    __pydantic_complete__: Whether model building is completed, or if there are still undefined fields.
    __pydantic_core_schema__: The core schema of the model.
    __pydantic_custom_init__: Whether the model has a custom `__init__` function.
    __pydantic_decorators__: Metadata containing the decorators defined on the model.
        This replaces `Model.__validators__` and `Model.__root_validators__` from Pydantic V1.
    __pydantic_generic_metadata__: Metadata for generic models; contains data used for a similar purpose to
        __args__, __origin__, __parameters__ in typing-module generics. May eventually be replaced by these.
    __pydantic_parent_namespace__: Parent namespace of the model, used for automatic rebuilding of models.
    __pydantic_post_init__: The name of the post-init method for the model, if defined.
    __pydantic_root_model__: Whether the model is a [`RootModel`][pydantic.root_model.RootModel].
    __pydantic_serializer__: The `pydantic-core` `SchemaSerializer` used to dump instances of the model.
    __pydantic_validator__: The `pydantic-core` `SchemaValidator` used to validate instances of the model.

    __pydantic_fields__: A dictionary of field names and their corresponding [`FieldInfo`][pydantic.fields.FieldInfo] objects.
    __pydantic_computed_fields__: A dictionary of computed field names and their corresponding [`ComputedFieldInfo`][pydantic.fields.ComputedFieldInfo] objects.

    __pydantic_extra__: A dictionary containing extra values, if [`extra`][pydantic.config.ConfigDict.extra]
        is set to `'allow'`.
    __pydantic_fields_set__: The names of fields explicitly set during instantiation.
    __pydantic_private__: Values of private attributes set on the model instance.

- `__init__(self, /, **data: 'Any') -> 'None'`
  Create a new model by parsing and validating input data from keyword arguments.
- `copy(self, *, include: 'AbstractSetIntStr | MappingIntStrAny | None' = None, exclude: 'AbstractSetIntStr | MappingIntStrAny | None' = None, update: 'Dict[str, Any] | None' = None, deep: 'bool' = False) -> 'Self'`
  Returns a copy of the model.
- `dict(self, *, include: 'IncEx | None' = None, exclude: 'IncEx | None' = None, by_alias: 'bool' = False, exclude_unset: 'bool' = False, exclude_defaults: 'bool' = False, exclude_none: 'bool' = False) -> 'Dict[str, Any]'`
- `json(self, *, include: 'IncEx | None' = None, exclude: 'IncEx | None' = None, by_alias: 'bool' = False, exclude_unset: 'bool' = False, exclude_defaults: 'bool' = False, exclude_none: 'bool' = False, encoder: 'Callable[[Any], Any] | None' = PydanticUndefined, models_as_dict: 'bool' = PydanticUndefined, **dumps_kwargs: 'Any') -> 'str'`
- `model_copy(self, *, update: 'Mapping[str, Any] | None' = None, deep: 'bool' = False) -> 'Self'`
  !!! abstract "Usage Documentation"
- `model_dump(self, *, mode: "Literal['json', 'python'] | str" = 'python', include: 'IncEx | None' = None, exclude: 'IncEx | None' = None, context: 'Any | None' = None, by_alias: 'bool | None' = None, exclude_unset: 'bool' = False, exclude_defaults: 'bool' = False, exclude_none: 'bool' = False, exclude_computed_fields: 'bool' = False, round_trip: 'bool' = False, warnings: "bool | Literal['none', 'warn', 'error']" = True, fallback: 'Callable[[Any], Any] | None' = None, serialize_as_any: 'bool' = False) -> 'dict[str, Any]'`
  !!! abstract "Usage Documentation"
- `model_dump_json(self, *, indent: 'int | None' = None, ensure_ascii: 'bool' = False, include: 'IncEx | None' = None, exclude: 'IncEx | None' = None, context: 'Any | None' = None, by_alias: 'bool | None' = None, exclude_unset: 'bool' = False, exclude_defaults: 'bool' = False, exclude_none: 'bool' = False, exclude_computed_fields: 'bool' = False, round_trip: 'bool' = False, warnings: "bool | Literal['none', 'warn', 'error']" = True, fallback: 'Callable[[Any], Any] | None' = None, serialize_as_any: 'bool' = False) -> 'str'`
  !!! abstract "Usage Documentation"
- `model_post_init(self, context: 'Any', /) -> 'None'`
  Override this method to perform additional initialization after `__init__` and `model_construct`.

## Functions

### `get_diagnostics_cache() -> 'Dict[str, Any]'`

GET /v1/diagnostics/cache → requirements-cache health (backend.core.cache_manager).

### `get_diagnostics_hardware(refresh: 'bool' = False) -> 'Dict[str, Any]'`

GET /v1/diagnostics/hardware?refresh=true → CPU/RAM/GPU/storage classification.

### `get_diagnostics_models() -> 'Dict[str, Any]'`

GET /v1/diagnostics/models → per-model ModelInfo for the whole
catalog, without the live compat/speed comparison GET /v1/models
does — this is "what does the backend know about each model file",
not "how does it stack up against this machine".

### `get_diagnostics_performance(threshold_ms: 'float' = 250.0) -> 'Dict[str, Any]'`

GET /v1/diagnostics/performance → instrumented-function timing summary + slow paths.

### `get_diagnostics_plugins() -> 'Dict[str, Any]'`

GET /v1/diagnostics/plugins → aggregated get_diagnostics() from every loaded plugin.

### `get_diagnostics_streaming() -> 'Dict[str, Any]'`

GET /v1/diagnostics/streaming → StreamingEngine capability/identity info.

### `get_diagnostics_transport() -> 'Dict[str, Any]'`

GET /v1/diagnostics/transport → this REST process's own identity/
uptime. Deliberately doesn't report the separate WebSocket server's
connection count — that process has no shared-memory link to this
one (they're two independent OS processes; see backend/rest/server.py's
module docstring), and fabricating a number here would be worse than
not reporting one.

### `get_health() -> 'Dict[str, Any]'`

GET /v1/health → {"status": "ok", "version": <backend version>, "uptime": <seconds>}

"version" is backend.core.self_knowledge.BACKEND_VERSION — the same
constant the chat pipeline's self-knowledge answers already use for
"what version are you" — not a fourth hardcoded copy.

### `get_metrics() -> 'Dict[str, Any]'`

GET /v1/metrics → tokens/sec, latency, throughput, memory, CPU/GPU utilization.

### `get_model_metadata(model_id: 'str') -> 'Dict[str, Any]'`

GET /v1/models/{model_id}/metadata → the unified ModelInfo view
(params, quant, quant_bytes, quant_difficulty, context, requirements,
cache_fingerprint, safety_profile) — see backend.core.model_info.

### `get_model_performance(model_id: 'str') -> 'Dict[str, Any]'`

GET /v1/models/{model_id}/performance → {"model_cfg", "projected_speed_toksec"}

Same estimate_speed() call (and TTL-cached hardware detection) as
ipc_router._handle_model_performance().

### `get_model_requirements(model_id: 'str') -> 'Dict[str, Any]'`

GET /v1/models/{model_id}/requirements → {"model_cfg", "compat"}

Same underlying check_requirements() call as
ipc_router._handle_model_requirements() — including the same warmed
requirements cache, so this is never a second, slower path to the
same answer.

### `get_models() -> 'Dict[str, Any]'`

GET /v1/models → {"models": [...]}

Calls backend.core.model_manager.list_models() directly — the exact
same function backend/ipc_router.py's models_list_request handler
calls for the WebUI's Models page (see
ipc_router._handle_models_list()) — so this can never drift from
what the UI shows.

### `get_plugins_list() -> 'Dict[str, Any]'`

GET /v1/plugins → every loaded plugin's identity + which tools it registered.

### `get_resources() -> 'Dict[str, Any]'`

GET /v1/resources → live CPU%/RAM/VRAM usage (backend.core.resource_monitor).

### `get_tools_list() -> 'Dict[str, Any]'`

GET /v1/tools → every registered tool's schema.

### `get_unified_provider_detail(provider_name: 'str') -> 'Dict[str, Any]'`

GET /v1/providers/unified/{name} → one provider's unified metadata/diagnostics/requirements.

### `get_unified_providers_list() -> 'Dict[str, Any]'`

GET /v1/providers/unified → every provider_registry entry's unified metadata/diagnostics/requirements.

### `list_models_cfgs() -> 'List[Dict[str, Any]]'`

_No docstring provided._

### `post_chat(payload: 'ChatRequest') -> 'Dict[str, Any]'`

POST /v1/chat → synchronous (non-streaming) completion.

Request:  {"message": str, "session_id": str?, "model_id": str?,
           "history": [{"role","content"}]?, "multi_turn": bool = true,
           "skip_safety_check": bool = false}
Response: {"reply": str, "model_id": str, "timestamp": float}
          — or, if the safety gate trips:
          {"type": "safety_warning", "model_id", "severity", "message",
           "projected": {"cpu","ram","vram"}, "suggestions": [...], "timestamp"}

Requires X-ARIA-API-Key (see backend/rest/auth.py).

### `post_chat_stream(payload: 'ChatRequest') -> 'StreamingResponse'`

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

### `post_tool_execute(tool_name: 'str', payload: 'ToolExecuteRequest') -> 'Dict[str, Any]'`

POST /v1/tools/{tool_name}/execute → run a registered tool through
the sandboxed execution path (backend.core.tool_registry.execute_tool()).
Requires X-ARIA-API-Key — tool execution can perform real network
I/O (the built-in weather/search tools do), so this is gated the
same way POST /v1/chat is.
