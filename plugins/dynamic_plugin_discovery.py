import json
import importlib
from pathlib import Path
from .base_plugin import BasePlugin

from logger import get_logger

logger = get_logger(__name__)


class DynamicPluginDiscoveryEngine:
    """
    Unified Plugin Discovery Engine for ARIA Lite.
    Scans /plugins for valid plugin folders containing:
      - plugin.py
      - manifest.json

    Returns structured metadata:
      {
        "name": ...,
        "version": ...,
        "author": ...,
        "description": ...,
        "path": ...,
        "module": ...,
        "class": ...,
        "manifest": ...
      }
    """

    def __init__(self, project_root: str):
        self.project_root = Path(project_root)
        self.plugins_dir = self.project_root / "plugins"

    def discover(self) -> list:
        discovered = []

        if not self.plugins_dir.exists():
            return discovered

        for item in self.plugins_dir.iterdir():
            if not item.is_dir():
                continue

            plugin_py = item / "plugin.py"
            manifest_json = item / "manifest.json"

            if not plugin_py.exists() or not manifest_json.exists():
                continue

            plugin_name = item.name

            try:
                # Load manifest
                manifest = json.loads(manifest_json.read_text(encoding="utf-8"))

                # Import plugin module
                module = importlib.import_module(f"plugins.{plugin_name}.plugin")

                # Find plugin class
                plugin_class = None
                for attr in dir(module):
                    obj = getattr(module, attr)
                    if (
                        isinstance(obj, type)
                        and issubclass(obj, BasePlugin)
                        and obj is not BasePlugin
                    ):
                        plugin_class = obj
                        break

                discovered.append({
                    "name": manifest.get("name", plugin_name),
                    "version": manifest.get("version", "0.0.0"),
                    "author": manifest.get("author", "Unknown"),
                    "description": manifest.get("description", ""),
                    "path": str(item),
                    "module": module,
                    "class": plugin_class,
                    "manifest": manifest,
                })

            except Exception as e:
                logger.exception(f"[DynamicPluginDiscovery] Failed to load plugin '{plugin_name}': {e}")

        return discovered
