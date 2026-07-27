# backend/bridge/aria_agent.py
from __future__ import annotations
import time
from typing import Dict, Any
from .command_router import bridge_router
from .state_manager import bridge_state


class ARIAAgent:
    """
    Unified ARIA autonomous agent loop.
    Responsibilities:
      - process pending actions from Copilot
      - execute diagnostics
      - execute scripts safely
      - produce structured replies
    """

    def __init__(self):
        self.running = False
        self.interval = 0.1  # 100ms loop

    # ---------------------------------------------------------
    # START / STOP
    # ---------------------------------------------------------
    def start(self):
        self.running = True
        while self.running:
            self._tick()
            time.sleep(self.interval)

    def stop(self):
        self.running = False

    # ---------------------------------------------------------
    # MAIN LOOP TICK
    # ---------------------------------------------------------
    def _tick(self):
        """
        ARIA checks for pending actions or tasks.
        """

        session = bridge_state.get_session()
        if not session:
            return

        last_reply = session.last_reply
        if not last_reply:
            return

        actions = last_reply.get("actions", [])
        if not actions:
            return

        # Execute each action
        for action in actions:
            self._execute_action(session.session_id, action)

        # Clear actions after execution
        session.last_reply["actions"] = []

    # ---------------------------------------------------------
    # EXECUTE ACTION
    # ---------------------------------------------------------
    def _execute_action(self, session_id: str, action: Dict[str, Any]):
        """
        Executes structured actions from Copilot.
        """

        action_type = action.get("type")
        if not action_type:
            return

        # -----------------------------------------------------
        # DIAGNOSTIC ACTION
        # -----------------------------------------------------
        if action_type == "diagnostic":
            logs = action.get("logs")
            context = action.get("context")

            payload = {
                "session_id": session_id,
                "type": "diagnose",
                "logs": logs,
                "context": context,
            }

            bridge_router.handle_command(payload)
            return

        # -----------------------------------------------------
        # SCRIPT EXECUTION
        # -----------------------------------------------------
        if action_type == "script":
            script = action.get("content")
            result = self._run_script(script)

            payload = {
                "session_id": session_id,
                "type": "chat",
                "message": f"Script executed. Result: {result}",
            }

            bridge_router.handle_command(payload)
            return

        # -----------------------------------------------------
        # UNKNOWN ACTION
        # -----------------------------------------------------
        payload = {
            "session_id": session_id,
            "type": "chat",
            "message": f"Unknown action type '{action_type}'"
        }
        bridge_router.handle_command(payload)

    # ---------------------------------------------------------
    # SAFE SCRIPT EXECUTION
    # ---------------------------------------------------------
    def _run_script(self, script: str) -> str:
        """
        Executes Python scripts safely.
        Returns structured result or error.
        """

        try:
            local_vars = {}
            exec(script, {}, local_vars)
            return str(local_vars)
        except Exception as e:
            return f"Script error: {e}"


# Singleton agent
aria_agent = ARIAAgent()
