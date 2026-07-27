# backend_stress_tester.py
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import time
import threading
import traceback
import os


class BackendStressTester(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)

        # Unified architecture references
        self.app = app
        self.backend = app.backend
        self.theme = app.theme_manager
        self.window = app.window

        self.running = False

        # Install safe_call + error reporter
        self.safe_call = self._install_safe_call()
        self._report_error = self._install_error_reporter()

        # Build UI safely
        self.safe_call("Build BackendStressTester UI", self._build_ui)

    # =========================================================
    # SAFE CALL SYSTEM
    # =========================================================
    def _install_safe_call(self):
        def safe_call(label, func):
            try:
                print(f"[BackendStressTester] Executing: {label}")
                start = time.time()
                result = func()
                elapsed = round((time.time() - start) * 1000, 2)
                print(f"[BackendStressTester] Completed: {label} ({elapsed} ms)")
                return result
            except Exception as e:
                self._report_error(label, e)
                return None
        return safe_call

    def _install_error_reporter(self):
        def report_error(label, exception):
            tb = traceback.format_exc()
            print("\n" + "=" * 60)
            print(f"[BackendStressTester ERROR] Action: {label}")
            print(f"[BackendStressTester ERROR] Exception: {exception}")
            print(tb)
            print("=" * 60 + "\n")

            os.makedirs("logs", exist_ok=True)
            with open("logs/backend_stress_tester_errors.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{time.ctime()}] ERROR in '{label}': {exception}\n")
                f.write(tb + "\n")

            try:
                messagebox.showerror(
                    "Backend Stress Tester Error",
                    f"An error occurred while running '{label}'.\n\n{exception}"
                )
            except Exception:
                pass

        return report_error

    # =========================================================
    # BUILD UI
    # =========================================================
    def _build_ui(self):
        self.title("Backend Stress Tester")
        self.geometry("700x500")

        # Apply theme background
        self.configure(bg=self.theme.get("bg"))

        header = tk.Label(
            self,
            text="Backend Stress Tester",
            bg=self.theme.get("header_bg"),
            fg=self.theme.get("header_fg"),
            font=self.theme.get("font_header")
        )
        header.pack(pady=10, fill="x")

        # Log window
        self.log = scrolledtext.ScrolledText(
            self,
            height=20,
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("text"),
            font=self.theme.get("font_mono")
        )
        self.log.pack(fill="both", expand=True, padx=10, pady=10)
        self.log.configure(state="disabled")

        # Buttons
        btn_frame = tk.Frame(self, bg=self.theme.get("bg"))
        btn_frame.pack(fill="x", pady=10)

        ttk.Button(
            btn_frame,
            text="Start Stress Test",
            command=lambda: self.safe_call("Start Stress Test", self.start_test)
        ).pack(side="left", padx=10)

        ttk.Button(
            btn_frame,
            text="Stop",
            command=lambda: self.safe_call("Stop Stress Test", self.stop_test)
        ).pack(side="left", padx=10)

        ttk.Button(
            btn_frame,
            text="Close",
            command=self.destroy
        ).pack(side="right", padx=10)

    # =========================================================
    # LOGGING
    # =========================================================
    def log_msg(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.configure(state="disabled")
        self.log.see("end")

    # =========================================================
    # CONTROL
    # =========================================================
    def start_test(self):
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._run_test, daemon=True).start()

    def stop_test(self):
        self.running = False

    # =========================================================
    # MAIN TEST LOOP
    # =========================================================
    def _run_test(self):
        count = 0
        while self.running:
            envelope = {
                "task": "metadata",
                "operation": "info",
                "path": "dashboard_test.txt"
            }
            try:
                resp = self.backend.send(envelope)
            except Exception as e:
                self._report_error("Stress Test Envelope Execution", e)
                resp = {"status": "error", "detail": str(e)}

            self.log_msg(f"[{count}] {resp}")
            count += 1
            time.sleep(0.05)
