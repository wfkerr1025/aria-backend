from __future__ import annotations
from typing import Dict, Any

from bridge.state_manager import bridge_state


class SystemEngine:
    """
    Version 3 — System Engine for ARIA Lite

    Handles:
      - set_model_pref:cloud
      - set_model_pref:local
      - set_model_pref:auto

    This is the backend subsystem ARIA expects when she emits:
        task: "system"
        query: "set_model_pref:<value>"
    """

    def dispatch(self, packet: Dict[str, Any]) -> Dict[str, Any]:
        """
        Dispatch system-level operations.
        """

        query = packet.get("query", "")
        session_id = packet.get("session_id", "default")

        # Expected format: "set_model_pref:<value>"
        if query.startswith("set_model_pref:"):
            value = query.split("set_model_pref:", 1)[1].strip()

            if value not in {"cloud", "local", "auto"}:
                return {
                    "status": "error",
                    "operation": "system",
                    "detail": f"Invalid model preference '{value}'"
                }

            # Update state
            bridge_state.set_model_pref(session_id, value)

            return {
                "status": "ok",
                "operation": "set_model_pref",
                "session_id": session_id,
                "mode": value
            }

        # Unknown system operation
        return {
            "status": "error",
            "operation": "system",
            "detail": f"Unknown system query '{query}'"
        }


system_engine = SystemEngine()
