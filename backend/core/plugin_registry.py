# backend/core/plugin_registry.py

"""
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
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from plugins.plugin_manager import PluginManager
from .sandbox import run_in_sandbox, SandboxLimits
from . import tool_registry
from .errors import PluginError, ErrorCode
from . import perf_profiler

from logger import get_logger

logger = get_logger(__name__)

_PLUGIN_HOOK_TIMEOUT_SECONDS = 5.0

_manager: Optional[PluginManager] = None
_registered_tool_names_by_plugin: Dict[str, List[str]] = {}


def _get_manager() -> PluginManager:
    global _manager
    if _manager is None:
        _manager = PluginManager()
    return _manager


def load_all() -> Dict[str, Any]:
    """
    Discover + instantiate every plugin under plugins/*, then pull in
    any tools they declare via get_tools(). Never raises — a completely
    absent/broken plugins/ directory just means zero plugins loaded, not
    a startup failure (matching this backend's established "optional
    subsystem failures are logged and swallowed" convention — see
    AriaLauncher's REST API startup for the same pattern on the launcher
    side).
    """
    manager = _get_manager()

    with perf_profiler.timed("plugin_registry.load_all"):
        try:
            manager.load_plugins()
        except Exception as e:
            logger.error(f"plugin_registry.load_all() → PluginManager.load_plugins() failed: {e}")
            return {"loaded": [], "failed": {"__manager__": str(e)}, "tools_registered": []}

    tools_registered: List[str] = []
    for name, plugin in manager.loaded_plugins.items():
        tools_registered.extend(_register_plugin_tools(name, plugin))

    logger.info(
        f"plugin_registry.load_all() → {len(manager.loaded_plugins)} loaded, "
        f"{len(manager.failed_plugins)} failed, {len(tools_registered)} tool(s) registered"
    )

    return {
        "loaded": list(manager.loaded_plugins.keys()),
        "failed": dict(manager.failed_plugins),
        "tools_registered": tools_registered,
    }


def _register_plugin_tools(plugin_name: str, plugin: Any) -> List[str]:
    result = run_in_sandbox(plugin.get_tools, limits=SandboxLimits(timeout_seconds=_PLUGIN_HOOK_TIMEOUT_SECONDS))
    if not result.ok:
        logger.warning(f"plugin_registry: {plugin_name}.get_tools() failed/timed out: {result.error}")
        return []

    registered = []
    for schema, handler in (result.value or []):
        try:
            tool_registry.register_tool(schema, handler)
            registered.append(schema.name)
        except Exception as e:
            logger.warning(f"plugin_registry: failed to register tool '{getattr(schema, 'name', '?')}' from {plugin_name}: {e}")

    _registered_tool_names_by_plugin[plugin_name] = registered
    return registered


def unload_plugin_tools(plugin_name: str) -> int:
    """Unregister whatever tools load_all() registered for one plugin — used by reload_all()."""
    names = _registered_tool_names_by_plugin.pop(plugin_name, [])
    for name in names:
        tool_registry.unregister_tool(name)
    return len(names)


def reload_all() -> Dict[str, Any]:
    global _manager
    for plugin_name in list(_registered_tool_names_by_plugin.keys()):
        unload_plugin_tools(plugin_name)
    _manager = None
    return load_all()


def list_plugins() -> List[Dict[str, Any]]:
    manager = _get_manager()
    return [
        {
            "name": plugin.get_name(),
            "version": plugin.get_version(),
            "author": plugin.get_author(),
            "description": plugin.get_description(),
            "tools": _registered_tool_names_by_plugin.get(key, []),
        }
        for key, plugin in manager.loaded_plugins.items()
    ]


def list_failed_plugins() -> Dict[str, str]:
    return dict(_get_manager().failed_plugins)


def get_plugin_diagnostics() -> Dict[str, Any]:
    """Aggregated get_diagnostics() from every loaded plugin, sandboxed per-plugin."""
    manager = _get_manager()
    diagnostics: Dict[str, Any] = {}
    for name, plugin in manager.loaded_plugins.items():
        result = run_in_sandbox(plugin.get_diagnostics, limits=SandboxLimits(timeout_seconds=_PLUGIN_HOOK_TIMEOUT_SECONDS))
        diagnostics[name] = result.value if result.ok else {"error": result.error}
    return diagnostics


def get_plugin_models() -> List[Dict[str, Any]]:
    """Aggregated get_models() from every loaded plugin, sandboxed per-plugin."""
    manager = _get_manager()
    models: List[Dict[str, Any]] = []
    for name, plugin in manager.loaded_plugins.items():
        result = run_in_sandbox(plugin.get_models, limits=SandboxLimits(timeout_seconds=_PLUGIN_HOOK_TIMEOUT_SECONDS))
        if result.ok and result.value:
            models.extend(result.value)
        elif not result.ok:
            logger.debug(f"plugin_registry: {name}.get_models() failed/timed out: {result.error}")
    return models


def run_plugin_command(plugin_name: str, command_id: str, args: Optional[Dict[str, Any]] = None) -> Any:
    """
    Execute one of a loaded plugin's declared commands (get_commands()),
    sandboxed the same way tool execution is. Raises PluginError (never a
    raw exception) so callers get the unified error shape.
    """
    manager = _get_manager()
    plugin = manager.loaded_plugins.get(plugin_name)
    if plugin is None:
        raise PluginError(code=ErrorCode.PLUGIN_NOT_FOUND, message=f"Plugin '{plugin_name}' is not loaded")

    commands = plugin.get_commands()
    handler = commands.get(command_id)
    if handler is None:
        raise PluginError(
            code=ErrorCode.PLUGIN_NOT_FOUND,
            message=f"Plugin '{plugin_name}' has no command '{command_id}'",
            context={"available_commands": list(commands.keys())},
        )

    result = run_in_sandbox(handler, **(args or {}))
    if not result.ok:
        raise PluginError(
            code=ErrorCode.PLUGIN_LOAD_FAILED,
            message=f"Plugin command '{plugin_name}.{command_id}' failed: {result.error}",
        )
    return result.value
