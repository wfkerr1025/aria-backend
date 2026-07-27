# windows/module_sandbox.py
import tkinter as tk
from tkinter import ttk, messagebox
import traceback
import importlib
import time
import os


class ModuleSandbox:
    """
    Unified lightweight harness to test window/frame modules in isolation.
    Usage (from shell):
        python -m windows.module_sandbox windows.task_browser TaskBrowserWindow
    """

    def __init__(self, module_path: str, class_name: str):
        self.module_path = module_path
        self.class_name = class_name

        # Safe-call system
        self.safe_call = self._install_safe_call()
        self._report_error = self._install_error_reporter()

    # ============================================================
    # SAFE CALL SYSTEM
    # ============================================================
    def _install_safe_call(self):
        def safe_call(label, func):
            try:
                print(f"[ModuleSandbox] Executing: {label}")
                start = time.time()
                result = func()
                elapsed = round((time.time() - start) * 1000, 2)
                print(f"[ModuleSandbox] Completed: {label} ({elapsed} ms)")
                return result
            except Exception as e:
                self._report_error(label, e)
                return None
        return safe_call

    def _install_error_reporter(self):
        def report_error(label, exception):
            tb = traceback.format_exc()
            print("\n" + "=" * 60)
            print(f"[ModuleSandbox ERROR] Action: {label}")
            print(f"[ModuleSandbox ERROR] Exception: {exception}")
            print(tb)
            print("=" * 60 + "\n")

            os.makedirs("logs", exist_ok=True)
            with open("logs/module_sandbox_errors.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{time.ctime()}] ERROR in '{label}': {exception}\n")
                f.write(tb + "\n")
        return report_error

    # ============================================================
    # SAFE IMPORT (Unified)
    # ============================================================
    def _safe_import(self, module_path: str, class_name: str):
        try:
            mod = importlib.import_module(module_path)
            return getattr(mod, class_name)
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[ModuleSandbox IMPORT ERROR] {module_path}.{class_name}: {e}")
            print(tb)
            return None

    # ============================================================
    # RUN SANDBOX
    # ============================================================
    def run(self):
        root = tk.Tk()
        root.title(f"Sandbox: {self.module_path}.{self.class_name}")
        root.geometry("900x600")

        FrameClass = self.safe_call(
            "Import Module",
            lambda: self._safe_import(self.module_path, self.class_name)
        )

        if FrameClass is None:
            self._render_failure(root)
        else:
            self.safe_call("Initialize FrameClass", lambda: self._render_frame(root, FrameClass))

        root.mainloop()

    # ============================================================
    # RENDER FAILURE
    # ============================================================
    def _render_failure(self, root):
        frame = tk.Frame(root, bg="#252525")
        tk.Label(
            frame,
            text=f"Failed to load {self.module_path}.{self.class_name}\nCheck console/logs.",
            bg="#252525",
            fg="#ffffff",
            font=("Segoe UI", 10)
        ).pack(padx=20, pady=20)
        frame.pack(fill="both", expand=True)

    # ============================================================
    # RENDER FRAME
    # ============================================================
    def _render_frame(self, root, FrameClass):
        try:
            # Most ARIA panels take (parent, app, theme_manager)
            # For sandboxing, we pass only the root.
            widget = FrameClass(root)
            widget.pack(fill="both", expand=True)
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[ModuleSandbox INIT ERROR] {self.module_path}.{self.class_name}: {e}")
            print(tb)

            frame = tk.Frame(root, bg="#252525")
            tk.Label(
                frame,
                text=f"Error initializing {self.module_path}.{self.class_name}\n\n{e}",
                bg="#252525",
                fg="#ffffff",
                font=("Segoe UI", 10)
            ).pack(padx=20, pady=20)
            frame.pack(fill="both", expand=True)


# ============================================================
# MAIN ENTRYPOINT
# ============================================================
def main():
    import sys

    if len(sys.argv) != 3:
        print("Usage: python -m windows.module_sandbox <module_path> <class_name>")
        sys.exit(1)

    module_path = sys.argv[1]
    class_name = sys.argv[2]

    sandbox = ModuleSandbox(module_path, class_name)
    sandbox.run()


if __name__ == "__main__":
    main()
