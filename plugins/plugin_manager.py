# plugins/plugin_manager.py

import os
import json
import traceback
from pathlib import Path

from .base_plugin import BasePlugin
from .dynamic_plugin_discovery import DynamicPluginDiscoveryEngine


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

    print(f"[PluginManager ERROR] {plugin_name}: {exception}")
    print(tb)


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
        print("[PluginManager] Discovering plugins inside workspace:", PROJECT_ROOT)

        discovered = self.discovery.discover()

        for meta in discovered:
            plugin_name = meta["name"]
            plugin_path = meta["path"]
            plugin_class = meta["class"]
            manifest = meta["manifest"]

            print(f"[PluginManager] Loading plugin: {plugin_name}")

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
                print(f"[PluginManager] Loaded plugin: {plugin_name}")

            except Exception as e:
                _log_plugin_error(plugin_name, "Instantiate Plugin", e)
                self.failed_plugins[plugin_name] = "Failed to instantiate plugin"
                continue

        # Sync loaded plugins to UI-facing dict
        self.plugins = self.loaded_plugins

        print("[PluginManager] Total plugins loaded:", len(self.plugins))
        print("[PluginManager] Total plugins failed:", len(self.failed_plugins))

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
