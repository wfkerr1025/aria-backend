# windows/copilot_packet_window.py
import tkinter as tk
from tkinter import scrolledtext
import json5 as json
import re
import traceback
import time
import os

from backend.aria_autocomplete import ARIAAutocomplete


class CopilotPacketWindow(tk.Toplevel):
    """
    Unified Copilot Packet Executor for ARIA Lite.
    - Safe-call architecture
    - Theme-aware (if aria_window.theme_manager exists)
    - JSON5 envelope parsing
    - Block literal extraction + restoration
    - Autocomplete engine integration
    - Deterministic backend routing
    """

    def __init__(self, aria_window):
        super().__init__(aria_window.root)

        self.aria_window = aria_window
        self.title("Copilot Packet Executor")
        self.geometry("800x600")

        # Safe-call system
        self.safe_call = self._install_safe_call()
        self._report_error = self._install_error_reporter()

        # Build UI
        self.safe_call("Build UI", self._build_ui)

        # Autocomplete engine
        self.autocomplete = ARIAAutocomplete(aria_window)

        # Bind autocomplete
        self.input_box.bind("<KeyRelease>", self._update_autocomplete)

        # Apply theme if available
        if hasattr(self.aria_window, "theme_manager"):
            self.safe_call("Apply Theme", self._apply_theme)

    # ============================================================
    # SAFE CALL SYSTEM
    # ============================================================
    def _install_safe_call(self):
        def safe_call(label, func):
            try:
                print(f"[CopilotPacketWindow] Executing: {label}")
                start = time.time()
                result = func()
                elapsed = round((time.time() - start) * 1000, 2)
                print(f"[CopilotPacketWindow] Completed: {label} ({elapsed} ms)")
                return result
            except Exception as e:
                self._report_error(label, e)
                return None
        return safe_call

    def _install_error_reporter(self):
        def report_error(label, exception):
            tb = traceback.format_exc()
            print("\n" + "=" * 60)
            print(f"[CopilotPacketWindow ERROR] Action: {label}")
            print(f"[CopilotPacketWindow ERROR] Exception: {exception}")
            print(tb)
            print("=" * 60 + "\n")

            os.makedirs("logs", exist_ok=True)
            with open("logs/copilot_packet_window_errors.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{time.ctime()}] ERROR in '{label}': {exception}\n")
                f.write(tb + "\n")
        return report_error

    # ============================================================
    # BUILD UI
    # ============================================================
    def _build_ui(self):
        # Input label
        self.input_label = tk.Label(
            self,
            text="Copilot Packet (JSON Envelope):",
            font=("Segoe UI", 10, "bold")
        )
        self.input_label.pack(anchor="w", padx=10, pady=(10, 0))

        # Input box
        self.input_box = scrolledtext.ScrolledText(
            self,
            height=15,
            wrap="word"
        )
        self.input_box.pack(fill="both", expand=False, padx=10, pady=10)

        # Suggestion box
        self.suggestion_box = tk.Listbox(self, height=6)
        self.suggestion_box.bind("<<ListboxSelect>>", self._apply_suggestion)
        self.suggestion_box.place_forget()

        # Execute button
        self.execute_button = tk.Button(
            self,
            text="Execute Packet",
            command=lambda: self.safe_call("Execute Packet", self._execute_packet),
            font=("Segoe UI", 10, "bold")
        )
        self.execute_button.pack(fill="x", padx=10, pady=(0, 10))

        # Clear output button
        self.clear_button = tk.Button(
            self,
            text="Clear Output",
            command=lambda: self.safe_call("Clear Output", self.clear_output),
            font=("Segoe UI", 10)
        )
        self.clear_button.pack(fill="x", padx=10, pady=(0, 10))

        # Output label
        self.output_label = tk.Label(
            self,
            text="Output:",
            font=("Segoe UI", 10, "bold")
        )
        self.output_label.pack(anchor="w", padx=10)

        # Output box
        self.output_box = scrolledtext.ScrolledText(
            self,
            height=15,
            wrap="word"
        )
        self.output_box.pack(fill="both", expand=True, padx=10, pady=10)
        self.output_box.configure(state="disabled")

    # ============================================================
    # CLEAR OUTPUT
    # ============================================================
    def clear_output(self):
        self.output_box.configure(state="normal")
        self.output_box.delete("1.0", "end")
        self.output_box.configure(state="disabled")

    # ============================================================
    # BLOCK LITERAL EXTRACTION
    # ============================================================
    def _extract_block_literals(self, raw: str):
        """
        Detects and extracts <<EOF ... EOF blocks inside JSON5 envelopes.
        Replaces them with placeholder keys before JSON parsing.
        Returns (modified_raw, blocks_dict)
        """
        blocks = {}
        pattern = r"<<EOF(.*?)EOF"
        matches = re.finditer(pattern, raw, re.DOTALL)
        index = 0

        for m in matches:
            block_content = m.group(1).lstrip("\n")
            key = f"__BLOCK_LITERAL_{index}__"
            blocks[key] = block_content
            raw = raw.replace(m.group(0), key)
            index += 1

        return raw, blocks

    # ============================================================
    # BLOCK RESTORATION
    # ============================================================
    def _restore_block_literals(self, obj, blocks):
        """
        Recursively restore block literal placeholders anywhere in the envelope.
        """
        if isinstance(obj, dict):
            return {k: self._restore_block_literals(v, blocks) for k, v in obj.items()}

        if isinstance(obj, list):
            return [self._restore_block_literals(v, blocks) for v in obj]

        if isinstance(obj, str) and obj in blocks:
            return blocks[obj]

        return obj

    # ============================================================
    # ENVELOPE NORMALIZATION
    # ============================================================
    def _normalize_envelope(self, envelope: dict) -> dict:
        """
        Normalize envelope into backend router format:
        - ensure 'task'
        - merge payload into top-level
        - legacy file_ops support
        """
        if "task" not in envelope and "action" in envelope:
            envelope["task"] = envelope["action"]

        if isinstance(envelope.get("payload"), dict):
            for k, v in envelope["payload"].items():
                envelope.setdefault(k, v)

        if "task" not in envelope and envelope.get("operation") and envelope.get("path"):
            envelope["task"] = "file_ops"

        return envelope

    # ============================================================
    # EXECUTE PACKET
    # ============================================================
    def _execute_packet(self):
        raw = self.input_box.get("1.0", tk.END)

        if not raw.strip():
            self._write_output("ERROR: No packet provided.")
            return

        # Step 1: Extract block literals
        raw, blocks = self._extract_block_literals(raw)

        # Step 2: Parse JSON5
        try:
            envelope = json.loads(raw)
        except Exception as e:
            self._write_output(f"JSON ERROR: {e}")
            return

        if not isinstance(envelope, dict):
            self._write_output("ERROR: Envelope must be a JSON object.")
            return

        # Step 3: Restore block literals
        envelope = self._restore_block_literals(envelope, blocks)

        # Step 4: Normalize envelope
        envelope = self._normalize_envelope(envelope)

        # Step 5: Execute via ARIA interface
        try:
            result = self.aria_window.interface.execute_packet(envelope)
            try:
                pretty = json.dumps(result, indent=2)
            except Exception:
                pretty = str(result)
            self._write_output(pretty)
        except Exception as e:
            self._write_output(f"EXECUTION ERROR: {e}")

    # ============================================================
    # AUTOCOMPLETE
    # ============================================================
    def _update_autocomplete(self, event=None):
        cursor_index = self.input_box.index("insert")
        line_start = cursor_index.split(".")[0] + ".0"
        current_line = self.input_box.get(line_start, cursor_index).strip()

        if not current_line:
            self.suggestion_box.place_forget()
            return

        suggestions = self.autocomplete.suggest(current_line)

        if not suggestions:
            self.suggestion_box.place_forget()
            return

        self.suggestion_box.delete(0, tk.END)
        for s in suggestions:
            self.suggestion_box.insert(tk.END, s)

        x = self.input_box.winfo_rootx()
        y = self.input_box.winfo_rooty() + self.input_box.winfo_height()
        self.suggestion_box.place(x=x, y=y, width=self.input_box.winfo_width())

    def _apply_suggestion(self, event=None):
        selection = self.suggestion_box.curselection()
        if not selection:
            return

        suggestion = self.suggestion_box.get(selection[0])
        self.input_box.insert("insert", suggestion)
        self.suggestion_box.place_forget()

    # ============================================================
    # WRITE OUTPUT
    # ============================================================
    def _write_output(self, text: str):
        self.output_box.configure(state="normal")
        self.output_box.insert(tk.END, text + "\n")
        self.output_box.configure(state="disabled")

    # ============================================================
    # APPLY THEME (Unified)
    # ============================================================
    def _apply_theme(self):
        theme = self.aria_window.theme_manager.current_theme

        # Window background
        self.configure(bg=theme["bg"])

        # Input label
        self.input_label.configure(bg=theme["bg"], fg=theme["text"])

        # Input box
        self.input_box.configure(bg=theme["panel_bg"], fg=theme["text"])

        # Suggestion box
        self.suggestion_box.configure(bg=theme["panel_bg"], fg=theme["text"])

        # Buttons
        self.execute_button.configure(bg=theme["panel_bg"], fg=theme["text"])
        self.clear_button.configure(bg=theme["panel_bg"], fg=theme["text"])

        # Output label
        self.output_label.configure(bg=theme["bg"], fg=theme["text"])

        # Output box
        self.output_box.configure(bg=theme["panel_bg"], fg=theme["text"])
