# Plugin System

Steam-style plugin system integration — Phase 2, item 3.

Research before writing this found THREE non-interoperating plugin
loaders already in the repo: plugins/plugin_manager.py (+ base_plugin.py
+ dynamic_plugin_discovery.py) using manifest.json with real shipped
plugins (blender/unity/unreal/wordpress), core/plugin_loader.py (a
different plugin.json shape, live at the desktop app's own startup path
in root app.py), and backend/plugin_engine.py (confirmed dead code,
nothing imports it).

This wraps plugins/plugin_manager.py specifically — it's the richest
one (real shipped plugins, a real BasePlugin contract) and is NOT
currently invoked automatically anywhere (its load_plugins() is a
one-shot method nobody calls at startup). This module is what actually
achieves "hot-loading at startup": load_all() is called once during
this backend's own startup (backend/ws_server.py), independent of
core/plugin_loader.py's desktop-app path, which is left completely
alone — this backend process and the desktop Electron/WinForms shell
are separate processes with separate lifecycles.

Plugins integrate with the tool registry and provider system through
BasePlugin's new get_tools()/get_models()/get_diagnostics() hooks
(plugins/base_plugin.py) — every hook call is wrapped in
backend.core.sandbox so one badly-behaved plugin can't hang or crash
the whole load.

## Functions

### `get_plugin_diagnostics() -> 'Dict[str, Any]'`

Aggregated get_diagnostics() from every loaded plugin, sandboxed per-plugin.

### `get_plugin_models() -> 'List[Dict[str, Any]]'`

Aggregated get_models() from every loaded plugin, sandboxed per-plugin.

### `list_failed_plugins() -> 'Dict[str, str]'`

_No docstring provided._

### `list_plugins() -> 'List[Dict[str, Any]]'`

_No docstring provided._

### `load_all() -> 'Dict[str, Any]'`

Discover + instantiate every plugin under plugins/*, then pull in
any tools they declare via get_tools(). Never raises — a completely
absent/broken plugins/ directory just means zero plugins loaded, not
a startup failure (matching this backend's established "optional
subsystem failures are logged and swallowed" convention — see
AriaLauncher's REST API startup for the same pattern on the launcher
side).

### `reload_all() -> 'Dict[str, Any]'`

_No docstring provided._

### `run_plugin_command(plugin_name: 'str', command_id: 'str', args: 'Optional[Dict[str, Any]]' = None) -> 'Any'`

Execute one of a loaded plugin's declared commands (get_commands()),
sandboxed the same way tool execution is. Raises PluginError (never a
raw exception) so callers get the unified error shape.

### `unload_plugin_tools(plugin_name: 'str') -> 'int'`

Unregister whatever tools load_all() registered for one plugin — used by reload_all().
