from __future__ import annotations
from typing import Dict, Any, List
import json
import os


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
        self.base_path = base_path
        self.data: Dict[str, Any] = {}
        self._load()

    # ---------------------------------------------------------
    # INTERNAL LOAD
    # ---------------------------------------------------------
    def _load(self) -> None:
        if os.path.exists(self.base_path):
            try:
                with open(self.base_path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}
        else:
            self.data = {}

    # ---------------------------------------------------------
    # INTERNAL SAVE
    # ---------------------------------------------------------
    def _save(self) -> Dict[str, Any]:
        try:
            with open(self.base_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            return {"status": "ok", "operation": "save_memory", "path": self.base_path}
        except Exception as e:
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
        if project_name not in self.data:
            self.data[project_name] = {
                "topics": [],
                "intents": [],
                "notes": [],
                "goals": []
            }

    # ---------------------------------------------------------
    # REMEMBER TOPIC
    # ---------------------------------------------------------
    def remember_topic(self, project_name: str, topic: str) -> Dict[str, Any]:
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
        project = self.data.get(project_name)

        if not project:
            return {
                "status": "error",
                "operation": "get_project",
                "project": project_name,
                "detail": "Project not found"
            }

        return {
            "status": "ok",
            "operation": "get_project",
            "project": project_name,
            "data": project
        }
