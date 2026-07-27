# backend/sandbox/powershell_executor.py

import subprocess
import os
import time
import traceback


class PowerShellExecutor:
    """
    Backend PowerShell script executor for ARIA Lite.
    - Runs .ps1 scripts or inline PowerShell commands
    """

    REQUIRES = ["config"]
    FORBIDDEN = ["root", "window", "theme_manager", "chat_panel"]

    def __init__(self, app):
        self.app = app
        self.config = app.config
        self.pwsh_path = getattr(self.config, "powershell_path", "pwsh")

    def run_script(self, script_path: str, args: list[str] | None = None) -> dict:
        args = args or []
        cmd = [self.pwsh_path, "-File", script_path] + args

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

    def run_inline(self, command: str) -> dict:
        cmd = [self.pwsh_path, "-Command", command]

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
