import tkinter as tk
from tkinter import ttk
import time


class StressTesterPanel(tk.Frame):
    """
    Unified architecture:
    - Frame-based diagnostic panel
    - (parent, app) signature
    - Uses app.theme_manager for styling
    - Runs repeated backend requests for stress testing
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
            text="Backend Stress Tester",
            bg=self.theme.get("header_bg"),
            fg=self.theme.get("header_fg"),
            font=self.theme.get("font_header")
        )
        header.pack(anchor="w", pady=(5, 5), fill="x")

        # Number of requests
        count_label = tk.Label(
            self,
            text="Requests:",
            bg=self.theme.get("bg"),
            fg=self.theme.get("text"),
            font=self.theme.get("font_default")
        )
        count_label.pack(anchor="w")

        self.count_var = tk.IntVar(value=10)

        count_entry = ttk.Entry(self, textvariable=self.count_var, width=8)
        count_entry.pack(anchor="w")

        # Run button
        ttk.Button(
            self,
            text="Run Stress Test",
            command=self._run
        ).pack(anchor="w", pady=5)

        # Result label
        self.result_label = tk.Label(
            self,
            text="",
            bg=self.theme.get("bg"),
            fg=self.theme.get("text"),
            font=self.theme.get("font_default")
        )
        self.result_label.pack(anchor="w")

    # =========================================================
    # RUN STRESS TEST
    # =========================================================
    def _run(self):
        n = self.count_var.get()
        start = time.time()

        for i in range(n):
            packet = {"action": "ping", "index": i}
            self.app.process_envelope(packet)

        duration = time.time() - start
        self.result_label.configure(
            text=f"Completed {n} requests in {duration:.2f}s"
        )
