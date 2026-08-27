# plugins/plugin_manager.py

import os
import json
import traceback
from pathlib import Path

from .base_plugin import BasePlugin
from .dynamic_plugin_discovery import DynamicPluginDiscoveryEngine

from logger import get_logger

logger = get_logger(__name__)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------
# Error Logging
# ---------------------------------------------------------
def _log_plugin_error(plugin_name, label, exception):
    """Write plugin errors to logs/plugin_errors.log."""
    os.makedirs("logs", exist_ok=True)
    tb = traceback.format_exc()

    with open("logs/plugin_errors.log", "a", encoding="utf-8") as f:
        f.write(f"\n[{plugin_name}] ERROR during '{label}': {exception}\n")
        f.write(tb + "\n")

    logger.exception(f"[PluginManager ERROR] {plugin_name}: {exception}")


# ---------------------------------------------------------
# Plugin Manager
# ---------------------------------------------------------
class PluginManager:
    """
    Unified, fault‑tolerant plugin loader for ARIA Lite.

    Responsibilities:
      - Discover plugins via DynamicPluginDiscoveryEngine
      - Safely load plugin classes
      - Safely load manifest.json
      - Safely instantiate plugin instances
      - Log errors instead of crashing ARIA Lite
      - Provide unified UI + command registry
    """

    def __init__(self, aria_window=None):
        self.aria_window = aria_window
        self.discovery = DynamicPluginDiscoveryEngine(str(PROJECT_ROOT))

        self.loaded_plugins = {}   # plugin_name → instance
        self.failed_plugins = {}   # plugin_name → error message
        self.plugins = {}          # UI-facing dict

    # ---------------------------------------------------------
    # Load Plugins (Unified + Fault‑Tolerant)
    # ---------------------------------------------------------
    def load_plugins(self):
        logger.info("[PluginManager] Discovering plugins inside workspace: %s", PROJECT_ROOT)

        discovered = self.discovery.discover()

        for meta in discovered:
            plugin_name = meta["name"]
            plugin_path = meta["path"]
            plugin_class = meta["class"]
            manifest = meta["manifest"]

            logger.info(f"[PluginManager] Loading plugin: {plugin_name}")

            # ---------------------------------------------------------
            # Validate plugin class
            # ---------------------------------------------------------
            if plugin_class is None:
                self.failed_plugins[plugin_name] = "No valid plugin class found"
                continue

            # ---------------------------------------------------------
            # Instantiate plugin safely
            # ---------------------------------------------------------
            try:
                instance = plugin_class(manifest, plugin_path)
                instance.on_load()

                self.loaded_plugins[plugin_name] = instance
                logger.info(f"[PluginManager] Loaded plugin: {plugin_name}")

            except Exception as e:
                _log_plugin_error(plugin_name, "Instantiate Plugin", e)
                self.failed_plugins[plugin_name] = "Failed to instantiate plugin"
                continue

        # Sync loaded plugins to UI-facing dict
        self.plugins = self.loaded_plugins

        logger.info("[PluginManager] Total plugins loaded: %s", len(self.plugins))
        logger.info("[PluginManager] Total plugins failed: %s", len(self.failed_plugins))

        return list(self.plugins.values())

    # ---------------------------------------------------------
    # UI Helpers
    # ---------------------------------------------------------
    def get_menu_tabs(self):
        tabs = []

        for plugin in self.plugins.values():
            try:
                tabs.extend(plugin.get_menu_tabs())
            except Exception as e:
                _log_plugin_error(plugin.__class__.__name__, "get_menu_tabs", e)

        return tabs

    def get_commands(self):
        commands = {}

        for plugin in self.plugins.values():
            try:
                commands.update(plugin.get_commands())
            except Exception as e:
                _log_plugin_error(plugin.__class__.__name__, "get_commands", e)

        return commands


# ============================================================
# STEP 3 — IPC ENTRY POINT
# ============================================================
def run(command: str, **kwargs):
    """
    Unified IPC entry point for the plugin manager.

    Supported commands:
      - "load_plugins"     → discover + load all plugins
      - "list_plugins"     → list loaded plugin names
      - "list_failed"      → list failed plugin names
      - "get_tabs"         → return UI menu tabs from plugins
      - "get_commands"     → return command registry from plugins
      - "run_plugin"       → run a specific plugin command
    """

    try:
        # Lazy import to avoid circular dependency
        from backend.app import app_instance
        manager = app_instance.plugins

        if command == "load_plugins":
            loaded = manager.load_plugins()
            return {
                "status": "ok",
                "operation": "plugins.load_plugins",
                "loaded": [p.__class__.__name__ for p in loaded],
                "failed": list(manager.failed_plugins.keys())
            }

        if command == "list_plugins":
            return {
                "status": "ok",
                "operation": "plugins.list_plugins",
                "plugins": list(manager.loaded_plugins.keys())
            }

        if command == "list_failed":
            return {
                "status": "ok",
                "operation": "plugins.list_failed",
                "failed": manager.failed_plugins
            }

        if command == "get_tabs":
            tabs = manager.get_menu_tabs()
            return {
                "status": "ok",
                "operation": "plugins.get_tabs",
                "tabs": tabs
            }

        if command == "get_commands":
            cmds = manager.get_commands()
            return {
                "status": "ok",
                "operation": "plugins.get_commands",
                "commands": cmds
            }

        if command == "run_plugin":
            plugin_name = kwargs.get("plugin")
            plugin_cmd = kwargs.get("command")
            args = kwargs.get("args", {})
            plugin = manager.loaded_plugins.get(plugin_name)

            if plugin is None:
                return {
                    "status": "error",
                    "operation": "plugins.run_plugin",
                    "detail": f"Plugin '{plugin_name}' not loaded"
                }

            try:
                result = plugin.run(plugin_cmd, **args)
                return {
                    "status": "ok",
                    "operation": "plugins.run_plugin",
                    "plugin": plugin_name,
                    "command": plugin_cmd,
                    "result": result
                }
            except Exception as e:
                return {
                    "status": "error",
                    "operation": "plugins.run_plugin",
                    "detail": str(e)
                }

        return {
            "status": "error",
            "operation": "plugins",
            "detail": f"Unknown command '{command}'"
        }

    except Exception as e:
        return {
            "status": "error",
            "operation": "plugins",
            "detail": str(e)
        }
