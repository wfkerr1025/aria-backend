# backend/diagnostics/dashboard/diagnostics_window.py

import tkinter as tk
from tkinter import ttk


class DiagnosticsWindow(tk.Toplevel):
    """
    Unified Diagnostics Window for ARIA Lite.
    - Tab-based diagnostics UI
    - Uses controller-provided tab factories
    - Theme-aware
    - Contract-clean (no auto-correction hooks)
    """

    def __init__(self, root, app, controller):
        super().__init__(root)

        self.app = app
        self.controller = controller
        self.theme = app.theme_manager

        self.title("ARIA Diagnostics Suite")
        self.geometry("1000x700")
        self.configure(bg=self.theme.get("bg"))

        self.tabs = {}
        self._build_ui()

        print("[DiagnosticsWindow] Loaded from:", __file__)

    # ============================================================
    # BUILD UI
    # ============================================================
    def _build_ui(self):
        # Title
        title = tk.Label(
            self,
            text="ARIA Diagnostics Suite",
            font=("Segoe UI", 16, "bold"),
            bg=self.theme.get("bg"),
            fg=self.theme.get("text"),
        )
        title.pack(pady=10)

        # Notebook
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)

        # Dynamically build tabs from controller
        tabs = self.controller.get_tabs()
        for name, factory in tabs.items():
            self._add_tab(name, factory)

    # ============================================================
    # TAB REGISTRATION
    # ============================================================
    def _add_tab(self, name, factory):
        """
        Create a tab using the controller factory and add it to the notebook.
        """
        frame = tk.Frame(self.notebook, bg=self.theme.get("bg"))
        tab = factory(frame)

        if tab is not None:
            tab.pack(fill="both", expand=True)
            self.notebook.add(frame, text=name)
            self.tabs[name] = tab

    # ============================================================
    # FINAL BUILD HOOK
    # ============================================================
    def build(self):
        """No-op for future expansion."""
        pass

    def register_tab(self, name: str, builder):
        """
        Registers a diagnostics tab.
        builder(parent_frame) → builds the tab UI.
        """
        frame = ttk.Frame(self.notebook)
        builder(frame)
        self.notebook.add(frame, text=name)
