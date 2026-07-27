# windows/backend_startup_panel.py
import json
import tkinter as tk
from tkinter import ttk
import traceback
import time
import os

from core.architecture_adapter import verify_window_module, adapt_window_module
from backend.diagnostics.dashboard.backend_startup_selftest import startup_selftest


class BackendStartupPanel(tk.Toplevel):
    """
    Unified ARIA Lite Startup Diagnostic Dashboard.
    - Safe-call architecture
    - Theme-aware (if app.theme_manager exists)
    - Deterministic rendering
    - Scrollable diagnostic sections
    - Plug-n-play standalone window
    """

    def __init__(self, app=None):
        super().__init__()

        self.app = app
        self.results = None

        # Window config
        self.geometry("900x700")
        self.title("ARIA Lite Startup Diagnostics")

        # Contract enforcement (window-level)
        self._check_ui_module()

        # Safe-call system
        self.safe_call = self._install_safe_call()
        self._report_error = self._install_error_reporter()

        # Build UI
        self.safe_call("Build UI", self._build_ui)

        # Run tests immediately
        self.safe_call("Run Startup Tests", self.run_tests)

        # Apply theme if available
        if self.app and hasattr(self.app, "theme_manager"):
            self.safe_call("Apply Theme", self._apply_theme)

    # ============================================================
    # CONTRACT ENFORCEMENT
    # ============================================================
    def _check_ui_module(self):
        """
        Enforce ARIA Lite window-level contract on this backend window.
        Mirrors ARIAWindow + DiagnosticsWindow behavior.
        """
        if self.app is None:
            return

        window = getattr(self.app, "window", None)
        if window is None:
            return

        name = self.__class__.__name__

        if not verify_window_module(window, self, name):
            adapt_window_module(window, self, name)

    # ============================================================
    # SAFE CALL SYSTEM
    # ============================================================
    def _install_safe_call(self):
        def safe_call(label, func):
            try:
                print(f"[BackendStartupPanel] Executing: {label}")
                start = time.time()
                result = func()
                elapsed = round((time.time() - start) * 1000, 2)
                print(f"[BackendStartupPanel] Completed: {label} ({elapsed} ms)")
                return result
            except Exception as e:
                self._report_error(label, e)
                return None
        return safe_call

    def _install_error_reporter(self):
        def report_error(label, exception):
            tb = traceback.format_exc()
            print("\n" + "=" * 60)
            print(f"[BackendStartupPanel ERROR] Action: {label}")
            print(f"[BackendStartupPanel ERROR] Exception: {exception}")
            print(tb)
            print("=" * 60 + "\n")

            os.makedirs("logs", exist_ok=True)
            with open("logs/backend_startup_panel_errors.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{time.ctime()}] ERROR in '{label}': {exception}\n")
                f.write(tb + "\n")
        return report_error

    # ============================================================
    # UI CONSTRUCTION
    # ============================================================
    def _build_ui(self):
        self.container = tk.Frame(self, padx=10, pady=10)
        self.container.pack(fill="both", expand=True)

        # Title
        self.title_label = tk.Label(
            self.container,
            text="ARIA Lite Startup Diagnostic Report",
            font=("Segoe UI", 18, "bold")
        )
        self.title_label.pack(pady=(0, 10))

        # Refresh button
        self.refresh_button = tk.Button(
            self.container,
            text="Run Diagnostics Again",
            command=lambda: self.safe_call("Run Startup Tests", self.run_tests),
            width=25
        )
        self.refresh_button.pack(pady=(0, 10))

        # Scrollable results area
        self.canvas = tk.Canvas(self.container)
        self.scrollbar = ttk.Scrollbar(self.container, orient="vertical", command=self.canvas.yview)

        self.scrollable_frame = tk.Frame(self.canvas)
        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

    # ============================================================
    # RUN STARTUP TESTS
    # ============================================================
    def run_tests(self):
        self.results = startup_selftest()
        self.safe_call("Render Results", self._render_results)

    # ============================================================
    # RENDER RESULTS
    # ============================================================
    def _render_results(self):
        # Clear previous content
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()

        # Diagnostic sections
        for key, value in self.results.items():
            if key == "boot_log":
                continue

            section = tk.LabelFrame(
                self.scrollable_frame,
                text=key.upper(),
                padx=10,
                pady=10,
                font=("Segoe UI", 12, "bold")
            )
            section.pack(fill="x", pady=5)

            # Status
            status = value.get("status") if isinstance(value, dict) else None
            if status:
                tk.Label(section, text=f"Status: {status}", font=("Segoe UI", 11)).pack(anchor="w")

            # Detailed JSON
            detail = tk.Label(
                section,
                text=json.dumps(value, indent=4),
                font=("Consolas", 10),
                justify="left"
            )
            detail.pack(anchor="w")

        # Boot log section
        boot_log = self.results.get("boot_log", "")
        log_section = tk.LabelFrame(
            self.scrollable_frame,
            text="BOOT LOG",
            padx=10,
            pady=10,
            font=("Segoe UI", 12, "bold")
        )
        log_section.pack(fill="x", pady=5)

        tk.Label(
            log_section,
            text=boot_log,
            font=("Consolas", 10),
            justify="left"
        ).pack(anchor="w")

    # ============================================================
    # APPLY THEME (Unified)
    # ============================================================
    def _apply_theme(self):
        theme = self.app.theme_manager.current_theme

        # Window background
        self.configure(bg=theme["bg"])
        self.container.configure(bg=theme["bg"])
        self.scrollable_frame.configure(bg=theme["bg"])
        self.canvas.configure(bg=theme["bg"])

        # Title
        self.title_label.configure(
            bg=theme["bg"],
            fg=theme["text"]
        )

        # Refresh button
        self.refresh_button.configure(
            bg=theme["panel_bg"],
            fg=theme["text"]
        )

        # LabelFrames + content
        for child in self.scrollable_frame.winfo_children():
            try:
                child.configure(bg=theme["panel_bg"], fg=theme["text"])
            except:
                pass

            for sub in child.winfo_children():
                try:
                    sub.configure(bg=theme["panel_bg"], fg=theme["text"])
                except:
                    pass
