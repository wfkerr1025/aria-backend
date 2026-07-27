# backend/diagnostics/diagnostics_controller.py

import os
import time
import traceback
import tkinter as tk

from backend.diagnostics.dashboard.diagnostics_window import DiagnosticsWindow

# Modern diagnostics tabs
from backend.diagnostics.dashboard.modern_tabs.health_tab import HealthTab
from backend.diagnostics.dashboard.modern_tabs.tools_tab import ToolsTab
from backend.diagnostics.dashboard.modern_tabs.module_status_tab import ModuleStatusTab
from backend.diagnostics.dashboard.modern_tabs.router_tab import RouterTab
from backend.diagnostics.dashboard.modern_tabs.context_tab import ContextTab
from backend.diagnostics.dashboard.modern_tabs.metadata_tab import MetadataTab


class DiagnosticsController:
    """
    Unified Diagnostics Controller for ARIA Lite.
    - No contract adapter hooks (no auto-correction)
    - Provides tab factories
    - DiagnosticsWindow builds tabs via get_tabs()
    """

    def __init__(self, app):
        self.app = app
        self.backend = app.backend
        self.theme = app.theme_manager
        self.telemetry = getattr(app, "telemetry", None)

        self.window = None
        self.last_result = None

        self.safe_call = self._install_safe_call()
        self._report_error = self._install_error_reporter()

        print("[DiagnosticsController] Loaded from:", __file__)

    # ============================================================
    # SAFE CALL SYSTEM
    # ============================================================
    def _install_safe_call(self):
        def safe_call(label, func):
            try:
                print(f"[DiagnosticsController] Executing: {label}")
                start = time.time()
                result = func()
                elapsed = round((time.time() - start) * 1000, 2)
                print(f"[DiagnosticsController] Completed: {label} ({elapsed} ms)")
                return result
            except Exception as e:
                self._report_error(label, e)
                return None
        return safe_call

    def _install_error_reporter(self):
        def report_error(label, exception):
            tb = traceback.format_exc()

            print("\n" + "=" * 60)
            print(f"[DiagnosticsController ERROR] Action: {label}")
            print(f"[DiagnosticsController ERROR] Exception: {exception}")
            print(tb)
            print("=" * 60 + "\n")

            os.makedirs("logs", exist_ok=True)
            with open("logs/diagnostics_controller_errors.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{time.ctime()}] ERROR in '{label}': {exception}\n")
                f.write(tb + "\n")

        return report_error

    # ============================================================
    # STARTUP DIAGNOSTICS
    # ============================================================
    def eager_initialize(self):
        self.safe_call("Run Startup Check", self._run_startup_check)

    def _run_startup_check(self):
        try:
            resp = self.backend.send("/health")
            self.last_result = resp

            if self.telemetry:
                self.telemetry.record_startup(0.0)

        except Exception as e:
            self.last_result = {"status": "error", "detail": str(e)}

    # ============================================================
    # OPEN DIAGNOSTICS WINDOW
    # ============================================================
    def open_window(self):
        def _open():
            if self.window is not None and self.window.winfo_exists():
                self.window.destroy()

            ui_window = getattr(self.app, "window", None)
            root = ui_window.root if ui_window is not None else None

            self.window = DiagnosticsWindow(
                root=root,
                app=self.app,
                controller=self
            )
            self.window.build()

        self.safe_call("Open Diagnostics Window", _open)

    # ============================================================
    # TAB FACTORIES
    # ============================================================
    def create_health_tab(self, parent):
        return self.safe_call("Create Health Tab", lambda: HealthTab(parent, self.app))

    def create_tools_tab(self, parent):
        return self.safe_call("Create Tools Tab", lambda: ToolsTab(parent, self.app))

    def create_module_status_tab(self, parent):
        return self.safe_call(
            "Create Module Status Tab",
            lambda: ModuleStatusTab(parent, self.app),
        )

    def create_router_tab(self, parent):
        return self.safe_call("Create Router Tab", lambda: RouterTab(parent, self.app))

    def create_context_tab(self, parent):
        return self.safe_call("Create Context Tab", lambda: ContextTab(parent, self.app))

    def create_metadata_tab(self, parent):
        return self.safe_call("Create Metadata Tab", lambda: MetadataTab(parent, self.app))

    def create_test_tab(self, parent):
        from backend.diagnostics.dashboard.modern_tabs.test_tab import TestTab
        return self.safe_call(
            "Create Test Tab",
            lambda: TestTab(parent, self.app, self.theme),
        )

    # ============================================================
    # TAB REGISTRY
    # ============================================================
    def get_tabs(self):
        return {
            "Health": self.create_health_tab,
            "Tools": self.create_tools_tab,
            "Modules": self.create_module_status_tab,
            "Router": self.create_router_tab,
            "Context": self.create_context_tab,
            "Metadata": self.create_metadata_tab,
            "Tests": self.create_test_tab,
        }
