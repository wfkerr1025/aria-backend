from plugins.base_plugin import BasePlugin


class BlenderPlugin(BasePlugin):
    """
    Unified Blender plugin for ARIA Lite.
    Provides:
      - menu tabs
      - command registry
      - test command
    """

    name = "Blender"

    def __init__(self, manifest, plugin_path):
        super().__init__(manifest, plugin_path)

    # ---------------------------------------------------------
    # Menu Tabs (UI)
    # ---------------------------------------------------------
    def get_menu_tabs(self):
        """
        Returns menu tab definitions from manifest.
        ARIA Lite uses this to build plugin UI.
        """
        return self.manifest.get("menu_tabs", [])

    # ---------------------------------------------------------
    # Command Registry
    # ---------------------------------------------------------
    def get_commands(self):
        """
        Returns a mapping of command IDs → handler functions.
        """
        return {
            "blender.test": self.run_test,
        }

    # ---------------------------------------------------------
    # Command Handlers
    # ---------------------------------------------------------
    def run_test(self):
        """
        Simple test command.
        """
        return "Blender plugin test executed."
