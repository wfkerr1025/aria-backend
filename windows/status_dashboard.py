# windows/status_dashboard.py
import tkinter as tk
from tkinter import ttk, scrolledtext
import json
import time

from core.architecture_adapter import verify_window_module, adapt_window_module


class StatusDashboardWindow(tk.Toplevel):
    """
    Unified Status Dashboard for ARIA Lite.
    Displays:
    - Module load status
    - Plugin load status
    - Backend events
    - Diagnostics events
    - Performance metrics
    """

    REFRESH_INTERVAL = 1500  # ms

    def __init__(self, app):
        super().__init__(app.root)

        self.app = app
        self.registry = app.status_registry
        self.theme = app.theme_manager

        self.title("ARIA Status Dashboard")
        self.geometry("900x600")
        self.configure(bg=self.theme.get("bg"))

        # Contract enforcement (window-level)
        self._check_ui_module()

        self.safe_call = self._install_safe_call()
        self._report_error = self._install_error_reporter()

        self.safe_call("Build Dashboard UI", self._build_ui)
        self.safe_call("Bind Shortcuts", self._bind_shortcuts)
        self.safe_call("Start Refresh Loop", self._refresh)

    # ============================================================
    # CONTRACT ENFORCEMENT
    # ============================================================
    def _check_ui_module(self):
        """
        Enforce ARIA Lite window-level contract on this dashboard window.
        Mirrors ARIAWindow + DiagnosticsWindow behavior.
        """
        window = self.app.window
        name = self.__class__.__name__

        if not verify_window_module(window, self, name):
            adapt_window_module(window, self, name)

    # ============================================================
    # SAFE CALL SYSTEM
    # ============================================================
    def _install_safe_call(self):
        def safe_call(label, func):
            try:
                print(f"[StatusDashboard] Executing: {label}")
                start = time.time()
                result = func()
                elapsed = round((time.time() - start) * 1000, 2)
                print(f"[StatusDashboard] Completed: {label} ({elapsed} ms)")
                return result
            except Exception as e:
                self._report_error(label, e)
                return None
        return safe_call

    def _install_error_reporter(self):
        def report_error(label, exception):
            import traceback
            tb = traceback.format_exc()

            print("\n" + "=" * 60)
            print(f"[StatusDashboard ERROR] Action: {label}")
            print(f"[StatusDashboard ERROR] Exception: {exception}")
            print(tb)
            print("=" * 60 + "\n")
        return report_error

    # ============================================================
    # BUILD UI
    # ============================================================
    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # Header
        header = tk.Label(
            self,
            text="Unified Status Dashboard",
            bg=self.theme.get("header_bg"),
            fg=self.theme.get("header_fg"),
            font=self.theme.get("font_header")
        )
        header.grid(row=0, column=0, sticky="ew", pady=10)

        # Main container
        self.container = tk.Frame(self, bg=self.theme.get("bg"))
        self.container.grid(row=1, column=0, sticky="nsew")
        self.container.columnconfigure(0, weight=1)
        self.container.rowconfigure(0, weight=1)

        # Scrollable panel
        self.canvas = tk.Canvas(self.container, bg=self.theme.get("bg"), highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self.container, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.grid(row=0, column=0, sticky="nsew")

        self.inner = tk.Frame(self.canvas, bg=self.theme.get("bg"))
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        # Sections
        self.sections = {}

        self._add_section("Modules", "modules")
        self._add_section("Plugins", "plugins")
        self._add_section("Backend Events", "backend")
        self._add_section("Diagnostics", "diagnostics")
        self._add_section("Performance Metrics", "performance")

    def _add_section(self, title, key):
        frame = tk.Frame(self.inner, bg=self.theme.get("panel_bg"), bd=1, relief="solid")
        frame.pack(fill="x", padx=10, pady=5)

        header = tk.Label(
            frame,
            text=title,
            bg=self.theme.get("header_bg"),
            fg=self.theme.get("header_fg"),
            font=self.theme.get("font_subheader"),
            anchor="w"
        )
        header.pack(fill="x")

        content = tk.Frame(frame, bg=self.theme.get("panel_bg"))
        content.pack(fill="x", padx=5, pady=5)

        self.sections[key] = content

    # ============================================================
    # SHORTCUTS
    # ============================================================
    def _bind_shortcuts(self):
        self.bind("<Control-r>", lambda e: self._refresh(force=True))
        self.bind("<Control-R>", lambda e: self._refresh(force=True))

    # ============================================================
    # REFRESH LOOP
    # ============================================================
    def _refresh(self, force=False):
        status = self.registry.get_status()

        for key, frame in self.sections.items():
            for widget in frame.winfo_children():
                widget.destroy()

            data = status.get(key, {})

            if not data:
                tk.Label(
                    frame,
                    text="No data available.",
                    bg=self.theme.get("panel_bg"),
                    fg=self.theme.get("text")
                ).pack(anchor="w")
                continue

            for name, entry in data.items():
                self._render_entry(frame, name, entry)

        if not force:
            self.after(self.REFRESH_INTERVAL, self._refresh)

    # ============================================================
    # ENTRY RENDERING
    # ============================================================
    def _render_entry(self, parent, name, entry):
        row = tk.Frame(parent, bg=self.theme.get("bg"), bd=1, relief="solid")
        row.pack(fill="x", pady=2)

        # Title
        title = tk.Label(
            row,
            text=name,
            bg=self.theme.get("bg"),
            fg=self.theme.get("text"),
            font=self.theme.get("font_mono_bold"),
            anchor="w"
        )
        title.pack(fill="x", padx=5, pady=2)

        # Status line
        status_text = json.dumps(entry, indent=2)

        text_box = scrolledtext.ScrolledText(
            row,
            height=6,
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("text"),
            wrap="word"
        )
        text_box.insert("1.0", status_text)
        text_box.configure(state="disabled")
        text_box.pack(fill="x", padx=5, pady=5)
