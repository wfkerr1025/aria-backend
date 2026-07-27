# startup_diagnostic_report.py
import tkinter as tk
from tkinter import ttk, messagebox
import traceback
import os
import time

from backend.diagnostics.startup_check_engine import StartupResult


class StartupDiagnosticReport(tk.Frame):
    """
    Unified architecture:
    - Frame-based diagnostic panel
    - (parent, app) signature
    - Displays full startup diagnostic report
    """

    def __init__(self, parent, app):
        super().__init__(parent)

        # Unified architecture references
        self.app = app
        self.backend = app.backend
        self.theme = app.theme_manager
        self.window = app.window

        self.startup_result: StartupResult = app.startup_result

        # Install safe_call + error reporter
        self.safe_call = self._install_safe_call()
        self._report_error = self._install_error_reporter()

        # Build UI safely
        self.safe_call("Build StartupDiagnosticReport UI", self._build_ui)

    # =========================================================
    # SAFE CALL SYSTEM
    # =========================================================
    def _install_safe_call(self):
        def safe_call(label, func):
            try:
                print(f"[StartupDiagnosticReport] Executing: {label}")
                start = time.time()
                result = func()
                elapsed = round((time.time() - start) * 1000, 2)
                print(f"[StartupDiagnosticReport] Completed: {label} ({elapsed} ms)")
                return result
            except Exception as e:
                self._report_error(label, e)
                return None
        return safe_call

    def _install_error_reporter(self):
        def report_error(label, exception):
            tb = traceback.format_exc()

            print("\n" + "=" * 60)
            print(f"[StartupDiagnosticReport ERROR] Action: {label}")
            print(f"[StartupDiagnosticReport ERROR] Exception: {exception}")
            print(tb)
            print("=" * 60 + "\n")

            os.makedirs("logs", exist_ok=True)
            with open("logs/startup_diagnostic_report_errors.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{time.ctime()}] ERROR in '{label}': {exception}\n")
                f.write(tb + "\n")

            try:
                messagebox.showerror(
                    "Startup Diagnostic Report Error",
                    f"An error occurred while running '{label}'.\n\n{exception}"
                )
            except Exception:
                pass

        return report_error

    # =========================================================
    # BUILD UI
    # =========================================================
    def _build_ui(self):
        self.configure(bg=self.theme.get("bg"))

        main_panel = tk.Frame(self, bg=self.theme.get("panel_bg"))
        main_panel.pack(fill="both", expand=True, padx=10, pady=10)

        # ---------------------------------------------------------
        # Header
        # ---------------------------------------------------------
        header = tk.Label(
            main_panel,
            text="Startup Diagnostic Report",
            bg=self.theme.get("header_bg"),
            fg=self.theme.get("header_fg"),
            font=self.theme.get("font_header")
        )
        header.pack(anchor="w", pady=(0, 10))

        # ---------------------------------------------------------
        # Info Section
        # ---------------------------------------------------------
        info = tk.Frame(main_panel, bg=self.theme.get("panel_bg"))
        info.pack(fill="x", pady=(0, 10))

        boot_id_lbl = tk.Label(
            info,
            text=f"Boot ID: {self.startup_result.boot_id}",
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("text"),
            font=self.theme.get("font_default")
        )
        boot_id_lbl.pack(anchor="w")

        duration_lbl = tk.Label(
            info,
            text=f"Duration: {self.startup_result.duration:.2f}s",
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("text"),
            font=self.theme.get("font_default")
        )
        duration_lbl.pack(anchor="w")

        timestamp_lbl = tk.Label(
            info,
            text=f"Timestamp: {self.startup_result.timestamp:.0f}",
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("text"),
            font=self.theme.get("font_default")
        )
        timestamp_lbl.pack(anchor="w")

        # ---------------------------------------------------------
        # Subsystem Header
        # ---------------------------------------------------------
        subs_header = tk.Label(
            main_panel,
            text="Subsystem Details",
            bg=self.theme.get("header_bg"),
            fg=self.theme.get("header_fg"),
            font=self.theme.get("font_header")
        )
        subs_header.pack(anchor="w", pady=(10, 5))

        # ---------------------------------------------------------
        # Subsystem List
        # ---------------------------------------------------------
        list_frame = tk.Frame(main_panel, bg=self.theme.get("panel_bg"))
        list_frame.pack(fill="both", expand=True, pady=(0, 10))

        for subsystem in self.startup_result.subsystems:
            row = tk.Frame(list_frame, bg=self.theme.get("panel_bg"))
            row.pack(fill="x", pady=2)

            # Name
            name_lbl = tk.Label(
                row,
                text=subsystem.name,
                width=20,
                bg=self.theme.get("panel_bg"),
                fg=self.theme.get("text"),
                font=self.theme.get("font_default")
            )
            name_lbl.pack(side="left")

            # Status (color-coded)
            status_lbl = tk.Label(
                row,
                text=subsystem.status.upper(),
                width=10,
                bg=self.theme.get("panel_bg"),
                fg=self.theme.get("text"),
                font=self.theme.get("font_default")
            )
            self.theme.apply_status(status_lbl, subsystem.status)
            status_lbl.pack(side="left")

            # Message
            msg_lbl = tk.Label(
                row,
                text=subsystem.message,
                bg=self.theme.get("panel_bg"),
                fg=self.theme.get("text"),
                font=self.theme.get("font_default")
            )
            msg_lbl.pack(side="left")
