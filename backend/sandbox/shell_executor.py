# backend/sandbox/shell_executor.py

import subprocess
import os
import time
import traceback
import platform


class ShellExecutor:
    """
    Backend shell command executor for ARIA Lite.
    - Runs OS shell commands safely
    """

    REQUIRES = ["config"]
    FORBIDDEN = ["root", "window", "theme_manager", "chat_panel"]

    def __init__(self, app):
        self.app = app
        self.config = app.config

        if platform.system().lower().startswith("win"):
            self.shell = getattr(self.config, "shell_path", "cmd.exe")
            self.shell_flag = "/C"
        else:
            self.shell = getattr(self.config, "shell_path", "/bin/bash")
            self.shell_flag = "-c"

    def run(self, command: str) -> dict:
        cmd = [self.shell, self.shell_flag, command]

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
