from backend.context_engine import Entity


class SuggestionEngine:
    """
    Unified suggestion engine for ARIA Lite.
    Generates context-aware suggestions based on:
      - detected intent
      - detected topics / keywords
    Returns Entity objects consistent with ContextEngine.
    """

    def generate(self, context_snapshot):
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

        intent = None
        topics = []

        # ---------------------------------------------------------
        # Extract intent + topics from unified entity schema
        # ---------------------------------------------------------
        for ent_id, ent_data in entities.items():

            # Intent entity
            if ent_data.get("type") == "Intent":
                intent = ent_data.get("value")

            # Topic entity (explicit)
            if ent_data.get("type") == "topic":
                topics.append(ent_data.get("topic"))

            # ARIAKeyword → treat as topic
            if ent_data.get("type") == "ARIAKeyword":
                topics.append(ent_data.get("value").lower())

            # Named entities (Unity, Blender, ARIA, etc.)
            if ent_data.get("type") == "Name":
                topics.append(ent_data.get("value").lower())

        # Normalize topics
        topics = [t.lower() for t in topics]

        # ---------------------------------------------------------
        # Intent-based suggestions
        # ---------------------------------------------------------
        if intent == "debug":
            return Entity(
                entity_id="suggestion_debug_tools",
                attributes={
                    "type": "Suggestion",
                    "value": "It looks like you're debugging — want me to open the Task Console?"
                }
            )

        if intent == "planning":
            return Entity(
                entity_id="suggestion_planning_tools",
                attributes={
                    "type": "Suggestion",
                    "value": "Planning something? I can generate a roadmap or create a task packet."
                }
            )

        if intent == "plugin_work":
            return Entity(
                entity_id="suggestion_plugin_tools",
                attributes={
                    "type": "Suggestion",
                    "value": "Working with plugins — want me to switch to the relevant plugin tab?"
                }
            )

        # ---------------------------------------------------------
        # Topic-based suggestions
        # ---------------------------------------------------------
        if "unity" in topics:
            return Entity(
                entity_id="suggestion_unity_tools",
                attributes={
                    "type": "Suggestion",
                    "value": "Unity topic detected — want me to activate the Unity plugin?"
                }
            )

        if "blender" in topics:
            return Entity(
                entity_id="suggestion_blender_tools",
                attributes={
                    "type": "Suggestion",
                    "value": "Blender topic detected — want me to activate the Blender plugin?"
                }
            )

        if "aria" in topics:
            return Entity(
                entity_id="suggestion_aria_tools",
                attributes={
                    "type": "Suggestion",
                    "value": "ARIA system topic detected — want me to open the Packet Inspector?"
                }
            )

        return None
