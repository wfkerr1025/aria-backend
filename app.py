import tkinter as tk
import traceback
import os
import time
from tkinter import messagebox

from ui.window import ARIAWindow
from ui.themes import ThemeManager, ThemeAdapter

from core.plugin_loader import load_plugins
from core.actions import ARIAActions
from core.chat_manager import ChatManager
from core.architecture_adapter import verify_app_module, adapt_app_module

from bridge.command_router import bridge_router

from backend.backend_adapter import BackendAdapter
from backend.diagnostics.telemetry.telemetry_manager import TelemetryManager
from backend.diagnostics.diagnostics_controller import DiagnosticsController
from backend.status_registry import StatusRegistry
from types import SimpleNamespace

# ============================================================
# UNIFIED ARCHITECTURE CONTRACT
# ============================================================
ARIA_CONTRACT = {
    "required_app": [
        "backend",
        "chat_manager",
        "actions",
        "theme_manager",
        "theme_adapter",
        "window",
        "plugin_manager",
        "diagnostics_controller",
        "status_registry",
        # LLM/other subsystems can be added here explicitly when wired
        # "llm",
    ],
    "required_window": [
        "sidebar",
        "chat_panel",
        "input_bar",
        "menubar",
        "refresh_theme",
        "apply_scale",
        "open_packet_inspector",
        "open_backend_test_panel",
    ],
    "forbidden": [
        "interface",
        "backend_adapter",
        "legacy_backend",
        "legacy_llm",
        "root",
        "main_frame",
    ],
}


