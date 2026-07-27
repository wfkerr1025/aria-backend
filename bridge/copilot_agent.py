# backend/bridge/copilot_agent.py
from __future__ import annotations
from typing import Dict, Any
from .state_manager import bridge_state


class CopilotAgent:
    """
    Unified Copilot agent for ARIA Lite.
    Builds structured packets that Copilot consumes.
    """

    def __init__(self):
        self.interval = 0.1

    # ---------------------------------------------------------
    # BUILD PACKET
    # ---------------------------------------------------------
    def build_packet(self) -> Dict[str, Any]:
        """
        Builds the JSON packet Copilot consumes.
        Includes:
          - session_id
          - history
          - last_reply
          - last_error
          - metadata
        """

        session = bridge_state.get_session()

        if not session:
            return {
                "status": "error",
                "operation": "build_packet",
                "detail": "No active session"
            }

        packet = {
            "status": "ok",
            "operation": "build_packet",
            "session_id": session.session_id,
            "history": session.history,
            "last_reply": session.last_reply,
            "last_error": session.last_error,
            "metadata": session.metadata,
        }

        return packet


# Singleton
copilot_agent = CopilotAgent()
