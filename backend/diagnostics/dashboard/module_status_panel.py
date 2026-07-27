import tkinter as tk
from tkinter import ttk


class ModuleStatusPanel(tk.Frame):
    """
    Unified architecture:
    - Frame-based diagnostic panel
    - (parent, app) signature
    - Uses app.theme_manager for styling
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
            text="Module Status",
            bg=self.theme.get("header_bg"),
            fg=self.theme.get("header_fg"),
            font=self.theme.get("font_header")
        )
        header.pack(fill="x", pady=10)

        self.tree = ttk.Treeview(
            self,
            columns=("Module", "Status"),
            show="headings",
            height=12
        )

        self.tree.heading("Module", text="Module")
        self.tree.heading("Status", text="Status")

        self.tree.column("Module", width=200, anchor="w")
        self.tree.column("Status", width=150, anchor="w")

        self.tree.pack(fill="both", expand=True, padx=10, pady=10)

        self._populate()

    # =========================================================
    # POPULATE MODULE STATUS
    # =========================================================
    def _populate(self):
        modules = [
            ("Router", "OK"),
            ("FileOps", "OK"),
            ("Metadata", "OK"),
            ("Security", "OK"),
            ("Context", "OK"),
            ("PatchEngine", "OK"),
        ]

        for name, status in modules:
            self.tree.insert("", "end", values=(name, status))
