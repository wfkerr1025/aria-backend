import re
from backend.context_engine import Entity


class EntityExtractor:
    """
    Unified lightweight entity extractor for ARIA Lite.
    Produces Entity objects consistent with ContextEngine’s expectations.
    """

    def extract(self, text: str):
        entities = []

        # ---------------------------------------------------------
        # PEOPLE (capitalized words)
        # ---------------------------------------------------------
        people = re.findall(r"\b([A-Z][a-z]+)\b", text)
        for p in people:
            entities.append(
                Entity(
                    entity_id=f"person_{p.lower()}",
                    attributes={
                        "type": "Person",
                        "value": p,
                        "source": "surface"
                    }
                )
            )

        # ---------------------------------------------------------
        # LOCATIONS (simple heuristic)
        # ---------------------------------------------------------
        location_keywords = [
            "Paris", "London", "New York", "Tokyo",
            "Virginia", "Orange"
        ]

        lower_text = text.lower()
        for loc in location_keywords:
            if loc.lower() in lower_text:
                entities.append(
                    Entity(
                        entity_id=f"location_{loc.lower()}",
                        attributes={
                            "type": "Location",
                            "value": loc,
                            "source": "semantic"
                        }
                    )
                )

        # ---------------------------------------------------------
        # DATES
        # ---------------------------------------------------------
        date_patterns = [
            r"\bnext week\b",
            r"\btomorrow\b",
            r"\btonight\b",
            r"\bnext month\b",
            r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
            r"\bJuly \d{1,2}\b",
        ]

        for pattern in date_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                date_str = match.group(0)
                entities.append(
                    Entity(
                        entity_id=f"date_{date_str.replace(' ', '_').lower()}",
                        attributes={
                            "type": "Date",
                            "value": date_str,
                            "source": "surface"
                        }
                    )
                )

        # ---------------------------------------------------------
        # TASKS (verbs + objects)
        # ---------------------------------------------------------
        task_match = re.search(
            r"\b(?:fix|build|create|make|install|debug|write|design)\b.*",
            text,
            re.IGNORECASE
        )

        if task_match:
            task = task_match.group(0)
            entities.append(
                Entity(
                    entity_id=f"task_{abs(hash(task))}",
                    attributes={
                        "type": "Task",
                        "description": task,
                        "source": "semantic"
                    }
                )
            )

        return entities
