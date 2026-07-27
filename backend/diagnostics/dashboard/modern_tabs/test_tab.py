import tkinter as tk
from tkinter import ttk


class TestTab(tk.Frame):
    """
    Backend Test Suite Tab for ARIA Lite.
    - Runs individual backend test routes
    - Provides a Run ALL Tests button
    - Displays structured output from backend
    """

    def __init__(self, parent, app, theme):
        super().__init__(parent, bg=theme.get("bg"))
        self.app = app
        self.backend = app.backend
        self.theme = theme

        self._build()

    # ============================================================
    # BUILD UI
    # ============================================================
    def _build(self):
        # Title
        title = tk.Label(
            self,
            text="ARIA Backend Test Suite",
            font=("Segoe UI", 14, "bold"),
            bg=self.theme.get("bg"),
            fg=self.theme.get("text"),
        )
        title.pack(pady=10)

        # Output console
        self.output_box = tk.Text(self, height=20, wrap="word")
        self.output_box.pack(fill="both", expand=True, padx=10, pady=10)

        # Buttons
        button_frame = tk.Frame(self, bg=self.theme.get("bg"))
        button_frame.pack(pady=10)

        tests = [
            ("Workspace Tests", "test_workspace"),
            ("Sandbox Tests", "test_sandbox"),
            ("Router Tests", "test_router"),
            ("Contract Tests", "test_contract"),
            ("Registry Tests", "test_registry"),
            ("Toolchain Tests", "test_toolchain"),
            ("Run ALL Tests", "run_python_tests"),
        ]

        for label, task in tests:
            btn = tk.Button(
                button_frame,
                text=label,
                command=lambda t=task: self._run_test(t),
                bg=self.theme.get("button_bg"),
                fg=self.theme.get("button_text"),
                font=("Segoe UI", 11),
                relief="raised",
                padx=10,
                pady=5,
            )
            btn.pack(fill="x", padx=5, pady=3)

    # ============================================================
    # BACKEND TEST EXECUTION
    # ============================================================
    def _run_test(self, task_name: str):
        try:
            result = self.backend.send({"task": task_name})
        except Exception as e:
            result = {
                "status": "error",
                "operation": "backend_send",
                "detail": str(e),
            }

        self._render_result(task_name, result)

    # ============================================================
    # RENDER RESULTS
    # ============================================================
    def _render_result(self, task_name: str, result: dict):
        self.output_box.delete("1.0", tk.END)

        self.output_box.insert(tk.END, f"Task: {task_name}\n")
        self.output_box.insert(tk.END, f"Status: {result.get('status')}\n")
        self.output_box.insert(tk.END, f"Operation: {result.get('operation')}\n\n")

        if "file" in result:
            self.output_box.insert(tk.END, f"File: {result.get('file')}\n\n")

        if "output" in result and result["output"]:
            self.output_box.insert(tk.END, "Output:\n")
            self.output_box.insert(tk.END, result["output"])
            self.output_box.insert(tk.END, "\n\n")

        if "errors" in result and result["errors"]:
            self.output_box.insert(tk.END, "Errors:\n")
            self.output_box.insert(tk.END, result["errors"])
            self.output_box.insert(tk.END, "\n\n")

        if "returncode" in result:
            self.output_box.insert(
                tk.END,
                f"Return code: {result.get('returncode')}\n"
            )
