class ContextAwarePluginActivator:
    """
    Unified plugin activator driven by context snapshots.
    - Reads entities from ContextEngine snapshots
    - Resolves topics → plugin names via PluginTopicMap
    - Activates plugin tabs safely through ARIAWindow
    """

    def __init__(self, aria_window):
        self.aria_window = aria_window

    # ---------------------------------------------------------
    # MAIN ACTIVATION LOGIC
    # ---------------------------------------------------------
    def activate_from_context(self, context_snapshot):
        """
        Given a snapshot from ContextEngine.get_snapshot(),
        attempt to activate a plugin based on detected topics.
        """

        # ContextEngine snapshot structure:
        # {
        #   "context": {
        #       "entities": { entity_id: { ... } },
        #       ...
        #   },
        #   "turn": <int>
        # }

        context = context_snapshot.get("context")
        if not context:
            return None

        entities = context.get("entities", {})
        if not entities:
            return None

        # Lazy import to avoid circular dependency
        from backend.plugin_topic_map import PluginTopicMap

        # Scan entities for topic-like attributes
        for ent_id, ent_data in entities.items():
            topic = self._extract_topic(ent_data)
            if not topic:
                continue

            plugin_name = PluginTopicMap.resolve(topic)
            if plugin_name:
                return self._activate_plugin(plugin_name)

        return None

    # ---------------------------------------------------------
    # TOPIC EXTRACTION
    # ---------------------------------------------------------
    def _extract_topic(self, ent_data):
        """
        Unified topic extraction.
        Supports:
        - entities with {"type": "topic", "topic": "..."}
        - entities with {"type": "ARIAKeyword", "value": "..."} if mapped
        - entities with {"type": "Name", "value": "..."} if mapped
        """

        # Explicit topic entity
        if ent_data.get("type") == "topic":
            return ent_data.get("topic")

        # ARIAKeyword → topic mapping
        if ent_data.get("type") == "ARIAKeyword":
            return ent_data.get("value")

        # Named entities (e.g., "Unity", "Blender")
        if ent_data.get("type") == "Name":
            return ent_data.get("value")

        return None

    # ---------------------------------------------------------
    # PLUGIN ACTIVATION
    # ---------------------------------------------------------
    def _activate_plugin(self, plugin_name):
        """
        Activates the plugin tab in the MenuBar + Sidebar.
        Unified with ARIAWindow plugin system.
        """

        for plugin in self.aria_window.plugins:
            name = getattr(plugin, "name", plugin.__class__.__name__)
            if name.lower() == plugin_name.lower():
                self.aria_window.menubar.activate_plugin_tab(plugin)
                return plugin_name

        return None
