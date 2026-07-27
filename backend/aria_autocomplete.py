class ARIAAutocomplete:
    """
    Unified autocomplete engine for ARIA packet commands.
    - No dependency on aria_window internals
    - Plugin commands collected via get_commands()
    - Core tasks + workflow keywords + plugin commands
    """

    def __init__(self, aria_window):
        self.aria_window = aria_window
        self.plugins = aria_window.plugins

        # Core ARIA task names
        self.task_commands = [
            "unity_scan_project",
            "unity_validate_script",
            "unreal_scan_project",
            "unreal_list_blueprints",
            "blender_list_scripts",
            "blender_generate_operator",
            "wordpress_generate_plugin",
            "read_file",
            "write_file",
            "list_directory",
            "search_project",
            "validate_script_generic",
            "summarize_text",
            "ask_aria",
        ]

        # Workflow keywords
        self.workflow_keywords = [
            "workflow",
            "steps",
            "task",
            "chat",
            "plugin",
            "system",
        ]

        # Plugin commands (Unified Plug‑N‑Play)
        self.plugin_commands = self._collect_plugin_commands()

    # ---------------------------------------------------------
    # PLUGIN COMMAND COLLECTION
    # ---------------------------------------------------------
    def _collect_plugin_commands(self):
        """
        Unified plugin command collector.
        Each plugin may expose get_commands() -> {command_id: handler}.
        """
        commands = []
        for plugin in self.plugins:
            if hasattr(plugin, "get_commands"):
                try:
                    for cid in plugin.get_commands().keys():
                        commands.append(cid)
                except Exception:
                    # Plugins must never break autocomplete
                    continue
        return commands

    # ---------------------------------------------------------
    # MAIN AUTOCOMPLETE LOGIC
    # ---------------------------------------------------------
    def suggest(self, text: str):
        """
        Return up to 20 suggestions based on prefix matching.
        Unified across:
        - core task commands
        - workflow keywords
        - plugin commands
        """
        text = text.lower().strip()
        suggestions = []

        # Core tasks
        for cmd in self.task_commands:
            if cmd.startswith(text):
                suggestions.append(cmd)

        # Workflow keywords
        for kw in self.workflow_keywords:
            if kw.startswith(text):
                suggestions.append(kw)

        # Plugin commands
        for pcmd in self.plugin_commands:
            if pcmd.lower().startswith(text):
                suggestions.append(pcmd)

        return suggestions[:20]
