class DebugEngine:
    """
    Unified debug/logging engine for ARIA Lite.
    - Structured logs
    - Predictable return format
    - Safe for PacketExecutor, Router, Inspector panels
    """

    def __init__(self):
        self.logs: List[Dict[str, Any]] = []

    # ---------------------------------------------------------
    # LOG ENTRY
    # ---------------------------------------------------------
    def log(self, level: str, message: str, data: Dict[str, Any] | None = None) -> Dict[str, Any]:
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
        return {
            "status": "ok",
            "logs": self.logs[-limit:]
        }

    # ---------------------------------------------------------
    # CLEAR LOGS
    # ---------------------------------------------------------
    def clear_logs(self) -> Dict[str, Any]:
        self.logs.clear()
        return {
            "status": "ok",
            "message": "Logs cleared"
        }

    # ---------------------------------------------------------
    # PACKET HANDLER
    # ---------------------------------------------------------
    def handle_debug(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        """
        Envelope format:
        {
          "task": "debug",
          "operation": "log" | "get" | "clear",
          "level": "info" | "warn" | "error",
          "message": "text",
          "data": {...}
        }
        """

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

        return {
            "status": "error",
            "message": f"Unknown debug operation '{op}'"
        }
