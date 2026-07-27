"""
ARIA Lite — Unified Config Manager
----------------------------------
Handles loading, saving, and managing ARIA Lite's configuration.
Backed by a simple JSON file, but structured for unified backend usage.
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict


class ConfigManager:
    """
    Unified configuration manager for ARIA Lite.
    Provides:
      - safe load
      - safe save
      - default injection
      - structured config blocks
    """

    DEFAULTS: Dict[str, Any] = {
        "theme": "Dark+",
        "window_geometry": None,
        "unity_root": None,
        "last_project": None,

        # Unified UI blocks
        "ui": {
            "features": {
                "navigation": True,
                "chats": True,
                "plugins": False,
                "quickrun": False,
                "status": False,
            },
            "windows": {
                "main": {
                    "size": None,
                    "position": None,
                    "default_tab": "health",
                    "theme_mode": "match_aria",
                },
                "diagnostics": {
                    "size": None,
                    "position": None,
                    "default_tab": "health",
                    "theme_mode": "match_aria",
                }
            }
        }
    }

    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        self.data: Dict[str, Any] = {}
        self._load()

    # ---------------------------------------------------------
    # LOAD
    # ---------------------------------------------------------
    def _load(self):
        """
        Load config from disk or create defaults.
        Ensures all default keys exist.
        """

        if not self.config_path.exists():
            self.data = self.DEFAULTS.copy()
            self.save()
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            # Corrupted config → reset to defaults
            self.data = self.DEFAULTS.copy()
            self.save()

        # Inject missing defaults
        self._inject_defaults(self.data, self.DEFAULTS)

    def _inject_defaults(self, target: Dict[str, Any], defaults: Dict[str, Any]):
        """
        Recursively inject missing default keys.
        """
        for key, value in defaults.items():
            if key not in target:
                target[key] = value
            else:
                if isinstance(value, dict) and isinstance(target[key], dict):
                    self._inject_defaults(target[key], value)

    # ---------------------------------------------------------
    # SAVE
    # ---------------------------------------------------------
    def save(self):
        """Write config to disk safely."""
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=4)
        except Exception as e:
            print(f"Config save error: {e}")

    # ---------------------------------------------------------
    # PUBLIC API
    # ---------------------------------------------------------
    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value):
        self.data[key] = value

    def delete(self, key: str):
        if key in self.data:
            del self.data[key]
