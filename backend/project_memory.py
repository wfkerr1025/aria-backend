from __future__ import annotations
from typing import Dict, Any, List, Optional
import json
import os


from logger import get_logger

logger = get_logger(__name__)

class ProjectMemory:
    """
    Unified long-term project memory for ARIA Lite.
    Stores per-project facts:
      - topics
      - intents
      - notes
      - goals
    All operations return unified result dictionaries.
    """

    def __init__(self, base_path: str = "aria_project_memory.json"):
        logger.debug(f"Initializing ProjectMemory → {base_path}")
        self.base_path = base_path
        self.data: Dict[str, Any] = {}
        self._load()

    # ---------------------------------------------------------
    # INTERNAL LOAD
    # ---------------------------------------------------------
    def _load(self) -> None:
        logger.debug(f"Loading memory file → {self.base_path}")

        if os.path.exists(self.base_path):
            try:
                with open(self.base_path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
                logger.debug("Memory file loaded successfully")
            except Exception as e:
                logger.error(f"Memory load ERROR → {e}")
                self.data = {}
        else:
            logger.debug("Memory file not found → starting empty")
            self.data = {}

    # ---------------------------------------------------------
    # INTERNAL SAVE
    # ---------------------------------------------------------
    def _save(self) -> Dict[str, Any]:
        logger.debug(f"Saving memory → {self.base_path}")

        try:
            with open(self.base_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)

            logger.debug("Memory saved successfully")
            return {"status": "ok", "operation": "save_memory", "path": self.base_path}

        except Exception as e:
            logger.error(f"Memory save ERROR → {e}")
            return {
                "status": "error",
                "operation": "save_memory",
                "path": self.base_path,
                "detail": str(e)
            }

    # ---------------------------------------------------------
    # ENSURE PROJECT
    # ---------------------------------------------------------
    def _ensure_project(self, project_name: str) -> None:
        logger.debug(f"Ensuring project exists → {project_name}")

        if project_name not in self.data:
            self.data[project_name] = {
                "topics": [],
                "intents": [],
                "notes": [],
                "goals": []
            }
            logger.debug(f"Created new project entry → {project_name}")

    # ---------------------------------------------------------
    # REMEMBER TOPIC
    # ---------------------------------------------------------
    def remember_topic(self, project_name: str, topic: str) -> Dict[str, Any]:
        logger.debug(f"remember_topic() → {project_name}: {topic}")

        self._ensure_project(project_name)

        if topic not in self.data[project_name]["topics"]:
            self.data[project_name]["topics"].append(topic)
            save_result = self._save()

            return {
                "status": "ok",
                "operation": "remember_topic",
                "project": project_name,
                "topic": topic,
                "save": save_result
            }

        logger.debug(f"Topic already stored → {topic}")
        return {
            "status": "ok",
            "operation": "remember_topic",
            "project": project_name,
            "topic": topic,
            "detail": "Topic already stored"
        }

    # ---------------------------------------------------------
    # REMEMBER INTENT
    # ---------------------------------------------------------
    def remember_intent(self, project_name: str, intent: str) -> Dict[str, Any]:
        logger.debug(f"remember_intent() → {project_name}: {intent}")

        self._ensure_project(project_name)
        self.data[project_name]["intents"].append(intent)
        save_result = self._save()

        return {
            "status": "ok",
            "operation": "remember_intent",
            "project": project_name,
            "intent": intent,
            "save": save_result
        }

    # ---------------------------------------------------------
    # REMEMBER NOTE
    # ---------------------------------------------------------
    def remember_note(self, project_name: str, note: str) -> Dict[str, Any]:
        logger.debug(f"remember_note() → {project_name}: {note}")

        self._ensure_project(project_name)
        self.data[project_name]["notes"].append(note)
        save_result = self._save()

        return {
            "status": "ok",
            "operation": "remember_note",
            "project": project_name,
            "note": note,
            "save": save_result
        }

    # ---------------------------------------------------------
    # REMEMBER GOAL
    # ---------------------------------------------------------
    def remember_goal(self, project_name: str, goal: str) -> Dict[str, Any]:
        logger.debug(f"remember_goal() → {project_name}: {goal}")

        self._ensure_project(project_name)
        self.data[project_name]["goals"].append(goal)
        save_result = self._save()

        return {
            "status": "ok",
            "operation": "remember_goal",
            "project": project_name,
            "goal": goal,
            "save": save_result
        }

    # ---------------------------------------------------------
    # GET PROJECT SNAPSHOT
    # ---------------------------------------------------------
    def get_project(self, project_name: str) -> Dict[str, Any]:
        logger.debug(f"get_project() → {project_name}")

        project = self.data.get(project_name)

        if not project:
            logger.debug(f"Project not found → {project_name}")
            return {
                "status": "error",
                "operation": "get_project",
                "project": project_name,
                "detail": "Project not found"
            }

        logger.debug(f"Project snapshot returned → {project_name}")
        return {
            "status": "ok",
            "operation": "get_project",
            "project": project_name,
            "data": project
        }
