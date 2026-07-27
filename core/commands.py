"""
ARIA Lite — Unified Command Registry
------------------------------------
Centralized command detection + execution for:
  - Core
  - Bridge
  - ChatManager
  - Copilot collaboration
"""

from __future__ import annotations
from typing import Optional, Dict, Any


class CommandResult:
    """
    Unified command return structure.
    Used by Core, Bridge, and ChatManager.
    """

    def __init__(
        self,
        ok: bool,
        reply: str,
        command_type: str = "normal",
        actions: Optional[list] = None,
        summary: Optional[str] = None,
    ):
        self.ok = ok
        self.reply = reply
        self.command_type = command_type
        self.actions = actions or []
        self.summary = summary

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": "ok" if self.ok else "error",
            "reply": self.reply,
            "type": self.command_type,
            "actions": self.actions,
            "summary": self.summary,
        }


# ---------------------------------------------------------
# COMMAND DETECTION
# ---------------------------------------------------------
def detect_command(message: str) -> Optional[str]:
    """
    Detects which command the user is invoking.
    Returns a command key or None.
    """

    msg = message.lower().strip()

    # Copilot packet generation
    if "generate copilot packet" in msg:
        return "copilot_packet"

    # Diagnostics
    if msg.startswith("diagnose ") or "analyze logs" in msg:
        return "diagnose"

    # Script generation
    if msg.startswith("make script") or msg.startswith("write script"):
        return "script_request"

    # Add more commands here as needed
    return None


# ---------------------------------------------------------
# COMMAND EXECUTION
# ---------------------------------------------------------
def execute_command(command_key: str, message: str, session_id: str) -> CommandResult:
    """
    Executes the command based on its key.
    Unified for Core + Bridge + ChatManager.
    """

    # -----------------------------------------------------
    # COPILOT PACKET COMMAND
    # -----------------------------------------------------
    if command_key == "copilot_packet":
        from bridge.copilot_agent import copilot_agent

        packet = copilot_agent.build_packet()

        reply_text = (
            "COPILOT PACKET (JSON):\n\n"
            + str(packet)
            + "\n\nCopy and paste this into Copilot."
        )

        return CommandResult(
            ok=True,
            reply=reply_text,
            command_type="packet_reply",
            actions=[],
            summary="Generated Copilot packet from current session.",
        )

    # -----------------------------------------------------
    # DIAGNOSTIC COMMAND
    # -----------------------------------------------------
    if command_key == "diagnose":
        # Minimal diagnostic stub (safe + unified)
        reply_text = f"Diagnostics triggered.\nContext: {message}"

        diagnosis = {
            "actions": [],
            "summary": "Diagnostics completed.",
        }

        return CommandResult(
            ok=True,
            reply=reply_text,
            command_type="diagnose_reply",
            actions=diagnosis["actions"],
            summary=diagnosis["summary"],
        )

    # -----------------------------------------------------
    # SCRIPT REQUEST COMMAND
    # -----------------------------------------------------
    if command_key == "script_request":
        from backend.script_engine import ScriptEngine

        engine = ScriptEngine()
        script = engine.generate(context=message, requirements="")

        return CommandResult(
            ok=True,
            reply="Script generated.",
            command_type="script_reply",
            actions=[
                {
                    "type": "script",
                    "language": "python",
                    "content": script,
                }
            ],
            summary="Script generated successfully.",
        )

    # -----------------------------------------------------
    # UNKNOWN COMMAND
    # -----------------------------------------------------
    return CommandResult(
        ok=False,
        reply=f"Unknown command: {command_key}",
        command_type="error",
        actions=[],
        summary="Command not recognized.",
    )
