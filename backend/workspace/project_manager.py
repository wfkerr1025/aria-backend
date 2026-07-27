# backend/workspace/project_manager.py

import os
import json
import time
import traceback

from backend.workspace.file_manager import FileManager


class ProjectManager:
    """
    Backend project manager for ARIA Lite.
    - Manages project metadata and structure
    - Uses FileManager for actual file I/O
    """

    REQUIRES = ["config"]
    FORBIDDEN = ["root", "window", "theme_manager", "chat_panel"]

    def __init__(self, app):
        self.app = app
        self.config = app.config

        self.file_manager = FileManager(app)
        self.projects_root = self._resolve_projects_root()
        os.makedirs(self.projects_root, exist_ok=True)

    def _resolve_projects_root(self) -> str:
        base = getattr(self.config, "projects_root", None)
        if not base:
            base = os.path.join(self.file_manager.workspace_root, "projects")
        return base

    # ---------------------------------------------------------
    # PROJECT PATH HELPERS
    # ---------------------------------------------------------
    def _project_path(self, project_name: str) -> str:
        return os.path.join(self.projects_root, project_name)

    def _project_meta_path(self, project_name: str) -> str:
        return os.path.join(self._project_path(project_name), "aria_project.json")

    # ---------------------------------------------------------
    # PROJECT LIFECYCLE
    # ---------------------------------------------------------
    def create_project(self, project_name: str, metadata: dict | None = None) -> bool:
        try:
            root = self._project_path(project_name)
            os.makedirs(root, exist_ok=True)

            meta = metadata or {}
            meta.setdefault("name", project_name)
            meta.setdefault("created_at", time.ctime())
            meta.setdefault("files", [])

            with open(self._project_meta_path(project_name), "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)

            return True
        except Exception:
            traceback.print_exc()
            return False

    def load_project_metadata(self, project_name: str) -> dict | None:
        try:
            meta_path = self._project_meta_path(project_name)
            if not os.path.exists(meta_path):
                return None
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            traceback.print_exc()
            return None

    def list_projects(self) -> list[str]:
        try:
            if not os.path.exists(self.projects_root):
                return []
            return sorted(
                d for d in os.listdir(self.projects_root)
                if os.path.isdir(os.path.join(self.projects_root, d))
            )
        except Exception:
            traceback.print_exc()
            return []

    def delete_project(self, project_name: str) -> bool:
        try:
            root = self._project_path(project_name)
            if not os.path.exists(root):
                return True

            for dirpath, dirnames, filenames in os.walk(root, topdown=False):
                for filename in filenames:
                    os.remove(os.path.join(dirpath, filename))
                for dirname in dirnames:
                    os.rmdir(os.path.join(dirpath, dirname))
            os.rmdir(root)
            return True
        except Exception:
            traceback.print_exc()
            return False

    # ---------------------------------------------------------
    # PROJECT FILE OPERATIONS
    # ---------------------------------------------------------
    def add_file_to_project(self, project_name: str, relative_path: str, content: str) -> bool:
        try:
            project_root = self._project_path(project_name)
            rel = os.path.join("projects", project_name, relative_path)
            full_path = os.path.join(self.file_manager.workspace_root, rel)

            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)

            meta = self.load_project_metadata(project_name) or {}
            files = meta.get("files", [])
            if relative_path not in files:
                files.append(relative_path)
            meta["files"] = files

            with open(self._project_meta_path(project_name), "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)

            return True
        except Exception:
            traceback.print_exc()
            return False

    def read_project_file(self, project_name: str, relative_path: str) -> str | None:
        rel = os.path.join("projects", project_name, relative_path)
        return self.file_manager.read_file(rel)

    def list_project_files(self, project_name: str) -> list[str]:
        meta = self.load_project_metadata(project_name) or {}
        return meta.get("files", [])
