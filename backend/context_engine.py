from __future__ import annotations
from typing import Dict, Any, List, Optional
import time
import re


# ============================================================
# ENTITY MODEL
# ============================================================
class Entity:
    """
    Unified entity model.
    Each entity has:
      - entity_id (unique key)
      - attributes (dict of semantic/surface properties)
    """

    def __init__(self, entity_id: str, attributes: Dict[str, Any]):
        self.entity_id = entity_id
        self.attributes = attributes

    def update_attributes(self, new_attributes: Dict[str, Any]):
        self.attributes.update(new_attributes)


# ============================================================
# CONTEXT MODEL
# ============================================================
class Context:
    """
    Unified context model.
    Tracks:
      - entities
      - timestamps
      - context_id
    """

    def __init__(self, context_id: str, entities: List[Entity] | None = None):
        self.context_id = context_id
        self.entities: Dict[str, Entity] = {}
        self.created_at = time.time()
        self.last_updated = self.created_at

        if entities:
            for e in entities:
                self.entities[e.entity_id] = e

    def update_entity(self, entity: Entity):
        if entity.entity_id in self.entities:
            self.entities[entity.entity_id].update_attributes(entity.attributes)
        else:
            self.entities[entity.entity_id] = entity

        self.last_updated = time.time()

    def remove_entity(self, entity_id: str):
        if entity_id in self.entities:
            del self.entities[entity_id]
            self.last_updated = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "context_id": self.context_id,
            "created_at": self.created_at,
            "last_updated": self.last_updated,
            "entities": {
                eid: e.attributes for eid, e in self.entities.items()
            }
        }


# ============================================================
# CONTEXT MANAGER
# ============================================================
class ContextManager:
    """
    Unified context manager.
    Handles:
      - active context
      - multiple contexts
      - turn counter
    """

    def __init__(self):
        self.active_context: Optional[Context] = None
        self.contexts: Dict[str, Context] = {}
        self.turn_number: int = 0

    def create_context(self, context_id: str, entities: List[Entity] | None = None):
        if context_id not in self.contexts:
            new_context = Context(context_id, entities or [])
            self.contexts[context_id] = new_context
            self.set_active_context(new_context)
        else:
            self.set_active_context(self.contexts[context_id])

    def set_active_context(self, context: Context):
        self.active_context = context

    def get_active_context(self) -> Optional[Context]:
        return self.active_context

    def update_entity_in_active_context(self, entity: Entity):
        if self.active_context:
            self.active_context.update_entity(entity)

    def remove_entity_from_active_context(self, entity_id: str):
        if self.active_context:
            self.active_context.remove_entity(entity_id)

    def start_turn(self):
        self.turn_number += 1
        return self.turn_number

    def get_context_snapshot(self) -> Dict[str, Any]:
        if not self.active_context:
            return {"context": None, "turn": self.turn_number}

        return {
            "context": self.active_context.to_dict(),
            "turn": self.turn_number
        }


# ============================================================
# CONTEXT ENGINE (UNIFIED)
# ============================================================
class ContextEngine:
    """
    Unified high-level context engine for ARIA Lite.
    Provides:
      - extract_entities(text)
      - detect_intent(text)
      - suggest_topics(text)
      - get_snapshot()
    """

    def __init__(self):
        self.manager = ContextManager()
        self.manager.create_context("default")

    # ---------------------------------------------------------
    # 1. Surface Extraction
    # ---------------------------------------------------------
    def _extract_surface_entities(self, text: str) -> List[Dict[str, Any]]:
        entities: List[Dict[str, Any]] = []

        # Capitalized words (names, objects, projects)
        for match in re.findall(r"\b[A-Z][a-zA-Z0-9_]+\b", text):
            entities.append({"type": "Name", "value": match})

        # Quoted strings
        for match in re.findall(r'"([^"]+)"', text):
            entities.append({"type": "QuotedString", "value": match})

        # File paths (Windows-style)
        for match in re.findall(r"[A-Za-z]:[\\/][^\s]+", text):
            entities.append({"type": "FilePath", "value": match})

        # Numbers
        for match in re.findall(r"\b\d+\b", text):
            entities.append({"type": "Number", "value": match})

        return entities

    # ---------------------------------------------------------
    # 2. Semantic Extraction
    # ---------------------------------------------------------
    def _extract_semantic_entities(self, text: str) -> List[Dict[str, Any]]:
        entities: List[Dict[str, Any]] = []
        lower = text.lower()

        # Simple action verbs
        actions = ["create", "delete", "open", "write", "patch", "update"]
        for action in actions:
            if action in lower:
                entities.append({"type": "Action", "value": action})

        # ARIA-specific keywords
        aria_keywords = ["plugin", "context", "task", "packet", "window", "backend"]
        for keyword in aria_keywords:
            if keyword in lower:
                entities.append({"type": "ARIAKeyword", "value": keyword})

        return entities

    # ---------------------------------------------------------
    # 3. Contextual Integration
    # ---------------------------------------------------------
    def extract_entities(self, text: str) -> List[Dict[str, Any]]:
        surface = self._extract_surface_entities(text)
        semantic = self._extract_semantic_entities(text)
        combined = surface + semantic

        # Convert extracted entities into Entity objects
        for ent in combined:
            entity_id = f"{ent['type']}_{ent['value']}"
            entity_obj = Entity(entity_id, ent)
            self.manager.update_entity_in_active_context(entity_obj)

        return combined

    # ---------------------------------------------------------
    # Intent Detection
    # ---------------------------------------------------------
    def detect_intent(self, text: str) -> Dict[str, Any]:
        lower = text.lower()

        if "create" in lower:
            return {"intent": "create"}
        if "delete" in lower:
            return {"intent": "delete"}
        if "open" in lower:
            return {"intent": "open"}
        if "write" in lower:
            return {"intent": "write"}
        if "patch" in lower or "update" in lower:
            return {"intent": "modify"}

        return {"intent": "unknown"}

    # ---------------------------------------------------------
    # Topic Suggestion
    # ---------------------------------------------------------
    def suggest_topics(self, text: str) -> List[str]:
        lower = text.lower()
        topics: List[str] = []

        if "file" in lower or "path" in lower:
            topics.append("filesystem")
        if "context" in lower:
            topics.append("context-management")
        if "plugin" in lower:
            topics.append("plugin-system")
        if "backend" in lower or "server" in lower:
            topics.append("backend-architecture")

        return topics

    # ---------------------------------------------------------
    # Context Snapshot Helper
    # ---------------------------------------------------------
    def get_snapshot(self) -> Dict[str, Any]:
        return self.manager.get_context_snapshot()
