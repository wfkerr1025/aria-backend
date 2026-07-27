# packet_inspector_v2.py
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import json5 as json
import traceback
import os
import time


class PacketInspectorV2(tk.Toplevel):
    """
    Unified architecture:
    - Standalone Toplevel window
    - Receives (app)
    - Uses app.last_packet + app.last_packet_result
    - Uses app.theme_manager for styling
    - Plug-n-play with BackendToolsTab and ARIAWindow
    """

    def __init__(self, app):
        super().__init__(app.root)

        # Unified architecture references
        self.app = app
        self.backend = app.backend
        self.theme = app.theme_manager
        self.window = app.window

        # Install safe_call + error reporter
        self.safe_call = self._install_safe_call()
        self._report_error = self._install_error_reporter()

        # Load packet safely
        self.packet = self.safe_call(
            "Load Last Packet",
            lambda: app.last_packet or {}
        )
        self.result = self.safe_call(
            "Load Last Packet Result",
            lambda: app.last_packet_result or {}
        )

        # Build UI safely
        self.safe_call("Build PacketInspectorV2 UI", self._build_ui)

    # =========================================================
    # SAFE CALL SYSTEM
    # =========================================================
    def _install_safe_call(self):
        def safe_call(label, func):
            try:
                print(f"[PacketInspectorV2] Executing: {label}")
                start = time.time()
                result = func()
                elapsed = round((time.time() - start) * 1000, 2)
                print(f"[PacketInspectorV2] Completed: {label} ({elapsed} ms)")
                return result
            except Exception as e:
                self._report_error(label, e)
                return None
        return safe_call

    def _install_error_reporter(self):
        def report_error(label, exception):
            tb = traceback.format_exc()

            print("\n" + "=" * 60)
            print(f"[PacketInspectorV2 ERROR] Action: {label}")
            print(f"[PacketInspectorV2 ERROR] Exception: {exception}")
            print(tb)
            print("=" * 60 + "\n")

            os.makedirs("logs", exist_ok=True)
            with open("logs/packet_inspector_v2_errors.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{time.ctime()}] ERROR in '{label}': {exception}\n")
                f.write(tb + "\n")

            try:
                messagebox.showerror(
                    "Packet Inspector v2 Error",
                    f"An error occurred while running '{label}'.\n\n{exception}"
                )
            except Exception:
                pass

        return report_error

    # =========================================================
    # BUILD UI
    # =========================================================
    def _build_ui(self):
        self.title("Packet Inspector v2")
        self.geometry("800x600")

        # Apply theme background
        self.configure(bg=self.theme.get("bg"))

        header = tk.Label(
            self,
            text="Packet Inspector v2",
            bg=self.theme.get("header_bg"),
            fg=self.theme.get("header_fg"),
            font=self.theme.get("font_header")
        )
        header.pack(pady=10, fill="x")

        # Text area
        self.text = scrolledtext.ScrolledText(
            self,
            height=30,
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("text"),
            font=self.theme.get("font_mono"),
            wrap="word"
        )
        self.text.pack(fill="both", expand=True, padx=10, pady=10)
        self.text.configure(state="normal")

        # Format packet + result safely
        try:
            formatted_packet = json.dumps(self.packet, indent=2)
        except Exception as e:
            self._report_error("Format Packet JSON", e)
            formatted_packet = "{\n  \"error\": \"Invalid packet JSON\"\n}"

        try:
            formatted_result = json.dumps(self.result, indent=2)
        except Exception as e:
            self._report_error("Format Result JSON", e)
            formatted_result = "{\n  \"error\": \"Invalid result JSON\"\n}"

        # Insert content
        self.text.insert("end", "=== Packet ===\n")
        self.text.insert("end", formatted_packet)
        self.text.insert("end", "\n\n=== Result ===\n")
        self.text.insert("end", formatted_result)
        self.text.configure(state="disabled")

        ttk.Button(self, text="Close", command=self.destroy).pack(pady=10)
