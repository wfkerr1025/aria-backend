from plugins.base_plugin import BasePlugin

class WordPressPlugin(BasePlugin):
    """
    WordPress integration plugin for ARIA Lite.
    Matches the manifest:
      - name: WordPress
      - commands:
          wordpress.test
    """

    name = "WordPress"

    def __init__(self, manifest, plugin_path):
        super().__init__(manifest, plugin_path)

    def get_menu_tabs(self):
        return self.manifest.get("menu_tabs", [])

    def get_commands(self):
        return {
            "wordpress.test": self.run_test,
        }

    def run_test(self):
        return "WordPress plugin test executed."
