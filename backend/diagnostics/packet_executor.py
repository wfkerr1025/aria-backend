# packet_executor.py
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import json
import traceback
import os
import time

from backend.diagnostics.packet_inspector.packet_syntax_highlighter import highlight_json
from utils.file_utils import ensure_directory


class PacketExecutor(tk.Frame):
    """
    Unified architecture:
    - Frame-based diagnostic panel
    - (parent, app) signature
    - JSON packet editor + execution + inspector
    """

    def __init__(self, parent, app):
        super().__init__(parent)

        # Unified architecture references
        self.app = app
        self.backend = app.backend
        self.theme = app.theme_manager
        self.window = app.window

        # Install safe_call + error reporter
        self.safe_call = self._install_safe_call()
        self._report_error = self._install_error_reporter()

        # Build UI safely
        self.safe_call("Build PacketExecutor UI", self._build_ui)

    # =========================================================
    # SAFE CALL SYSTEM
    # =========================================================
    def _install_safe_call(self):
        def safe_call(label, func):
            try:
                print(f"[PacketExecutor] Executing: {label}")
                start = time.time()
                result = func()
                elapsed = round((time.time() - start) * 1000, 2)
                print(f"[PacketExecutor] Completed: {label} ({elapsed} ms)")
                return result
            except Exception as e:
                self._report_error(label, e)
                return None
        return safe_call

    def _install_error_reporter(self):
        def report_error(label, exception):
            tb = traceback.format_exc()

            print("\n" + "=" * 60)
            print(f"[PacketExecutor ERROR] Action: {label}")
            print(f"[PacketExecutor ERROR] Exception: {exception}")
            print(tb)
            print("=" * 60 + "\n")

            os.makedirs("logs", exist_ok=True)
            with open("logs/packet_executor_errors.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{time.ctime()}] ERROR in '{label}': {exception}\n")
                f.write(tb + "\n")

            try:
                messagebox.showerror(
                    "Packet Executor Error",
                    f"An error occurred while running '{label}'.\n\n{exception}"
                )
            except Exception:
                pass

        return report_error

    # =========================================================
    # BUILD UI
    # =========================================================
    def _build_ui(self):
        self.configure(bg=self.theme.get("bg"))

        main_panel = tk.Frame(self, bg=self.theme.get("panel_bg"))
        main_panel.pack(fill="both", expand=True, padx=10, pady=10)

        # Header
        header = tk.Label(
            main_panel,
            text="Build Packet",
            bg=self.theme.get("header_bg"),
            fg=self.theme.get("header_fg"),
            font=self.theme.get("font_header")
        )
        header.pack(anchor="w", pady=(0, 10))

        # Packet Editor Section
        editor_section = tk.Frame(main_panel, bg=self.theme.get("panel_bg"))
        editor_section.pack(fill="x", pady=(0, 10))

        self.text = tk.Text(
            editor_section,
            height=10,
            width=80,
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("text"),
            font=self.theme.get("font_mono")
        )
        self.text.pack(fill="x", pady=5)
        self.text.bind("<KeyRelease>", lambda e: highlight_json(self.text))

        # Buttons Section
        btn_section = tk.Frame(main_panel, bg=self.theme.get("panel_bg"))
        btn_section.pack(fill="x", pady=(0, 10))

        ttk.Button(
            btn_section,
            text="Browse Path",
            command=lambda: self.safe_call("Browse Path", self._browse)
        ).pack(side="left")

        ttk.Button(
            btn_section,
            text="Execute Packet",
            command=lambda: self.safe_call("Execute Packet", self._execute)
        ).pack(side="right")

    # =========================================================
    # Browse for file path
    # =========================================================
    def _browse(self):
        path = filedialog.askopenfilename()
        if not path:
            return

        raw = self.text.get("1.0", tk.END)

        try:
            packet = json.loads(raw)
        except Exception as e:
            self.app.chat_panel.add_aria_message(f"Invalid JSON before browse: {e}")
            return

        packet["path"] = path

        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", json.dumps(packet, indent=2))

        highlight_json(self.text)

    # =========================================================
    # Execute packet
    # =========================================================
    def _execute(self):
        raw = self.text.get("1.0", tk.END)

        # Parse JSON
        try:
            packet = json.loads(raw)
        except Exception as e:
            self.app.chat_panel.add_aria_message(f"Invalid JSON: {e}")
            return

        # Auto-mkdir
        if "path" in packet:
            try:
                ensure_directory(packet["path"])
            except Exception as e:
                self.app.chat_panel.add_aria_message(f"Directory error: {e}")
                return

        # Backend execution
        try:
            result = self.app.process_envelope(packet)
        except Exception as e:
            self._report_error("Backend Envelope Execution", e)
            self.app.chat_panel.add_aria_message(f"Backend error: {e}")
            return

        # Store last packet
        self.app.last_packet = packet
        self.app.last_packet_result = result

        # Open inspector
        try:
            self.app.open_packet_inspector(packet, result)
        except Exception as e:
            self._report_error("Open Packet Inspector", e)
            self.app.chat_panel.add_aria_message(f"Inspector error: {e}")
