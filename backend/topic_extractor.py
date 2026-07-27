from backend.context_engine import Entity


class TopicExtractor:
    """
    Unified lightweight topic extractor for ARIA Lite.
    Uses keyword heuristics to identify conversation topics.
    Produces Entity objects consistent with ContextEngine.
    """

    TOPIC_KEYWORDS = {
        "unity": ["unity", "game engine", "csharp", "editor"],
        "unreal": ["unreal", "ue5", "blueprints", "epic"],
        "blender": ["blender", "3d modeling", "mesh", "render"],
        "aria": ["aria", "context engine", "packet", "task console"],
        "travel": ["flight", "hotel", "paris", "trip", "vacation"],
        "coding": ["python", "javascript", "code", "debug", "fix"],
        "plugins": ["plugin", "extension", "module"],
    }

    def extract(self, text: str):
        lower = text.lower()
        detected_topics = []

        # ---------------------------------------------------------
        # Keyword detection
        # ---------------------------------------------------------
        for topic, keywords in self.TOPIC_KEYWORDS.items():
            if any(k in lower for k in keywords):
                detected_topics.append(topic)

        # ---------------------------------------------------------
        # Convert topics → unified Entity objects
        # ---------------------------------------------------------
        entities = []
        for topic in detected_topics:
            entities.append(
                Entity(
                    entity_id=f"topic_{topic}",
                    attributes={
                        "type": "Topic",
                        "value": topic,
                        "source": "keyword"
                    }
                )
            )

        return entities
