"""
ARIA Lite — Unified Path Manager
--------------------------------
Handles:
  - Unity root paths
  - Project paths
  - General filesystem resolution
  - Config persistence
"""

from __future__ import annotations
from pathlib import Path
from typing import Optional, Dict, Any


# ---------------------------------------------------------
# Asset Paths (module-level constants)
# ---------------------------------------------------------
ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"


class PathManager:
    """
    Unified PathManager for ARIA Lite.
    Integrates with ConfigManager for persistence.
    """

    def __init__(self, base_dir: Path, config_manager=None):
        self.base_dir = Path(base_dir).expanduser().resolve()
        self.config = config_manager

        # Load persisted values if config is available
        if self.config:
            self.unity_root = self._load_path("unity_root")
            self.project_path = self._load_path("last_project")
        else:
            self.unity_root = None
            self.project_path = None

    # ---------------------------------------------------------
    # Internal loader
    # ---------------------------------------------------------
    def _load_path(self, key: str) -> Optional[Path]:
        value = self.config.get(key) if self.config else None
        if not value:
            return None

        p = Path(value).expanduser().resolve()
        return p if p.exists() else None

    # ---------------------------------------------------------
    # Unity Root
    # ---------------------------------------------------------
    def set_unity_root(self, path: str) -> Dict[str, Any]:
        p = Path(path).expanduser().resolve()

        if not p.exists():
            return {
                "status": "error",
                "operation": "set_unity_root",
                "detail": f"Path does not exist: {p}"
            }

        self.unity_root = p

        if self.config:
            self.config.set("unity_root", str(p))
            self.config.save()

        return {
            "status": "ok",
            "operation": "set_unity_root",
            "unity_root": str(p)
        }

    def get_unity_root(self) -> Optional[Path]:
        return self.unity_root

    # ---------------------------------------------------------
    # Project Path
    # ---------------------------------------------------------
    def set_project_path(self, path: str) -> Dict[str, Any]:
        p = Path(path).expanduser().resolve()

        if not p.exists():
            return {
                "status": "error",
                "operation": "set_project_path",
                "detail": f"Path does not exist: {p}"
            }

        self.project_path = p

        if self.config:
            self.config.set("last_project", str(p))
            self.config.save()

        return {
            "status": "ok",
            "operation": "set_project_path",
            "project_path": str(p)
        }

    def get_project_path(self) -> Optional[Path]:
        return self.project_path

    # ---------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------
    def resolve(self, path: str) -> Path:
        """
        Resolve a path relative to the base directory.
        """
        return (self.base_dir / path).expanduser().resolve()

    def exists(self, path: str) -> bool:
        """
        Check if a path exists.
        """
        return Path(path).expanduser().resolve().exists()
