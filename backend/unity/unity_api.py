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
