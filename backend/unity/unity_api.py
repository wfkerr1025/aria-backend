import os
from backend.unity.unity_scanner import (
    find_unity_projects,
    read_unity_version,
    find_unity_scenes,
)
from backend.unity.unity_generator import (
    create_script,
    create_scene,
)
from backend.unity.unity_build import read_build_settings


class UnityAPI:
    """
    Unified Unity API wrapper.
    Provides:
      - project selection
      - version lookup
      - scene enumeration
      - build settings
      - script + scene creation
    """

    def __init__(self):
        self.active_project = None

    # ---------------------------------------------------------
    # PROJECT SELECTION
    # ---------------------------------------------------------
    def set_active_project(self, path: str):
        if os.path.exists(path):
            self.active_project = path
            return f"Active Unity project set: {path}"
        return "Project does not exist."

    def list_projects(self):
        return find_unity_projects()

    # ---------------------------------------------------------
    # PROJECT INFO
    # ---------------------------------------------------------
    def version(self):
        if not self.active_project:
            return "No active project."
        return read_unity_version(self.active_project)

    def scenes(self):
        if not self.active_project:
            return "No active project."
        return find_unity_scenes(self.active_project)

    def build_settings(self):
        if not self.active_project:
            return "No active project."
        return read_build_settings(self.active_project)

    # ---------------------------------------------------------
    # CREATION OPERATIONS
    # ---------------------------------------------------------
    def create_script(self, name):
        if not self.active_project:
            return "No active project."
        return create_script(self.active_project, name)

    def create_scene(self, name):
        if not self.active_project:
            return "No active project."
        return create_scene(self.active_project, name)


# ============================================================
# STEP 3 — IPC ENTRY POINT
# ============================================================
def run(command: str, **kwargs):
    """
    Unified IPC entry point for the Unity module.

    Supported commands:
      - "list_projects"
      - "set_project"
      - "version"
      - "scenes"
      - "build_settings"
      - "create_script"
      - "create_scene"
    """

    try:
        # Lazy import to avoid circular dependency
        from backend.app import app_instance
        unity = app_instance.unity

        if command == "list_projects":
            return {
                "status": "ok",
                "operation": "unity.list_projects",
                "projects": unity.list_projects()
            }

        if command == "set_project":
            path = kwargs.get("path")
            result = unity.set_active_project(path)
            return {
                "status": "ok",
                "operation": "unity.set_project",
                "result": result,
                "active_project": unity.active_project
            }

        if command == "version":
            return {
                "status": "ok",
                "operation": "unity.version",
                "version": unity.version()
            }

        if command == "scenes":
            return {
                "status": "ok",
                "operation": "unity.scenes",
                "scenes": unity.scenes()
            }

        if command == "build_settings":
            return {
                "status": "ok",
                "operation": "unity.build_settings",
                "settings": unity.build_settings()
            }

        if command == "create_script":
            name = kwargs.get("name")
            result = unity.create_script(name)
            return {
                "status": "ok",
                "operation": "unity.create_script",
                "name": name,
                "result": result
            }

        if command == "create_scene":
            name = kwargs.get("name")
            result = unity.create_scene(name)
            return {
                "status": "ok",
                "operation": "unity.create_scene",
                "name": name,
                "result": result
            }

        return {
            "status": "error",
            "operation": "unity",
            "detail": f"Unknown command '{command}'"
        }

    except Exception as e:
        return {
            "status": "error",
            "operation": "unity",
            "detail": str(e)
        }
