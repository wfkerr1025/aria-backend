import tkinter as tk
from tkinter import ttk


class ContextSnapshotPanel(tk.Frame):
    """
    Unified architecture:
    - Frame-based panel
    - (parent, app) signature
    - Uses app.backend + app.theme_manager
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
            text="Context Snapshot",
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
            text="Analyze Context",
            command=self._run_context
        ).pack(pady=5)

    # =========================================================
    # RUN CONTEXT ANALYSIS
    # =========================================================
    def _run_context(self):
        envelope = {
            "task": "context",
            "data": {
                "text": "Diagnostics context snapshot",
                "mode": "all"
            }
        }

        resp = self.backend.send_to("/context", envelope)

        self.output.insert("end", f"{resp}\n")
        self.output.see("end")
