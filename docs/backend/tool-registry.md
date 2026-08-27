# Tool Registry

Unified tool registry — Phase 2, item 2.

backend.core.tool_executor.py has two real, working tools (weather,
web search) hardcoded into backend/websocket/handlers.py and
backend/rest/router.py's intent-detection branches. There is no
general-purpose registry anywhere in the codebase (confirmed by
research before writing this) — this is genuinely new infrastructure,
not a replacement for anything.

register_tool() takes a ToolSchema (name/description/JSON-schema-like
parameters/permission level) plus a plain callable handler. execute_tool()
is the single path every provider/plugin/pipeline should call through —
it runs the handler inside backend.core.sandbox.run_in_sandbox() (real
timeout, monitored memory) and returns a uniform ToolResult regardless
of which tool ran or how it failed.

The two existing tools are registered below as thin wrappers around
tool_executor.py's real functions — this does NOT change
handlers.py/rest/router.py's existing hand-wired weather/search
short-circuit branches (those keep working exactly as before); it gives
NEW code (providers, plugins, the execution pipeline) one consistent way
to reach the same underlying tools instead of re-deriving the wiring.

## Classes

### `ToolResult`

ToolResult(ok: 'bool', value: 'Any' = None, error: 'Optional[str]' = None, error_code: 'Optional[str]' = None, sandbox: 'Optional[Dict[str, Any]]' = None)

- `__init__(self, ok: 'bool', value: 'Any' = None, error: 'Optional[str]' = None, error_code: 'Optional[str]' = None, sandbox: 'Optional[Dict[str, Any]]' = None) -> None`
  Initialize self.  See help(type(self)) for accurate signature.

### `ToolSchema`

ToolSchema(name: 'str', description: 'str', parameters: 'Dict[str, Dict[str, Any]]' = <factory>, permission: 'str' = 'safe', timeout_seconds: 'float' = 10.0)

- `__init__(self, name: 'str', description: 'str', parameters: 'Dict[str, Dict[str, Any]]' = <factory>, permission: 'str' = 'safe', timeout_seconds: 'float' = 10.0) -> None`
  Initialize self.  See help(type(self)) for accurate signature.
- `to_dict(self) -> 'Dict[str, Any]'`

## Functions

### `execute_tool(name: 'str', args: 'Optional[Dict[str, Any]]' = None, allowed_permissions: 'Optional[set]' = None) -> 'ToolResult'`

The single execution path for every tool, regardless of who's
calling (a provider, a plugin, the unified execution pipeline).
allowed_permissions=None means "everything allowed" (the caller is
fully trusted, e.g. this backend's own built-in tools) — pass an
explicit set (even an empty one) to actually gate less-trusted
callers (a plugin's declared tool access, say).

### `get_tool_schema(name: 'str') -> 'Optional[ToolSchema]'`

_No docstring provided._

### `list_tools() -> 'List[Dict[str, Any]]'`

_No docstring provided._

### `register_builtin_tools() -> 'None'`

_No docstring provided._

### `register_tool(schema: 'ToolSchema', handler: 'Callable[..., Any]') -> 'None'`

_No docstring provided._

### `unregister_tool(name: 'str') -> 'bool'`

_No docstring provided._
