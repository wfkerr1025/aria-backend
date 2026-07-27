class BasePlugin:
    """
    Base class for all ARIA Lite plugins.
    Plugins must inherit from this class and implement required methods.

    Provides:
      - manifest metadata access
      - menu tab definitions
      - command registry
      - lifecycle hooks
    """

    def __init__(self, manifest, plugin_path):
        self.manifest = manifest
        self.plugin_path = plugin_path

    # ---------------------------------------------------------
    # Metadata
    # ---------------------------------------------------------
    def get_name(self):
        return self.manifest.get("name", "Unnamed Plugin")

    def get_version(self):
        return self.manifest.get("version", "0.0.0")

    def get_author(self):
        return self.manifest.get("author", "Unknown")

    def get_description(self):
        return self.manifest.get("description", "")

    # ---------------------------------------------------------
    # UI Hooks
    # ---------------------------------------------------------
    def get_menu_tabs(self):
        """
        Return a list of menu tab definitions from manifest.
        Example:
        [
            {
                "label": "Unity Tools",
                "commands": [
                    {"name": "Build Project", "id": "unity.build_project"}
                ]
            }
        ]
        """
        return self.manifest.get("menu_tabs", [])

    # ---------------------------------------------------------
    # Command Hooks
    # ---------------------------------------------------------
    def get_commands(self):
        """
        Return a dict of command_name -> function.
        Example:
        {
            "unity.build_project": self.build_project
        }
        """
        return {}

    # ---------------------------------------------------------
    # Lifecycle Hooks
    # ---------------------------------------------------------
    def on_load(self):
        """Called when plugin is loaded."""
        pass

    def on_unload(self):
        """Called when plugin is unloaded."""
        pass
