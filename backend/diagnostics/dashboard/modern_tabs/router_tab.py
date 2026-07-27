# backend/diagnostics/dashboard/modern_tabs/router_tab.py
import tkinter as tk
from tkinter import ttk


class RouterTab(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)

        self.app = app
        self.backend = app.backend
        self.theme = app.theme_manager

        self.configure(bg=self.theme.get("bg"))
        self._build_ui()
        self._render_router()

    def _build_ui(self):
        title = tk.Label(
            self,
            text="Backend Router Map",
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

    def _render_router(self):
        self.output.delete("1.0", tk.END)

        try:
            routes = self.backend.router_map()
            self.output.insert(tk.END, f"{routes}\n")
        except Exception as e:
            self.output.insert(tk.END, f"Router ERROR: {e}\n")
