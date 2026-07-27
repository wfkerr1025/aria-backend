# backend/toolchain/git_tools.py

import subprocess
import time
import traceback


class GitTools:
    """
    Git command wrapper for ARIA Lite.
    - Backend-only
    - Safe execution
    """

    REQUIRES = ["config"]
    FORBIDDEN = ["root", "window", "theme_manager", "chat_panel"]

    def __init__(self, app):
        self.app = app
        self.config = app.config

        # Load configured path or default to "git"
        self.git_path = getattr(self.config, "git_path", "git")

        # Auto-detect git if "git" is not resolvable
        if self.git_path == "git":
            import shutil
            detected = shutil.which("git")
            if detected:
                self.git_path = detected

    def run(self, args: list[str]):
        cmd = [self.git_path] + args

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
