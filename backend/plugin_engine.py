from __future__ import annotations
from typing import Dict, Any, Callable, Optional


class Plugin:
    """
    Unified plugin descriptor.
    Each plugin has:
      - name
      - handler(envelope) -> dict
    """

    def __init__(self, name: str, handler: Callable[[Dict[str, Any]], Dict[str, Any]]):
        self.name = name
        self.handler = handler


class PluginEngine:
    """
    Unified plugin engine for ARIA Lite.
    Manages:
      - plugin registry
      - plugin dispatch
      - safe handler execution
      - structured return values
    """

    def __init__(self):
        self.plugins: Dict[str, Plugin] = {}

    # ---------------------------------------------------------
    # REGISTER PLUGIN
    # ---------------------------------------------------------
    def register_plugin(self, name: str, handler: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
        self.plugins[name] = Plugin(name, handler)
        return {
            "status": "ok",
            "operation": "register_plugin",
            "plugin": name
        }

    # ---------------------------------------------------------
    # UNREGISTER PLUGIN
    # ---------------------------------------------------------
    def unregister_plugin(self, name: str) -> Dict[str, Any]:
        if name in self.plugins:
            del self.plugins[name]
            return {
                "status": "ok",
                "operation": "unregister_plugin",
                "plugin": name
            }

        return {
            "status": "error",
            "operation": "unregister_plugin",
            "plugin": name,
            "detail": "Plugin not found"
        }

    # ---------------------------------------------------------
    # LIST PLUGINS
    # ---------------------------------------------------------
    def list_plugins(self) -> Dict[str, Any]:
        return {
            "status": "ok",
            "operation": "list_plugins",
            "plugins": list(self.plugins.keys())
        }

    # ---------------------------------------------------------
    # HANDLE PLUGIN
    # ---------------------------------------------------------
    def handle_plugin(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        """
        Envelope format:
        {
          "task": "plugin",
          "plugin": "name",
          "data": {...}
        }
        """

        plugin_name = envelope.get("plugin")

        if not plugin_name:
            return {
                "status": "error",
                "operation": "handle_plugin",
                "detail": "Plugin envelope missing 'plugin' field"
            }

        plugin = self.plugins.get(plugin_name)

        if not plugin:
            return {
                "status": "error",
                "operation": "handle_plugin",
                "plugin": plugin_name,
                "detail": f"Plugin '{plugin_name}' not registered"
            }

        try:
            result = plugin.handler(envelope)

            # Ensure plugin returns a dict
            if not isinstance(result, dict):
                return {
                    "status": "error",
                    "operation": "handle_plugin",
                    "plugin": plugin_name,
                    "detail": "Plugin returned non-dict result"
                }

            # Normalize plugin result
            if "status" not in result:
                result["status"] = "ok"

            result.setdefault("operation", "plugin_handler")
            result.setdefault("plugin", plugin_name)

            return result

        except Exception as e:
            return {
                "status": "error",
                "operation": "handle_plugin",
                "plugin": plugin_name,
                "detail": str(e)
            }
