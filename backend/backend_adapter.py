import traceback
import os
import time
from tkinter import messagebox


class BackendAdapter:
    """
    Unified backend adapter.
    Provides safe loading and safe execution wrappers around backend.router
    and any backend operation that must be isolated from UI crashes.
    """

    def __init__(self):
        # Load backend router safely
        self.router = self.safe_call("Import backend.router", self._load_router)

    # ---------------------------------------------------------
    # SAFE CALL WRAPPER
    # ---------------------------------------------------------
    def safe_call(self, label, func):
        """
        Execute a backend operation safely.
        - Logs timing
        - Captures exceptions
        - Writes unified error logs
        - Never crashes the UI
        """
        try:
            print(f"[BackendAdapter] Executing: {label}")
            start = time.time()

            result = func()

            elapsed = round((time.time() - start) * 1000, 2)
            print(f"[BackendAdapter] Completed: {label} ({elapsed} ms)")

            return result

        except Exception as e:
            self._report_error(label, e)
            return None

    # ---------------------------------------------------------
    # ERROR REPORTING
    # ---------------------------------------------------------
    def _report_error(self, label, exception):
        tb = traceback.format_exc()

        print("\n" + "=" * 60)
        print(f"[BackendAdapter ERROR] Action: {label}")
        print(f"[BackendAdapter ERROR] Exception: {exception}")
        print(tb)
        print("=" * 60 + "\n")

        os.makedirs("logs", exist_ok=True)
        with open("logs/backend_adapter_errors.log", "a", encoding="utf-8") as f:
            f.write(f"\n[{time.ctime()}] ERROR in '{label}': {exception}\n")
            f.write(tb + "\n")

        try:
            messagebox.showerror(
                "Backend Adapter Error",
                f"An error occurred while running '{label}'.\n\n{exception}"
            )
        except Exception:
            # UI may not be available — fail silently
            pass

    # ---------------------------------------------------------
    # ROUTER LOADING
    # ---------------------------------------------------------
    def _load_router(self):
        import backend.router as router
        return router

    # ---------------------------------------------------------
    # ENVELOPE NORMALIZATION
    # ---------------------------------------------------------
    def _normalize_envelope(self, envelope):
        """
        Normalize any envelope input into a safe dict structure.

        Accepts:
            - string task names ("/health", "ping", etc.)
            - dict envelopes
            - malformed envelopes (auto-corrected)

        Returns a dict:
            {
                "task": <string>,
                "args": <dict>,
                "meta": <dict>,
                ... (any extra keys preserved)
            }
        """

        # Case 1: simple string → treat as task
        if isinstance(envelope, str):
            return {
                "task": envelope,
                "args": {},
                "meta": {}
            }

        # Case 2: proper dict → ensure required keys exist
        if isinstance(envelope, dict):
            task = envelope.get("task") or envelope.get("action")

            # If dict has no task, infer or default
            if not task:
                if len(envelope) == 1:
                    task = next(iter(envelope.keys()))
                else:
                    task = "unknown"

            normalized = {
                "task": task,
                "args": envelope.get("args", {}),
                "meta": envelope.get("meta", {}),
            }

            # Preserve any extra keys
            for k, v in envelope.items():
                if k not in ["task", "action", "args", "meta"]:
                    normalized[k] = v

            return normalized

        # Case 3: anything else → wrap safely
        return {
            "task": "unknown",
            "args": {"raw": envelope},
            "meta": {}
        }

    # ---------------------------------------------------------
    # COMPATIBILITY: Stress Tester expects backend.send()
    # ---------------------------------------------------------
    def send(self, envelope):
        """
        Unified send() that accepts either:
        - a full envelope dict
        - a simple task/path string like "/health"
        """
        normalized = self._normalize_envelope(envelope)
        return self.process_envelope(normalized)

    # ---------------------------------------------------------
    # SEND TO ENDPOINT
    # ---------------------------------------------------------
    def send_to(self, endpoint, envelope):
        """
        Safely send an envelope to a specific backend endpoint.
        """
        normalized = self._normalize_envelope(envelope)

        return self.safe_call(
            f"Send To {endpoint}",
            lambda: self.router.send_to(endpoint, normalized)
        )

    # ---------------------------------------------------------
    # PROCESS ENVELOPE
    # ---------------------------------------------------------
    def process_envelope(self, envelope):
        """
        Safely process an envelope through backend.router.
        Unified entry point for all backend packet execution.
        """
        normalized = self._normalize_envelope(envelope)

        return self.safe_call(
            "Process Envelope",
            lambda: self.router.process_envelope(normalized)
        )
