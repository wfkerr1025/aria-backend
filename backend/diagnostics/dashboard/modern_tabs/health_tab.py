# backend/diagnostics/dashboard/modern_tabs/health_tab.py
import tkinter as tk
from tkinter import ttk
import time
import traceback


class HealthTab(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)

        self.app = app
        self.backend = app.backend
        self.theme = app.theme_manager

        self.safe_call = self._install_safe_call()
        self._report_error = self._install_error_reporter()

        self.safe_call("Build Health Tab UI", self._build_ui)
        self.safe_call("Run Health Checks", self._run_checks)

    # ============================================================
    # SAFE CALL
    # ============================================================
    def _install_safe_call(self):
        def safe_call(label, func):
            try:
                print(f"[HealthTab] Executing: {label}")
                start = time.time()
                result = func()
                elapsed = round((time.time() - start) * 1000, 2)
                print(f"[HealthTab] Completed: {label} ({elapsed} ms)")
                return result
            except Exception as e:
                self._report_error(label, e)
                return None
        return safe_call

    def _install_error_reporter(self):
        def report_error(label, exception):
            tb = traceback.format_exc()
            print("\n" + "=" * 60)
            print(f"[HealthTab ERROR] Action: {label}")
            print(f"[HealthTab ERROR] Exception: {exception}")
            print(tb)
            print("=" * 60 + "\n")
        return report_error

    # ============================================================
    # UI
    # ============================================================
    def _build_ui(self):
        self.configure(bg=self.theme.get("bg"))

        title = tk.Label(
            self,
            text="Backend Health Status",
            font=("Segoe UI", 16, "bold"),
            bg=self.theme.get("bg"),
            fg=self.theme.get("text")
        )
        title.pack(pady=10)

        self.output = tk.Text(
            self,
            height=25,
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("text"),
            font=("Consolas", 10)
        )
        self.output.pack(fill="both", expand=True, padx=10, pady=10)

        refresh = tk.Button(
            self,
            text="Refresh",
            command=lambda: self.safe_call("Run Health Checks", self._run_checks),
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("text")
        )
        refresh.pack(pady=5)

    # ============================================================
    # CHECKS
    # ============================================================
    def _run_checks(self):
        self.output.delete("1.0", tk.END)

        def write(label, data):
            self.output.insert(tk.END, f"{label}:\n{data}\n\n")

        # /health
        try:
            resp = self.backend.send("/health")
            write("Health", resp)
        except Exception as e:
            write("Health ERROR", str(e))

        # /ping
        try:
            start = time.time()
            resp = self.backend.send("/ping")
            latency = round((time.time() - start) * 1000, 2)
            write("Ping", {"latency_ms": latency, "response": resp})
        except Exception as e:
            write("Ping ERROR", str(e))
