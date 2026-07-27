from backend.unity.unity_api import UnityAPI
from plugins.base_plugin import BasePlugin


class UnityToolsPlugin(BasePlugin):
    """
    Unity integration plugin for ARIA Lite.
    Matches the manifest:
      - name: Unity
      - commands:
          unity.open_project
          unity.build_project
    """

    name = "Unity"

    def __init__(self, manifest, plugin_path):
        super().__init__(manifest, plugin_path)
        self.unity = UnityAPI()

    # ---------------------------------------------------------
    # Menu Tabs (UI)
    # ---------------------------------------------------------
    def get_menu_tabs(self):
        return self.manifest.get("menu_tabs", [])

    # ---------------------------------------------------------
    # Command Registry
    # ---------------------------------------------------------
    def get_commands(self):
        return {
            "unity.open_project": self._cmd_open_project,
            "unity.build_project": self._cmd_build_project,
            "unity.list_projects": self._cmd_list_projects,
            "unity.scenes": self._cmd_scenes,
            "unity.version": self._cmd_version,
        }

    # ---------------------------------------------------------
    # Command Handlers (Unified)
    # ---------------------------------------------------------
    def _cmd_open_project(self):
        try:
            return self.unity.set_active_project("UnityProject")
        except Exception as e:
            return f"[Unity Error] {e}"

    def _cmd_build_project(self):
        try:
            return self.unity.build_project()
        except Exception as e:
            return f"[Unity Error] {e}"

    def _cmd_list_projects(self):
        try:
            return self.unity.list_projects()
        except Exception as e:
            return f"[Unity Error] {e}"

    def _cmd_scenes(self):
        try:
            return self.unity.scenes()
        except Exception as e:
            return f"[Unity Error] {e}"

    def _cmd_version(self):
        try:
            return self.unity.version()
        except Exception as e:
            return f"[Unity Error] {e}"
