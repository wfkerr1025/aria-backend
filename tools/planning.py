# backend/tools/planning.py
from __future__ import annotations
import time
from typing import Dict, Any

from backend.llm.auto_planner import auto_planner

from logger import get_logger

logger = get_logger(__name__)


def run_planning(session_id: str = "default") -> Dict[str, Any]:
    """
    Tool wrapper for the Auto‑Planner.
    """
    start = time.monotonic()
    logger.debug("run_planning invoked: session_id=%s", session_id)

    try:
        plan = auto_planner.build_plan(session_id)
    except Exception:
        logger.exception("run_planning failed: session_id=%s", session_id)
        raise

    elapsed_ms = (time.monotonic() - start) * 1000
    logger.info("run_planning completed: session_id=%s elapsed_ms=%.2f", session_id, elapsed_ms)

    return {
        "type": "assistant",
        "content": (
            f"Goal: {plan['goal']}\n\n"
            f"Steps:\n" +
            "\n".join(f"- {step}" for step in plan["steps"])
        )
    }
