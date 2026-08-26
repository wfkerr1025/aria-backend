from __future__ import annotations
from typing import Dict, Any, List


from logger import get_logger

logger = get_logger(__name__)

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
        logger.debug(f"require_fields() → envelope={envelope}, fields={fields}")

        missing = [
            f for f in fields
            if f not in envelope or envelope[f] in (None, "")
        ]

        if missing:
            logger.debug(f"Missing required fields → {missing}")
            return {
                "status": "error",
                "message": "Missing required fields",
                "missing": missing
            }

        logger.debug("require_fields() OK")
        return {"status": "ok"}

    # ---------------------------------------------------------
    # TASK WHITELIST
    # ---------------------------------------------------------
    def enforce_task_whitelist(self, task: str, allowed: List[str]) -> Dict[str, Any]:
        logger.debug(f"enforce_task_whitelist() → task={task}, allowed={allowed}")

        if task not in allowed:
            logger.debug(f"Task not allowed → {task}")
            return {
                "status": "error",
                "message": f"Task '{task}' not allowed",
                "allowed": allowed
            }

        logger.debug("enforce_task_whitelist() OK")
        return {"status": "ok"}

    # ---------------------------------------------------------
    # OPERATION VALIDATION
    # ---------------------------------------------------------
    def validate_operation(self, op: str, allowed: List[str]) -> Dict[str, Any]:
        logger.debug(f"validate_operation() → op={op}, allowed={allowed}")

        if op not in allowed:
            logger.debug(f"Operation not allowed → {op}")
            return {
                "status": "error",
                "message": f"Operation '{op}' not allowed",
                "allowed": allowed
            }

        logger.debug("validate_operation() OK")
        return {"status": "ok"}
