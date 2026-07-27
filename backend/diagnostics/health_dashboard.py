import tkinter as tk
from tkinter import ttk, messagebox
import traceback
import os
import time

from backend.diagnostics.telemetry.telemetry_panel import TelemetryPanel


class HealthDashboard(tk.Frame):
    """
    Unified architecture:
    - Frame-based diagnostic panel
    - (parent, app) signature
    - Displays startup summary + subsystem status + telemetry
    """

    def __init__(self, parent, app):
        super().__init__(parent)

        # Unified architecture references
        self.app = app
        self.backend = app.backend
        self.theme = app.theme_manager
        self.window = app.window

        self.startup_result = app.startup_result
        self.telemetry = app.telemetry

        # Install safe_call + error reporter
        self.safe_call = self._install_safe_call()
        self._report_error = self._install_error_reporter()

        # Build UI safely
        self.safe_call("Build HealthDashboard UI", self._build_ui)

    # =========================================================
    # SAFE CALL SYSTEM
    # =========================================================
    def _install_safe_call(self):
        def safe_call(label, func):
            try:
                print(f"[HealthDashboard] Executing: {label}")
                start = time.time()
                result = func()
                elapsed = round((time.time() - start) * 1000, 2)
                print(f"[HealthDashboard] Completed: {label} ({elapsed} ms)")
                return result
            except Exception as e:
                self._report_error(label, e)
                return None
        return safe_call

    def _install_error_reporter(self):
        def report_error(label, exception):
            tb = traceback.format_exc()

            print("\n" + "=" * 60)
            print(f"[HealthDashboard ERROR] Action: {label}")
            print(f"[HealthDashboard ERROR] Exception: {exception}")
            print(tb)
            print("=" * 60 + "\n")

            os.makedirs("logs", exist_ok=True)
            with open("logs/health_dashboard_errors.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{time.ctime()}] ERROR in '{label}': {exception}\n")
                f.write(tb + "\n")

            try:
                messagebox.showerror(
                    "Health Dashboard Error",
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
        # Startup Summary Header
        # ---------------------------------------------------------
        header = tk.Label(
            main_panel,
            text="Startup Summary",
            bg=self.theme.get("header_bg"),
            fg=self.theme.get("header_fg"),
            font=self.theme.get("font_header")
        )
        header.pack(anchor="w", pady=(0, 10))

        # ---------------------------------------------------------
        # Summary Section
        # ---------------------------------------------------------
        summary_section = tk.Frame(main_panel, bg=self.theme.get("panel_bg"))
        summary_section.pack(fill="x", pady=(0, 10))

        dur_lbl = tk.Label(
            summary_section,
            text=f"Startup Duration: {self.startup_result.duration:.2f}s",
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("text"),
            font=self.theme.get("font_default")
        )
        dur_lbl.pack(anchor="w")

        subs_lbl = tk.Label(
            summary_section,
            text=f"Subsystems Checked: {len(self.startup_result.subsystems)}",
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("text"),
            font=self.theme.get("font_default")
        )
        subs_lbl.pack(anchor="w")

        ts_lbl = tk.Label(
            summary_section,
            text=f"Timestamp: {self.startup_result.timestamp:.0f}",
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("text"),
            font=self.theme.get("font_default")
        )
        ts_lbl.pack(anchor="w")

        # ---------------------------------------------------------
        # Subsystem Status Header
        # ---------------------------------------------------------
        status_header = tk.Label(
            main_panel,
            text="Subsystem Status",
            bg=self.theme.get("header_bg"),
            fg=self.theme.get("header_fg"),
            font=self.theme.get("font_header")
        )
        status_header.pack(anchor="w", pady=(10, 5))

        # ---------------------------------------------------------
        # Subsystem Status Grid
        # ---------------------------------------------------------
        grid_section = tk.Frame(main_panel, bg=self.theme.get("panel_bg"))
        grid_section.pack(fill="x", pady=(0, 10))

        for subsystem in self.startup_result.subsystems:
            row = tk.Frame(grid_section, bg=self.theme.get("panel_bg"))
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

        # ---------------------------------------------------------
        # Telemetry Panel
        # ---------------------------------------------------------
        telemetry_section = tk.Frame(main_panel, bg=self.theme.get("panel_bg"))
        telemetry_section.pack(fill="x", pady=(10, 0))

        TelemetryPanel(telemetry_section, self.app).pack(fill="x")
