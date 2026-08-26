from __future__ import annotations
from typing import Dict, Any, List
import time


from logger import get_logger

logger = get_logger(__name__)

class DebugEngine:
    """
    Unified debug/logging engine for ARIA Lite.
    - Structured logs
    - Predictable return format
    - Safe for PacketExecutor, Router, Inspector panels
    """

    def __init__(self):
        logger.debug("Initializing DebugEngine")
        self.logs: List[Dict[str, Any]] = []

    # ---------------------------------------------------------
    # LOG ENTRY
    # ---------------------------------------------------------
    def log(self, level: str, message: str, data: Dict[str, Any] | None = None) -> Dict[str, Any]:
        logger.debug(f"log() → level={level}, message={message}, data={data}")

        entry = {
            "timestamp": time.time(),
            "level": level,
            "message": message,
            "data": data or {},
        }

        self.logs.append(entry)

        return {
            "status": "ok",
            "entry": entry
        }

    # ---------------------------------------------------------
    # GET LOGS
    # ---------------------------------------------------------
    def get_logs(self, limit: int = 100) -> Dict[str, Any]:
        logger.debug(f"get_logs() → limit={limit}")

        return {
            "status": "ok",
            "logs": self.logs[-limit:]
        }

    # ---------------------------------------------------------
    # CLEAR LOGS
    # ---------------------------------------------------------
    def clear_logs(self) -> Dict[str, Any]:
        logger.debug("clear_logs()")

        self.logs.clear()
        return {
            "status": "ok",
            "message": "Logs cleared"
        }

    # ---------------------------------------------------------
    # PACKET HANDLER
    # ---------------------------------------------------------
    def handle_debug(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        logger.debug(f"handle_debug() → envelope={envelope}")

        op = envelope.get("operation")

        if op == "log":
            return self.log(
                envelope.get("level", "info"),
                envelope.get("message", ""),
                envelope.get("data", {}) or {}
            )

        if op == "get":
            return self.get_logs(
                envelope.get("limit", 100)
            )

        if op == "clear":
            return self.clear_logs()

        logger.debug(f"Unknown debug operation → {op}")

        return {
            "status": "error",
            "message": f"Unknown debug operation '{op}'"
        }
