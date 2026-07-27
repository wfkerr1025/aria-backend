# backend/diagnostics/dashboard/modern_tabs/metadata_tab.py
import tkinter as tk
from tkinter import ttk


class MetadataTab(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)

        self.app = app
        self.backend = app.backend
        self.theme = app.theme_manager

        self.configure(bg=self.theme.get("bg"))
        self._build_ui()
        self._render_metadata()

    def _build_ui(self):
        title = tk.Label(
            self,
            text="Backend Metadata",
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

    def _render_metadata(self):
        self.output.delete("1.0", tk.END)

        try:
            meta = self.backend.send("/metadata")
            self.output.insert(tk.END, f"{meta}\n")
        except Exception as e:
            self.output.insert(tk.END, f"Metadata ERROR: {e}\n")
