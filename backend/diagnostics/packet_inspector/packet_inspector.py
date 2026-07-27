# packet_inspector.py
import tkinter as tk
from tkinter import ttk, messagebox
import json
import traceback
import os
import time


class PacketInspector(tk.Frame):
    """
    Unified architecture:
    - Frame-based inspector
    - (parent, app) signature
    - Uses app.theme_manager for styling
    - Uses app.last_packet + app.last_packet_result
    - Plug-n-play inside DiagnosticsWindow or ARIAWindow
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
        self.safe_call("Build PacketInspector UI", self._build_ui)

    # =========================================================
    # SAFE CALL SYSTEM
    # =========================================================
    def _install_safe_call(self):
        def safe_call(label, func):
            try:
                print(f"[PacketInspector] Executing: {label}")
                start = time.time()
                result = func()
                elapsed = round((time.time() - start) * 1000, 2)
                print(f"[PacketInspector] Completed: {label} ({elapsed} ms)")
                return result
            except Exception as e:
                self._report_error(label, e)
                return None
        return safe_call

    def _install_error_reporter(self):
        def report_error(label, exception):
            tb = traceback.format_exc()

            print("\n" + "=" * 60)
            print(f"[PacketInspector ERROR] Action: {label}")
            print(f"[PacketInspector ERROR] Exception: {exception}")
            print(tb)
            print("=" * 60 + "\n")

            os.makedirs("logs", exist_ok=True)
            with open("logs/packet_inspector_errors.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{time.ctime()}] ERROR in '{label}': {exception}\n")
                f.write(tb + "\n")

            try:
                messagebox.showerror(
                    "Packet Inspector Error",
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

        # Main panel wrapper
        main_panel = tk.Frame(self, bg=self.theme.get("panel_bg"))
        main_panel.pack(fill="both", expand=True, padx=10, pady=10)

        # ---------------------------------------------------------
        # Last Packet Section
        # ---------------------------------------------------------
        packet_label = tk.Label(
            main_panel,
            text="Last Packet",
            bg=self.theme.get("header_bg"),
            fg=self.theme.get("header_fg"),
            font=self.theme.get("font_header")
        )
        packet_label.pack(anchor="w", pady=(0, 5))

        self.packet_text = tk.Text(
            main_panel,
            height=10,
            width=80,
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("text"),
            font=self.theme.get("font_mono"),
            wrap="word"
        )
        self.packet_text.pack(fill="x", pady=5)

        # Insert packet JSON safely
        try:
            packet_json = json.dumps(self.app.last_packet, indent=2)
        except Exception as e:
            self._report_error("Render Last Packet JSON", e)
            packet_json = "{\n  \"error\": \"Invalid packet JSON\" \n}"

        self.packet_text.insert("1.0", packet_json)

        # ---------------------------------------------------------
        # Result Section
        # ---------------------------------------------------------
        result_label = tk.Label(
            main_panel,
            text="Result",
            bg=self.theme.get("header_bg"),
            fg=self.theme.get("header_fg"),
            font=self.theme.get("font_header")
        )
        result_label.pack(anchor="w", pady=(10, 5))

        self.result_text = tk.Text(
            main_panel,
            height=10,
            width=80,
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("text"),
            font=self.theme.get("font_mono"),
            wrap="word"
        )
        self.result_text.pack(fill="x", pady=5)

        # Insert result JSON safely
        try:
            result_json = json.dumps(self.app.last_packet_result, indent=2)
        except Exception as e:
            self._report_error("Render Last Packet Result JSON", e)
            result_json = "{\n  \"error\": \"Invalid result JSON\" \n}"

        self.result_text.insert("1.0", result_json)

        # ---------------------------------------------------------
        # Replay Button
        # ---------------------------------------------------------
        ttk.Button(
            main_panel,
            text="Replay Packet",
            command=lambda: self.safe_call("Replay Packet", self._replay)
        ).pack(anchor="e", pady=5)

    # =========================================================
    # REPLAY PACKET
    # =========================================================
    def _replay(self):
        packet = self.app.last_packet

        if not packet:
            self.app.chat_panel.add_aria_message("No packet available to replay.")
            return

        try:
            result = self.app.process_envelope(packet)
        except Exception as e:
            self._report_error("Replay Packet Backend Execution", e)
            self.app.chat_panel.add_aria_message(f"Replay error: {e}")
            return

        try:
            self.app.open_packet_inspector(packet, result)
        except Exception as e:
            self._report_error("Open Packet Inspector After Replay", e)
            self.app.chat_panel.add_aria_message(f"Inspector error: {e}")
