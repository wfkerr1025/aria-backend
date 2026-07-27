"""
ARIA Lite — Unified Plugin Loader
---------------------------------
Loads plugins from subdirectories containing plugin.json.
Each plugin defines:
  - entry: Python module file
  - class: class name inside that module
"""

from __future__ import annotations
import json
import importlib
from pathlib import Path
from typing import Any, Dict, List


BASE_DIR = Path(__file__).resolve().parent.parent


def load_plugins() -> List[Any]:
    """
    Unified plugin loader.
    Returns a list of instantiated plugin classes.
    """

    plugins = []

    for entry in BASE_DIR.iterdir():
        if not entry.is_dir():
            continue

        manifest = entry / "plugin.json"
        if not manifest.exists():
            continue

        result = _load_plugin_from_manifest(entry.name, manifest)

        # Only append successful loads
        if result.get("status") == "ok":
            plugins.append(result["instance"])
        else:
            print(f"[PLUGIN] Failed to load {entry.name}: {result.get('detail')}")

    return plugins


def _load_plugin_from_manifest(folder_name: str, manifest_path: Path) -> Dict[str, Any]:
    """
    Loads a plugin from its manifest.
    Returns a unified result dictionary.
    """

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {
            "status": "error",
            "operation": "load_plugin_manifest",
            "detail": f"Invalid JSON in {manifest_path}: {e}",
        }

    # Validate manifest fields
    if "entry" not in data or "class" not in data:
        return {
            "status": "error",
            "operation": "validate_manifest",
            "detail": f"Manifest missing required fields: {manifest_path}",
        }

    module_name = data["entry"].replace(".py", "")
    class_name = data["class"]

    try:
        module = importlib.import_module(f"{folder_name}.{module_name}")
    except Exception as e:
        return {
            "status": "error",
            "operation": "import_module",
            "detail": f"Failed to import module '{folder_name}.{module_name}': {e}",
        }

    try:
        cls = getattr(module, class_name)
    except Exception as e:
        return {
            "status": "error",
            "operation": "load_class",
            "detail": f"Class '{class_name}' not found in module '{module_name}': {e}",
        }

    try:
        instance = cls()
    except Exception as e:
        return {
            "status": "error",
            "operation": "instantiate_plugin",
            "detail": f"Failed to instantiate plugin '{class_name}': {e}",
        }

    return {
        "status": "ok",
        "operation": "load_plugin",
        "folder": folder_name,
        "module": module_name,
        "class": class_name,
        "instance": instance,
    }
