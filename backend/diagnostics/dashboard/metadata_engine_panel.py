import tkinter as tk
from tkinter import ttk


class MetadataEnginePanel(tk.Frame):
    """
    Unified architecture:
    - Frame-based diagnostic panel
    - (parent, app) signature
    - Uses app.theme_manager for styling
    - Uses app.backend for metadata queries
    - Plug-n-play inside DiagnosticsWindow
    """

    def __init__(self, parent, app):
        super().__init__(parent)

        # Unified architecture references
        self.app = app
        self.backend = app.backend
        self.theme = app.theme_manager
        self.window = app.window

        self._build_ui()

    # =========================================================
    # BUILD UI
    # =========================================================
    def _build_ui(self):
        self.configure(bg=self.theme.get("bg"))

        header = tk.Label(
            self,
            text="Metadata Engine",
            bg=self.theme.get("header_bg"),
            fg=self.theme.get("header_fg"),
            font=self.theme.get("font_header")
        )
        header.pack(fill="x", pady=10)

        self.output = tk.Text(
            self,
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("text"),
            font=self.theme.get("font_mono"),
            height=20,
            wrap="word"
        )
        self.output.pack(fill="both", expand=True, padx=10, pady=10)

        ttk.Button(
            self,
            text="Query Metadata",
            command=self._query_metadata
        ).pack(pady=5)

    # =========================================================
    # QUERY METADATA ENGINE
    # =========================================================
    def _query_metadata(self):
        envelope = {
            "task": "metadata",
            "operation": "info",
            "path": "dashboard_test.txt"
        }

        resp = self.backend.send(envelope)

        self.output.insert("end", f"{resp}\n")
        self.output.see("end")
