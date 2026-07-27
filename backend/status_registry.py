# backend/status_registry.py
import time
import traceback
import os


class StatusRegistry:
    """
    Unified Module Status System (UMSS)
    Tracks:
    - module load events
    - plugin load events
    - backend health
    - diagnostics events
    - performance metrics
    """

    def __init__(self):
        self.status = {
            "modules": {},
            "plugins": {},
            "backend": {},
            "diagnostics": {},
            "performance": {},
        }

        os.makedirs("logs", exist_ok=True)
        self.log_path = "logs/status_registry.log"

    # ---------------------------------------------------------
    # INTERNAL LOGGING
    # ---------------------------------------------------------
    def _log(self, message):
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(f"[{time.ctime()}] {message}\n")

    # ---------------------------------------------------------
    # MODULE STATUS
    # ---------------------------------------------------------
    def mark_loaded(self, name):
        self.status["modules"][name] = {
            "status": "loaded",
            "timestamp": time.time()
        }
        self._log(f"MODULE LOADED: {name}")

    def mark_failed(self, name, error):
        tb = traceback.format_exc()
        self.status["modules"][name] = {
            "status": "failed",
            "error": str(error),
            "traceback": tb,
            "timestamp": time.time()
        }
        self._log(f"MODULE FAILED: {name} — {error}\n{tb}")

    # ---------------------------------------------------------
    # PLUGIN STATUS
    # ---------------------------------------------------------
    def plugin_loaded(self, name):
        self.status["plugins"][name] = {
            "status": "loaded",
            "timestamp": time.time()
        }
        self._log(f"PLUGIN LOADED: {name}")

    def plugin_failed(self, name, error):
        tb = traceback.format_exc()
        self.status["plugins"][name] = {
            "status": "failed",
            "error": str(error),
            "traceback": tb,
            "timestamp": time.time()
        }
        self._log(f"PLUGIN FAILED: {name} — {error}\n{tb}")

    # ---------------------------------------------------------
    # BACKEND STATUS
    # ---------------------------------------------------------
    def backend_event(self, event, data=None):
        self.status["backend"][event] = {
            "data": data,
            "timestamp": time.time()
        }
        self._log(f"BACKEND EVENT: {event} — {data}")

    # ---------------------------------------------------------
    # DIAGNOSTICS
    # ---------------------------------------------------------
    def diagnostics_event(self, event, data=None):
        self.status["diagnostics"][event] = {
            "data": data,
            "timestamp": time.time()
        }
        self._log(f"DIAGNOSTICS EVENT: {event} — {data}")

    # ---------------------------------------------------------
    # PERFORMANCE
    # ---------------------------------------------------------
    def performance_metric(self, name, value):
        self.status["performance"][name] = {
            "value": value,
            "timestamp": time.time()
        }
        self._log(f"PERFORMANCE: {name} = {value}")

    # ---------------------------------------------------------
    # PUBLIC API
    # ---------------------------------------------------------
    def get_status(self):
        return self.status
