from plugins.base_plugin import BasePlugin

class UnrealPlugin(BasePlugin):
    """
    Unreal integration plugin for ARIA Lite.
    Matches the manifest:
      - name: Unreal
      - commands:
          unreal.test
    """

    name = "Unreal"

    def __init__(self, manifest, plugin_path):
        super().__init__(manifest, plugin_path)

    def get_menu_tabs(self):
        return self.manifest.get("menu_tabs", [])

    def get_commands(self):
        return {
            "unreal.test": self.run_test,
        }

    def run_test(self):
        return "Unreal plugin test executed."
