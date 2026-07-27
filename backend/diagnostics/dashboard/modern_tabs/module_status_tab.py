# backend/diagnostics/dashboard/modern_tabs/module_status_tab.py
import tkinter as tk
from tkinter import ttk


class ModuleStatusTab(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)

        self.app = app
        self.theme = app.theme_manager

        self.configure(bg=self.theme.get("bg"))
        self._build_ui()
        self._render_status()

    def _build_ui(self):
        title = tk.Label(
            self,
            text="Module Status",
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

    def _render_status(self):
        self.output.delete("1.0", tk.END)

        registry = getattr(self.app, "status_registry", {})
        corrections = getattr(self.app, "auto_corrections", [])

        self.output.insert(tk.END, "=== Module Registry ===\n")
        self.output.insert(tk.END, f"{registry}\n\n")

        self.output.insert(tk.END, "=== Auto-Corrections ===\n")
        for c in corrections:
            self.output.insert(tk.END, f"{c}\n")
