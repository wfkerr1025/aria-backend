# backend/diagnostics/dashboard/modern_tabs/tools_tab.py
import tkinter as tk
from tkinter import ttk

from backend.diagnostics.dashboard.backend_test_panel import BackendTestPanel
from backend.diagnostics.dashboard.backend_stress_tester import BackendStressTester
from backend.diagnostics.dashboard.packet_inspector_v2 import PacketInspectorV2


class ToolsTab(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)

        self.app = app
        self.theme = app.theme_manager

        self.configure(bg=self.theme.get("bg"))
        self._build_ui()

    def _build_ui(self):
        title = tk.Label(
            self,
            text="Diagnostics Tools",
            font=("Segoe UI", 16, "bold"),
            bg=self.theme.get("bg"),
            fg=self.theme.get("text")
        )
        title.pack(pady=10)

        btn = lambda text, func: tk.Button(
            self,
            text=text,
            command=func,
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("text"),
            width=25
        )

        btn("Backend Test Panel", lambda: BackendTestPanel(self.app)).pack(pady=5)
        btn("Backend Stress Tester", lambda: BackendStressTester(self.app)).pack(pady=5)
        btn("Packet Inspector", lambda: PacketInspectorV2(self.app)).pack(pady=5)
