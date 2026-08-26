


from logger import get_logger

logger = get_logger(__name__)

class ContextAwarePluginActivator:
    """
    Unified plugin activator driven by context snapshots.
    - Reads entities from ContextEngine snapshots
    - Resolves topics → plugin names via PluginTopicMap
    - Activates plugin tabs safely through ARIAWindow
    """

    def __init__(self, aria_window):
        logger.debug("Initializing ContextAwarePluginActivator")
        self.aria_window = aria_window

    # ---------------------------------------------------------
    # MAIN ACTIVATION LOGIC
    # ---------------------------------------------------------
    def activate_from_context(self, context_snapshot):
        logger.debug(f"activate_from_context() → {context_snapshot}")

        context = context_snapshot.get("context")
        if not context:
            logger.debug("No context found → skipping activation")
            return None

        entities = context.get("entities", {})
        if not entities:
            logger.debug("No entities found → skipping activation")
            return None

        from backend.plugin_topic_map import PluginTopicMap

        for ent_id, ent_data in entities.items():
            logger.debug(f"Scanning entity → {ent_id}: {ent_data}")

            topic = self._extract_topic(ent_data)
            if not topic:
                continue

            logger.debug(f"Resolved topic candidate → {topic}")

            plugin_name = PluginTopicMap.resolve(topic)
            if plugin_name:
                logger.debug(f"Topic matched plugin → {plugin_name}")
                return self._activate_plugin(plugin_name)

        logger.debug("No plugin matched from context")
        return None

    # ---------------------------------------------------------
    # TOPIC EXTRACTION
    # ---------------------------------------------------------
    def _extract_topic(self, ent_data):
        logger.debug(f"_extract_topic() → {ent_data}")

        if ent_data.get("type") == "topic":
            return ent_data.get("topic")

        if ent_data.get("type") == "ARIAKeyword":
            return ent_data.get("value")

        if ent_data.get("type") == "Name":
            return ent_data.get("value")

        return None

    # ---------------------------------------------------------
    # PLUGIN ACTIVATION
    # ---------------------------------------------------------
    def _activate_plugin(self, plugin_name):
        logger.debug(f"_activate_plugin() → {plugin_name}")

        for plugin in self.aria_window.plugins:
            name = getattr(plugin, "name", plugin.__class__.__name__)
            logger.debug(f"Checking plugin → {name}")

            if name.lower() == plugin_name.lower():
                logger.debug(f"Activating plugin tab → {plugin_name}")
                self.aria_window.menubar.activate_plugin_tab(plugin)
                return plugin_name

        logger.debug(f"Plugin not found → {plugin_name}")
        return None
