# windows/backend_test_panel.py
import tkinter as tk
from tkinter import ttk, scrolledtext
import json5 as json
import traceback
import time
import os

from core.architecture_adapter import verify_window_module, adapt_window_module


class BackendTestPanel(tk.Toplevel):
    """
    Unified Backend Test Panel for ARIA Lite.
    - Safe-call architecture
    - Theme-aware (if aria_window.theme_manager exists)
    - Deterministic routing
    - Scrollable output
    - Plug-n-play standalone window
    """

    def __init__(self, aria_window):
        super().__init__(aria_window.root)

        self.aria_window = aria_window
        self.title("Backend Test Panel")
        self.geometry("900x700")

        # Contract enforcement (window-level)
        self._check_ui_module()

        # Safe-call system
        self.safe_call = self._install_safe_call()
        self._report_error = self._install_error_reporter()

        # Build UI
        self.safe_call("Build UI", self._build_ui)

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
                print(f"[BackendTestPanel] Executing: {label}")
                start = time.time()
                result = func()
                elapsed = round((time.time() - start) * 1000, 2)
                print(f"[BackendTestPanel] Completed: {label} ({elapsed} ms)")
                return result
            except Exception as e:
                self._report_error(label, e)
                return None
        return safe_call

    def _install_error_reporter(self):
        def report_error(label, exception):
            tb = traceback.format_exc()
            print("\n" + "=" * 60)
            print(f"[BackendTestPanel ERROR] Action: {label}")
            print(f"[BackendTestPanel ERROR] Exception: {exception}")
            print(tb)
            print("=" * 60 + "\n")

            os.makedirs("logs", exist_ok=True)
            with open("logs/backend_test_panel_errors.log", "a", encoding="utf-8") as f:
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
            text="Backend Test Panel",
            font=("Segoe UI", 14, "bold")
        )
        self.title_label.pack(anchor="center", pady=10)

        # Builder frame
        builder_frame = tk.Frame(self)
        builder_frame.pack(fill="x", padx=10, pady=10)

        # Task
        tk.Label(builder_frame, text="Task:").grid(row=0, column=0, sticky="w")
        self.task_var = tk.StringVar()
        self.task_dropdown = ttk.Combobox(
            builder_frame,
            textvariable=self.task_var,
            values=[
                "file_ops", "context", "patch", "fs", "metadata",
                "binary", "transaction", "security", "plugin", "debug"
            ],
            state="readonly"
        )
        self.task_dropdown.grid(row=0, column=1, sticky="ew", padx=5)

        # Operation
        tk.Label(builder_frame, text="Operation:").grid(row=1, column=0, sticky="w")
        self.operation_var = tk.StringVar()
        self.operation_entry = ttk.Entry(builder_frame, textvariable=self.operation_var)
        self.operation_entry.grid(row=1, column=1, sticky="ew", padx=5)

        # Path
        tk.Label(builder_frame, text="Path:").grid(row=2, column=0, sticky="w")
        self.path_var = tk.StringVar()
        self.path_entry = ttk.Entry(builder_frame, textvariable=self.path_var)
        self.path_entry.grid(row=2, column=1, sticky="ew", padx=5)

        # Content
        tk.Label(builder_frame, text="Content:").grid(row=3, column=0, sticky="nw")
        self.content_box = scrolledtext.ScrolledText(builder_frame, height=6, wrap="word")
        self.content_box.grid(row=3, column=1, sticky="ew", padx=5)

        builder_frame.grid_columnconfigure(1, weight=1)

        # Execute button
        self.execute_button = tk.Button(
            self,
            text="Execute Envelope",
            command=lambda: self.safe_call("Execute Envelope", self._execute_envelope),
            font=("Segoe UI", 10, "bold")
        )
        self.execute_button.pack(fill="x", padx=10, pady=(0, 10))

        # Presets
        presets_frame = tk.LabelFrame(
            self,
            text="Engine Presets",
            font=("Segoe UI", 10, "bold")
        )
        presets_frame.pack(fill="x", padx=10, pady=10)

        presets = [
            ("Read File", lambda: self._preset_file_ops("read")),
            ("Write File", lambda: self._preset_file_ops("write")),
            ("Delete File", lambda: self._preset_file_ops("delete")),
            ("Context Snapshot", self._preset_context_snapshot),
            ("List Directory", self._preset_list_dir),
            ("File Info", self._preset_file_info),
            ("File Hash", self._preset_file_hash),
            ("Atomic Write", self._preset_atomic_write),
            ("Validate Path", self._preset_validate_path),
            ("List Plugins", self._preset_list_plugins),
            ("Get Logs", self._preset_get_logs),
        ]

        for text, cmd in presets:
            tk.Button(
                presets_frame,
                text=text,
                command=lambda c=cmd: self.safe_call(f"Preset: {text}", c),
                font=("Segoe UI", 9)
            ).pack(fill="x", pady=2)

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
            height=18,
            wrap="word"
        )
        self.output_box.pack(fill="both", expand=True, padx=10, pady=10)
        self.output_box.configure(state="disabled")

        # Clear output
        self.clear_button = tk.Button(
            self,
            text="Clear Output",
            command=lambda: self.safe_call("Clear Output", self._clear_output)
        )
        self.clear_button.pack(fill="x", padx=10, pady=(0, 10))

    # ============================================================
    # PRESET HELPERS
    # ============================================================
    def _preset_file_ops(self, op):
        self.task_var.set("file_ops")
        self.operation_var.set(op)
        self.path_var.set("test.txt")
        self.content_box.delete("1.0", "end")
        if op == "write":
            self.content_box.insert("1.0", "Hello from ARIA Lite!")

    def _preset_context_snapshot(self):
        self.task_var.set("context")
        self.operation_var.set("snapshot")
        self.path_var.set("")
        self.content_box.delete("1.0", "end")

    def _preset_list_dir(self):
        self.task_var.set("fs")
        self.operation_var.set("list")
        self.path_var.set("./")
        self.content_box.delete("1.0", "end")

    def _preset_file_info(self):
        self.task_var.set("metadata")
        self.operation_var.set("info")
        self.path_var.set("test.txt")
        self.content_box.delete("1.0", "end")

    def _preset_file_hash(self):
        self.task_var.set("metadata")
        self.operation_var.set("hash")
        self.path_var.set("test.txt")
        self.content_box.delete("1.0", "end")

    def _preset_atomic_write(self):
        self.task_var.set("transaction")
        self.operation_var.set("atomic_write")
        self.path_var.set("atomic_test.txt")
        self.content_box.delete("1.0", "end")
        self.content_box.insert("1.0", "Atomic write test content.")

    def _preset_validate_path(self):
        self.task_var.set("security")
        self.operation_var.set("validate")
        self.path_var.set("./workspace/test.txt")
        self.content_box.delete("1.0", "end")

    def _preset_list_plugins(self):
        self.task_var.set("plugin")
        self.operation_var.set("list")
        self.path_var.set("")
        self.content_box.delete("1.0", "end")

    def _preset_get_logs(self):
        self.task_var.set("debug")
        self.operation_var.set("get")
        self.path_var.set("")
        self.content_box.delete("1.0", "end")

    # ============================================================
    # EXECUTE ENVELOPE (Unified Routing)
    # ============================================================
    def _execute_envelope(self):
        envelope = {
            "task": self.task_var.get(),
            "operation": self.operation_var.get(),
            "path": self.path_var.get(),
            "content": self.content_box.get("1.0", "end").rstrip(),
            "data": {"text": self.content_box.get("1.0", "end").rstrip()}
        }

        task = envelope["task"]

        try:
            # Unified routing
            if task == "file_ops":
                result = self.aria_window.backend.send_to("/file_ops", envelope)

            elif task == "context":
                result = self.aria_window.backend.send_to("/context", envelope)

            elif task == "debug":
                result = self.aria_window.backend.send_to("/debug/log", envelope)

            elif task == "patch":
                result = self.aria_window.backend.send(envelope)

            elif task in ("fs", "metadata", "binary", "transaction", "security", "plugin"):
                result = self.aria_window.backend.send(envelope)

            else:
                result = {"status": "error", "message": f"Unknown task '{task}'"}

            pretty = json.dumps(result, indent=2)
            self._write_output(pretty)

        except Exception as e:
            self._write_output(f"EXECUTION ERROR: {e}")

    # ============================================================
    # OUTPUT HELPERS
    # ============================================================
    def _write_output(self, text):
        self.output_box.configure(state="normal")
        self.output_box.insert("end", text + "\n")
        self.output_box.configure(state="disabled")

    def _clear_output(self):
        self.output_box.configure(state="normal")
        self.output_box.delete("1.0", "end")
        self.output_box.configure(state="disabled")

    # ============================================================
    # APPLY THEME (Unified)
    # ============================================================
    def _apply_theme(self):
        theme = self.aria_window.theme_manager.current_theme

        # Window background
        self.configure(bg=theme["bg"])

        # Title
        self.title_label.configure(bg=theme["bg"], fg=theme["text"])

        # Builder frame + children
        for child in self.winfo_children():
            try:
                child.configure(bg=theme["bg"], fg=theme["text"])
            except:
                pass

        # Output box
        self.output_box.configure(
            bg=theme["panel_bg"],
            fg=theme["text"]
        )
