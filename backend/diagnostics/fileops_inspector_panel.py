import tkinter as tk
from tkinter import ttk
import json
import time


class FileOpsInspectorPanel(tk.Frame):
    """
    Unified architecture:
    - Frame-based diagnostic panel
    - (parent, app) signature
    - Tracks file_ops packets (write/read/delete/mkdir)
    - Provides replay + inspection + search
    - Plug-n-play inside DiagnosticsWindow
    """

    def __init__(self, parent, app, max_history=40):
        super().__init__(parent)

        # Unified architecture references
        self.app = app
        self.backend = app.backend
        self.theme = app.theme_manager
        self.window = app.window

        self.max_history = max_history
        self.history = []  # list of {packet, result, timestamp}

        self._build_ui()
        self._auto_refresh()

    # =========================================================
    # BUILD UI
    # =========================================================
    def _build_ui(self):
        self.configure(bg=self.theme.get("bg"))

        header = tk.Label(
            self,
            text="FileOps Inspector",
            bg=self.theme.get("header_bg"),
            fg=self.theme.get("header_fg"),
            font=self.theme.get("font_header")
        )
        header.pack(anchor="w", pady=(5, 5), fill="x")

        # Search bar
        search_frame = tk.Frame(self, bg=self.theme.get("bg"))
        search_frame.pack(fill="x", pady=5)

        tk.Label(
            search_frame,
            text="Search:",
            bg=self.theme.get("bg"),
            fg=self.theme.get("text"),
            font=self.theme.get("font_default")
        ).pack(side="left")

        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(search_frame, textvariable=self.search_var)
        search_entry.pack(side="left", fill="x", expand=True, padx=5)
        search_entry.bind("<KeyRelease>", lambda e: self._refresh_list())

        # Listbox
        self.listbox = tk.Listbox(
            self,
            height=10,
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("text"),
            font=self.theme.get("font_default")
        )
        self.listbox.pack(fill="both", expand=True, pady=5)
        self.listbox.bind("<Double-Button-1>", lambda e: self._inspect_selected())

        # Buttons
        btn_frame = tk.Frame(self, bg=self.theme.get("bg"))
        btn_frame.pack(fill="x", pady=5)

        ttk.Button(btn_frame, text="Replay Selected", command=self._replay_selected).pack(side="left")
        ttk.Button(btn_frame, text="Inspect Selected", command=self._inspect_selected).pack(side="left")
        ttk.Button(btn_frame, text="Clear History", command=self._clear_history).pack(side="right")

    # =========================================================
    # PUBLIC API — called by process_envelope()
    # =========================================================
    def add_fileop(self, packet, result):
        """Record file_ops packets only."""
        if packet.get("action") != "file_ops":
            return

        entry = {
            "packet": packet,
            "result": result,
            "timestamp": time.time()
        }

        self.history.append(entry)

        if len(self.history) > self.max_history:
            self.history.pop(0)

        self._refresh_list()

    # =========================================================
    # AUTO REFRESH
    # =========================================================
    def _auto_refresh(self):
        self._refresh_list()
        self.after(1500, self._auto_refresh)

    # =========================================================
    # REFRESH LIST
    # =========================================================
    def _refresh_list(self):
        self.listbox.delete(0, tk.END)
        query = self.search_var.get().lower()

        for entry in self.history:
            packet = entry["packet"]
            ts = time.strftime("%H:%M:%S", time.localtime(entry["timestamp"]))

            op = packet.get("operation", "unknown")
            path = packet.get("path", "")

            label = f"[{ts}] {op.upper()} → {path}"

            if query and query not in label.lower():
                continue

            self.listbox.insert(tk.END, label)

    # =========================================================
    # GET SELECTED ENTRY
    # =========================================================
    def _get_selected_entry(self):
        idx = self.listbox.curselection()
        if not idx:
            return None
        return self.history[idx[0]]

    # =========================================================
    # REPLAY
    # =========================================================
    def _replay_selected(self):
        entry = self._get_selected_entry()
        if not entry:
            return

        packet = entry["packet"]

        # Replay using unified backend pipeline
        result = self.app.process_envelope(packet)

        # Add new entry
        self.add_fileop(packet, result)

        # Open inspector
        self.app.open_packet_inspector(packet, result)

    # =========================================================
    # INSPECT
    # =========================================================
    def _inspect_selected(self):
        entry = self._get_selected_entry()
        if not entry:
            return

        packet = entry["packet"]
        result = entry["result"]

        self.app.open_packet_inspector(packet, result)

    # =========================================================
    # CLEAR
    # =========================================================
    def _clear_history(self):
        self.history.clear()
        self.listbox.delete(0, tk.END)
