# windows/packet_inspector_window.py
import tkinter as tk
from tkinter import scrolledtext
import json
import traceback
import time
import os

from core.architecture_adapter import verify_window_module, adapt_window_module


class PacketInspectorWindow(tk.Toplevel):
    """
    Unified ARIA Packet Inspector.
    - Safe-call architecture
    - Theme-aware (if aria_window.theme_manager exists)
    - Shows packet, result, context snapshot
    - Allows re-running the packet through backend router
    """

    def __init__(self, aria_window, packet, result):
        super().__init__(aria_window.root)

        self.aria_window = aria_window
        self.packet = packet
        self.result = result

        self.title("ARIA Packet Inspector")
        self.geometry("900x700")

        # Contract enforcement (window-level)
        self._check_ui_module()

        # Safe-call system
        self.safe_call = self._install_safe_call()
        self._report_error = self._install_error_reporter()

        # Build UI
        self.safe_call("Build UI", self._build_ui)

        # Render initial content
        self.safe_call("Render Packet", self._render)

        # Apply theme if available
        if hasattr(self.aria_window, "theme_manager"):
            self.safe_call("Apply Theme", self._apply_theme)

    # ============================================================
    # CONTRACT ENFORCEMENT
    # ============================================================
    def _check_ui_module(self):
        """
        Enforce ARIA Lite window-level contract on this backend window.
        Mirrors ARIAWindow + DiagnosticsWindow behavior.
        """
        window = self.aria_window
        name = self.__class__.__name__

        if not verify_window_module(window, self, name):
            adapt_window_module(window, self, name)

    # ============================================================
    # SAFE CALL SYSTEM
    # ============================================================
    def _install_safe_call(self):
        def safe_call(label, func):
            try:
                print(f"[PacketInspectorWindow] Executing: {label}")
                start = time.time()
                result = func()
                elapsed = round((time.time() - start) * 1000, 2)
                print(f"[PacketInspectorWindow] Completed: {label} ({elapsed} ms)")
                return result
            except Exception as e:
                self._report_error(label, e)
                return None
        return safe_call

    def _install_error_reporter(self):
        def report_error(label, exception):
            tb = traceback.format_exc()
            print("\n" + "=" * 60)
            print(f"[PacketInspectorWindow ERROR] Action: {label}")
            print(f"[PacketInspectorWindow ERROR] Exception: {exception}")
            print(tb)
            print("=" * 60 + "\n")

            os.makedirs("logs", exist_ok=True)
            with open("logs/packet_inspector_errors.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{time.ctime()}] ERROR in '{label}': {exception}\n")
                f.write(tb + "\n")
        return report_error

    # ============================================================
    # BUILD UI
    # ============================================================
    def _build_ui(self):
        # Title
        self.title_label = tk.Label(
            self,
            text="ARIA Packet Inspector",
            font=("Segoe UI", 12, "bold")
        )
        self.title_label.pack(anchor="w", padx=10, pady=(10, 5))

        # Action label
        action = self.packet.get("action", "unknown")
        self.action_label = tk.Label(
            self,
            text=f"Action: {action}",
            font=("Segoe UI", 10)
        )
        self.action_label.pack(anchor="w", padx=10, pady=(0, 10))

        # Re-run button
        self.rerun_button = tk.Button(
            self,
            text="Re-run Inspection",
            command=lambda: self.safe_call("Re-run Packet", self._rerun_inspection),
            font=("Segoe UI", 10, "bold")
        )
        self.rerun_button.pack(fill="x", padx=10, pady=(0, 10))

        # Log box
        self.log_box = scrolledtext.ScrolledText(
            self,
            wrap="word"
        )
        self.log_box.pack(fill="both", expand=True, padx=10, pady=10)

    # ============================================================
    # RENDER CONTENT
    # ============================================================
    def _render(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", tk.END)

        # Packet
        self.log_box.insert(tk.END, "=== Packet ===\n")
        self.log_box.insert(tk.END, json.dumps(self.packet, indent=2))
        self.log_box.insert(tk.END, "\n\n")

        # Result
        self.log_box.insert(tk.END, "=== Result ===\n")
        self.log_box.insert(tk.END, json.dumps(self.result, indent=2))
        self.log_box.insert(tk.END, "\n\n")

        # Context Snapshot
        context_snapshot = self.aria_window.context_manager.get_context_snapshot()
        self.log_box.insert(tk.END, "=== Context Snapshot ===\n")

        context_id = context_snapshot.get("context", None)
        turn = context_snapshot.get("turn", None)

        self.log_box.insert(tk.END, f"Context ID: {context_id}\n")
        self.log_box.insert(tk.END, f"Turn: {turn}\n\n")

        entities = context_snapshot.get("entities", {})
        if entities:
            self.log_box.insert(tk.END, "Entities:\n")
            for ent_id, ent_data in entities.items():
                self.log_box.insert(tk.END, f"- {ent_id}: {ent_data}\n")
        else:
            self.log_box.insert(tk.END, "No entities detected.\n")

        self.log_box.insert(tk.END, "\n")
        self.log_box.configure(state="disabled")
        self.log_box.see(tk.END)

    # ============================================================
    # RE-RUN PACKET THROUGH ROUTER
    # ============================================================
    def _rerun_inspection(self):
        try:
            new_result = self.aria_window.process_envelope(self.packet)
            self.result = new_result
            self.safe_call("Render Packet", self._render)
        except Exception as e:
            self.log_box.configure(state="normal")
            self.log_box.insert(tk.END, f"\n\nERROR re-running packet: {e}")
            self.log_box.configure(state="disabled")
            self.log_box.see(tk.END)

    # ============================================================
    # APPLY THEME (Unified)
    # ============================================================
    def _apply_theme(self):
        theme = self.aria_window.theme_manager.current_theme

        # Window background
        self.configure(bg=theme["bg"])

        # Title + action label
        self.title_label.configure(bg=theme["bg"], fg=theme["text"])
        self.action_label.configure(bg=theme["bg"], fg=theme["text"])

        # Buttons
        self.rerun_button.configure(bg=theme["panel_bg"], fg=theme["text"])

        # Log box
        self.log_box.configure(bg=theme["panel_bg"], fg=theme["text"])
