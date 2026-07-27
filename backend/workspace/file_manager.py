# backend/workspace/file_manager.py

import os
import time
import traceback


class FileManager:
    """
    Backend workspace file manager for ARIA Lite.
    - Pure backend (no UI)
    - Safe file operations
    - Rooted in a workspace directory
    """

    REQUIRES = ["config"]
    FORBIDDEN = ["root", "window", "theme_manager", "chat_panel"]

    def __init__(self, app):
        self.app = app
        self.config = app.config

        self.workspace_root = self._resolve_workspace_root()
        os.makedirs(self.workspace_root, exist_ok=True)

    def _resolve_workspace_root(self) -> str:
        base = getattr(self.config, "workspace_root", None)
        if not base:
            base = os.path.join(os.getcwd(), "workspace")
        return base

    # ---------------------------------------------------------
    # PATH HELPERS
    # ---------------------------------------------------------
    def _abs_path(self, relative_path: str) -> str:
        return os.path.join(self.workspace_root, relative_path.replace("\\", "/"))

    # ---------------------------------------------------------
    # FILE OPERATIONS
    # ---------------------------------------------------------
    def read_file(self, relative_path: str, encoding: str = "utf-8") -> str | None:
        try:
            full_path = self._abs_path(relative_path)
            if not os.path.exists(full_path):
                return None
            with open(full_path, "r", encoding=encoding) as f:
                return f.read()
        except Exception:
            traceback.print_exc()
            return None

    def write_file(self, relative_path: str, content: str, encoding: str = "utf-8") -> bool:
        try:
            full_path = self._abs_path(relative_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding=encoding) as f:
                f.write(content)
            return True
        except Exception:
            traceback.print_exc()
            return False

    def append_file(self, relative_path: str, content: str, encoding: str = "utf-8") -> bool:
        try:
            full_path = self._abs_path(relative_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "a", encoding=encoding) as f:
                f.write(content)
            return True
        except Exception:
            traceback.print_exc()
            return False

    def delete_file(self, relative_path: str) -> bool:
        try:
            full_path = self._abs_path(relative_path)
            if os.path.exists(full_path):
                os.remove(full_path)
            return True
        except Exception:
            traceback.print_exc()
            return False

    def list_dir(self, relative_path: str = "") -> list[str]:
        try:
            full_path = self._abs_path(relative_path)
            if not os.path.exists(full_path):
                return []
            return sorted(os.listdir(full_path))
        except Exception:
            traceback.print_exc()
            return []

    def file_exists(self, relative_path: str) -> bool:
        return os.path.exists(self._abs_path(relative_path))
