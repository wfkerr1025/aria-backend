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
    # Capability Hooks (Phase 2 — backend.core.plugin_registry)
    #
    # Optional, additive, safe-default overrides — a plugin that doesn't
    # implement any of these (every plugin shipped today: blender, unity,
    # unreal, wordpress) behaves exactly as before. A plugin that DOES
    # override one of these gets picked up automatically by
    # backend.core.plugin_registry's aggregation without any change to
    # PluginManager itself.
    # ---------------------------------------------------------
    def get_tools(self):
        """
        Return a list of (ToolSchema, handler) tuples this plugin wants
        registered into backend.core.tool_registry. See that module for
        ToolSchema's shape. Default: no tools.
        """
        return []

    def get_models(self):
        """
        Return a list of model_cfg dicts (same shape as
        backend.core.model_registry entries) this plugin wants to make
        available. Default: no models.
        """
        return []

    def get_diagnostics(self):
        """
        Return a dict of plugin-specific diagnostic info, surfaced via
        backend.core.plugin_registry's aggregation (and, from there,
        /v1/diagnostics/plugins). Default: empty.
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
