# backend/sandbox/python_executor.py

import subprocess
import sys
import os
import time
import traceback


class PythonExecutor:
    """
    Backend Python script executor for ARIA Lite.
    - Runs Python scripts safely
    - Captures stdout, stderr, exit code, duration
    """

    REQUIRES = ["config"]
    FORBIDDEN = ["root", "window", "theme_manager", "chat_panel"]

    def __init__(self, app):
        self.app = app
        self.config = app.config
        self.python_path = getattr(self.config, "python_path", sys.executable)

    def run_script(self, script_path: str, args: list[str] | None = None) -> dict:
        args = args or []
        cmd = [self.python_path, script_path] + args

        start = time.time()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout, stderr = proc.communicate()
            duration = round((time.time() - start) * 1000, 2)

            return {
                "command": cmd,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": proc.returncode,
                "duration_ms": duration,
            }
        except Exception as e:
            tb = traceback.format_exc()
            return {
                "command": cmd,
                "stdout": "",
                "stderr": f"{e}\n{tb}",
                "exit_code": -1,
                "duration_ms": round((time.time() - start) * 1000, 2),
            }
    
    def run_inline(self, code: str) -> dict:
        """
        Execute a small inline Python snippet safely.
        """
        import tempfile

        # Create a temporary script file
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".py") as tmp:
            tmp.write(code)
            tmp_path = tmp.name

        # Reuse run_script for consistent behavior
        result = self.run_script(tmp_path)

        # Clean up temp file
        try:
            os.remove(tmp_path)
        except Exception:
            pass

        return result
