from __future__ import annotations
from typing import Dict, Any, List


class Invariants:
    """
    Unified backend safety and consistency rules for ARIA Lite.
    Pure validation layer:
      - require_fields
      - enforce_task_whitelist
      - validate_operation
    Returns unified result dictionaries.
    """

    # ---------------------------------------------------------
    # REQUIRED FIELDS
    # ---------------------------------------------------------
    def require_fields(self, envelope: Dict[str, Any], fields: List[str]) -> Dict[str, Any]:
        """
        Ensures required fields exist and are not None or empty.
        Returns:
          {"status": "ok"}
          {"status": "error", "missing": [...], "message": "..."}
        """
        missing = [
            f for f in fields
            if f not in envelope or envelope[f] in (None, "")
        ]

        if missing:
            return {
                "status": "error",
                "message": "Missing required fields",
                "missing": missing
            }

        return {"status": "ok"}

    # ---------------------------------------------------------
    # TASK WHITELIST
    # ---------------------------------------------------------
    def enforce_task_whitelist(self, task: str, allowed: List[str]) -> Dict[str, Any]:
        """
        Ensures a task is allowed.
        Returns:
          {"status": "ok"}
          {"status": "error", "allowed": [...], "message": "..."}
        """
        if task not in allowed:
            return {
                "status": "error",
                "message": f"Task '{task}' not allowed",
                "allowed": allowed
            }

        return {"status": "ok"}

    # ---------------------------------------------------------
    # OPERATION VALIDATION
    # ---------------------------------------------------------
    def validate_operation(self, op: str, allowed: List[str]) -> Dict[str, Any]:
        """
        Ensures an operation is allowed.
        Returns:
          {"status": "ok"}
          {"status": "error", "allowed": [...], "message": "..."}
        """
        if op not in allowed:
            return {
                "status": "error",
                "message": f"Operation '{op}' not allowed",
                "allowed": allowed
            }

        return {"status": "ok"}
