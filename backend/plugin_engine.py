from __future__ import annotations
from typing import Dict, Any, Callable, Optional


from logger import get_logger

logger = get_logger(__name__)

class Plugin:
    """
    Unified plugin descriptor.
    Each plugin has:
      - name
      - handler(envelope) -> dict
    """

    def __init__(self, name: str, handler: Callable[[Dict[str, Any]], Dict[str, Any]]):
        logger.debug(f"Creating Plugin → {name}")
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
        logger.debug("Initializing PluginEngine")
        self.plugins: Dict[str, Plugin] = {}

    # ---------------------------------------------------------
    # REGISTER PLUGIN
    # ---------------------------------------------------------
    def register_plugin(self, name: str, handler: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
        logger.debug(f"register_plugin() → {name}")
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
        logger.debug(f"unregister_plugin() → {name}")

        if name in self.plugins:
            del self.plugins[name]
            logger.debug(f"Plugin unregistered → {name}")
            return {
                "status": "ok",
                "operation": "unregister_plugin",
                "plugin": name
            }

        logger.error(f"unregister_plugin() ERROR → Plugin not found: {name}")
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
        logger.debug("list_plugins()")
        return {
            "status": "ok",
            "operation": "list_plugins",
            "plugins": list(self.plugins.keys())
        }

    # ---------------------------------------------------------
    # HANDLE PLUGIN
    # ---------------------------------------------------------
    def handle_plugin(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        logger.debug(f"handle_plugin() → envelope={envelope}")

        plugin_name = envelope.get("plugin")

        if not plugin_name:
            logger.error("ERROR → Missing 'plugin' field")
            return {
                "status": "error",
                "operation": "handle_plugin",
                "detail": "Plugin envelope missing 'plugin' field"
            }

        plugin = self.plugins.get(plugin_name)

        if not plugin:
            logger.error(f"ERROR → Plugin not registered: {plugin_name}")
            return {
                "status": "error",
                "operation": "handle_plugin",
                "plugin": plugin_name,
                "detail": f"Plugin '{plugin_name}' not registered"
            }

        try:
            logger.debug(f"Dispatching to plugin handler → {plugin_name}")
            result = plugin.handler(envelope)

            if not isinstance(result, dict):
                logger.error(f"ERROR → Plugin returned non-dict result: {plugin_name}")
                return {
                    "status": "error",
                    "operation": "handle_plugin",
                    "plugin": plugin_name,
                    "detail": "Plugin returned non-dict result"
                }

            if "status" not in result:
                result["status"] = "ok"

            result.setdefault("operation", "plugin_handler")
            result.setdefault("plugin", plugin_name)

            logger.debug(f"Plugin handler OK → {plugin_name}")
            return result

        except Exception as e:
            logger.error(f"Plugin handler ERROR → {plugin_name}: {e}")
            return {
                "status": "error",
                "operation": "handle_plugin",
                "plugin": plugin_name,
                "detail": str(e)
            }
