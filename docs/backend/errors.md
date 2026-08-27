# Unified Error System

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

## Classes

### `AriaError`

Base exception for every Phase 2 subsystem. Carries everything
to_error_packet()/to_http_detail() need to produce a fully-formed,
transport-appropriate error response without the caller having to
know which transport it's headed for.

- `__init__(self, code: 'str', message: 'str', request_type: 'Optional[str]' = None, context: 'Dict[str, Any]' = <factory>) -> None`
  Initialize self.  See help(type(self)) for accurate signature.
- `to_dict(self) -> 'Dict[str, Any]'`
- `to_error_packet(self) -> 'Dict[str, Any]'`
  IPC wire shape — a strict superset of ipc_errors.build_error()'s
- `to_http_detail(self) -> 'Dict[str, Any]'`
  REST wire shape — matches backend/rest/router.py's existing

### `CacheError`

Base exception for every Phase 2 subsystem. Carries everything
to_error_packet()/to_http_detail() need to produce a fully-formed,
transport-appropriate error response without the caller having to
know which transport it's headed for.

- `__init__(self, code: 'str', message: 'str', request_type: 'Optional[str]' = None, context: 'Dict[str, Any]' = <factory>) -> None`
  Initialize self.  See help(type(self)) for accurate signature.
- `to_dict(self) -> 'Dict[str, Any]'`
- `to_error_packet(self) -> 'Dict[str, Any]'`
  IPC wire shape — a strict superset of ipc_errors.build_error()'s
- `to_http_detail(self) -> 'Dict[str, Any]'`
  REST wire shape — matches backend/rest/router.py's existing

### `ErrorCode`

New codes for Phase 2 subsystems, alongside backend.ipc_errors's
existing transport-level codes (reused, not duplicated, for anything
that already has one — e.g. UNKNOWN_MODEL, HANDLER_EXCEPTION).


### `PipelineError`

Base exception for every Phase 2 subsystem. Carries everything
to_error_packet()/to_http_detail() need to produce a fully-formed,
transport-appropriate error response without the caller having to
know which transport it's headed for.

- `__init__(self, code: 'str', message: 'str', request_type: 'Optional[str]' = None, context: 'Dict[str, Any]' = <factory>) -> None`
  Initialize self.  See help(type(self)) for accurate signature.
- `to_dict(self) -> 'Dict[str, Any]'`
- `to_error_packet(self) -> 'Dict[str, Any]'`
  IPC wire shape — a strict superset of ipc_errors.build_error()'s
- `to_http_detail(self) -> 'Dict[str, Any]'`
  REST wire shape — matches backend/rest/router.py's existing

### `PluginError`

Base exception for every Phase 2 subsystem. Carries everything
to_error_packet()/to_http_detail() need to produce a fully-formed,
transport-appropriate error response without the caller having to
know which transport it's headed for.

- `__init__(self, code: 'str', message: 'str', request_type: 'Optional[str]' = None, context: 'Dict[str, Any]' = <factory>) -> None`
  Initialize self.  See help(type(self)) for accurate signature.
- `to_dict(self) -> 'Dict[str, Any]'`
- `to_error_packet(self) -> 'Dict[str, Any]'`
  IPC wire shape — a strict superset of ipc_errors.build_error()'s
- `to_http_detail(self) -> 'Dict[str, Any]'`
  REST wire shape — matches backend/rest/router.py's existing

### `ProviderError`

Base exception for every Phase 2 subsystem. Carries everything
to_error_packet()/to_http_detail() need to produce a fully-formed,
transport-appropriate error response without the caller having to
know which transport it's headed for.

- `__init__(self, code: 'str', message: 'str', request_type: 'Optional[str]' = None, context: 'Dict[str, Any]' = <factory>) -> None`
  Initialize self.  See help(type(self)) for accurate signature.
- `to_dict(self) -> 'Dict[str, Any]'`
- `to_error_packet(self) -> 'Dict[str, Any]'`
  IPC wire shape — a strict superset of ipc_errors.build_error()'s
- `to_http_detail(self) -> 'Dict[str, Any]'`
  REST wire shape — matches backend/rest/router.py's existing

### `ToolError`

Base exception for every Phase 2 subsystem. Carries everything
to_error_packet()/to_http_detail() need to produce a fully-formed,
transport-appropriate error response without the caller having to
know which transport it's headed for.

- `__init__(self, code: 'str', message: 'str', request_type: 'Optional[str]' = None, context: 'Dict[str, Any]' = <factory>) -> None`
  Initialize self.  See help(type(self)) for accurate signature.
- `to_dict(self) -> 'Dict[str, Any]'`
- `to_error_packet(self) -> 'Dict[str, Any]'`
  IPC wire shape — a strict superset of ipc_errors.build_error()'s
- `to_http_detail(self) -> 'Dict[str, Any]'`
  REST wire shape — matches backend/rest/router.py's existing

## Functions

### `categorize(code: 'str') -> 'str'`

_No docstring provided._

### `recovery_suggestion(code: 'str') -> 'Optional[str]'`

_No docstring provided._