class ARIALiteApp:
    """
    Unified ARIA Lite Application Bootstrapper.
    - Safe-call architecture
    - Deterministic subsystem initialization
    - Unified backend + diagnostics pipeline
    - Unified theme propagation
    - Plug-n-play root orchestrator
    - Enforced Unified Architecture Contract
    """

    def __init__(self):
        # Root window
        self.root = tk.Tk()
        self.root.title("ARIA Lite")
        self.root.geometry("1000x700")

        # Contract
        self.contract = ARIA_CONTRACT

        # REQUIRED BY BACKEND CONTRACT
        self.window = None
        self.status_registry = {}

        # Safe-call system
        self.safe_call = self._install_safe_call()
        self._report_error = self._install_error_reporter()

        # ============================================================
        # CORE SUBSYSTEMS
        # ============================================================
        self.chat_manager = self.safe_call("Init ChatManager", ChatManager)
        self.actions = self.safe_call("Init ARIAActions", ARIAActions)
        self.backend = self.safe_call("Init BackendAdapter", BackendAdapter)

        self.last_packet = None
        self.last_packet_result = None

        # Attach actions to ChatManager
        self.safe_call(
            "Attach Actions to ChatManager",
            lambda: self.chat_manager.attach_actions(self.actions)
        )

        # Attach ChatManager to bridge router
        self.safe_call(
            "Attach ChatManager to Bridge",
            lambda: bridge_router.attach_chat_manager(self.chat_manager)
        )

        # Load plugins
        self.plugin_manager = self.safe_call("Load Plugins", load_plugins)

        # Enforce contract on plugins (if any)
        plugins = getattr(self.plugin_manager, "plugins", [])
        for plugin in plugins:
            self.check_module(plugin, plugin.__class__.__name__)

        # Telemetry FIRST
        self.telemetry = self.safe_call("Init TelemetryManager", TelemetryManager)

        # ============================================================
        # THEME SUBSYSTEM (owned by app)
        # ============================================================
        self.theme_manager = self.safe_call(
            "Init ThemeManager",
            lambda: ThemeManager(self.root)
        )
        self.theme_adapter = self.safe_call(
            "Init ThemeAdapter",
            lambda: ThemeAdapter(self.theme_manager)
        )
        self.theme_adapter.app = self

        # ============================================================
        # DIAGNOSTICS CONTROLLER (now has theme_manager)
        # ============================================================
        self.diagnostics_controller = self.safe_call(
            "Init DiagnosticsController",
            lambda: DiagnosticsController(app=self)
        )

        # Enforce contract on diagnostics controller
        self.check_module(self.diagnostics_controller, "DiagnosticsController")

        # MAIN UI WINDOW (uses app-owned subsystems)
        self.window = self.safe_call(
            "Init ARIAWindow",
            lambda: ARIAWindow(
                root=self.root,
                actions=self.actions,
                chat_manager=self.chat_manager,
                app=self
            )
        )
        # NOTE: ARIAWindow is the host; it is NOT checked against the app contract here.

        # Build unified UI (Sidebar + MenuBar)
        self.safe_call("Build ARIAWindow UI", self.window.build)

        # ============================================================
        # DIAGNOSTICS STARTUP
        # ============================================================
        self.safe_call(
            "Run Diagnostics Startup",
            lambda: self.diagnostics_controller.eager_initialize()
        )

        # Status Registry
        self.status_registry = StatusRegistry()

        # Simple subsystem registry for modules to query
        self.subsystems = {
            "backend": self.backend,
            "chat": self.chat_manager,
            "actions": self.actions,
            "theme_manager": self.theme_manager,
            "theme_adapter": self.theme_adapter,
            "window": self.window,
            "plugins": self.plugin_manager,
            "diagnostics": self.diagnostics_controller,
            "status_registry": self.status_registry,
        }

        self.config = SimpleNamespace(
            workspace_root="workspace",
            projects_root="workspace/projects",
            python_path="python",
            powershell_path="pwsh",
            shell_path="/bin/bash",
            git_path="git",
            flake8_path="flake8",
            black_path="black",
            isort_path="isort"
        )

    # ============================================================
    # SAFE CALL SYSTEM
    # ============================================================
    def _install_safe_call(self):
        def safe_call(label, func):
            try:
                print(f"[ARIALiteApp] Executing: {label}")
                start = time.time()
                result = func()
                elapsed = round((time.time() - start) * 1000, 2)
                print(f"[ARIALiteApp] Completed: {label} ({elapsed} ms)")
                return result
            except Exception as e:
                self._report_error(label, e)
                return None
        return safe_call

    def _install_error_reporter(self):
        def report_error(label, exception):
            tb = traceback.format_exc()
            print("\n" + "=" * 60)
            print(f"[ARIALiteApp ERROR] Action: {label}")
            print(f"[ARIALiteApp ERROR] Exception: {exception}")
            print(tb)
            print("=" * 60 + "\n")

            os.makedirs("logs", exist_ok=True)
            with open("logs/app_errors.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{time.ctime()}] ERROR in '{label}': {exception}\n")
                f.write(tb + "\n")

            try:
                messagebox.showerror(
                    "ARIA Lite Error",
                    f"An error occurred while running '{label}'.\n\n{exception}"
                )
            except Exception:
                pass
        return report_error

    # Convenience facade for backend calls
    def send(self, route, payload=None):
        return self.backend.send_to(route, payload)

    # ============================================================
    # CONTRACT ENFORCEMENT / ADAPTATION
    # ============================================================
    def check_module(self, module, module_name="UnknownModule"):
        """
        Run contract verification + auto-correction for a module.
        Returns True if the module is safe to integrate.
        """
        if not verify_app_module(self, module, module_name):
            if not adapt_app_module(self, module, module_name):
                print(f"[ARIA Contract Failure] {module_name} cannot be integrated safely")
                return False
        return True
    
    # ============================================================
    # UNIFIED THEME REFRESH DISPATCHER
    # ============================================================
    def refresh_theme(self):
        """
        Unified theme refresh dispatcher.
        Called by MenuBar when user switches themes.
        """
        theme = self.theme_manager.current_theme

        # Refresh ARIAWindow
        if hasattr(self, "window") and self.window:
            if hasattr(self.window, "apply_theme"):
                self.window.apply_theme(theme)

        # Refresh Diagnostics Window
        if hasattr(self, "diagnostics_controller"):
            diag_win = getattr(self.diagnostics_controller, "window", None)
            if diag_win and hasattr(diag_win, "apply_theme"):
                diag_win.apply_theme(theme)

        # Refresh backend windows (Task Browser, Console, Packet Inspector, etc.)
        open_windows = getattr(self, "open_windows", [])
        for win in open_windows:
            if hasattr(win, "apply_theme"):
                win.apply_theme(theme)

    # ============================================================
    # RUN LOOP
    # ============================================================
    def run(self):
        self.safe_call("Mainloop", self.root.mainloop)


if __name__ == "__main__":
    app = ARIALiteApp()
    app.run()
