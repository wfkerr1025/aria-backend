# backend/toolchain/lint_tools.py

import subprocess
import time
import traceback


class LintTools:
    """
    Linting and formatting tools for ARIA Lite.
    - Supports flake8, black, isort, etc.
    """

    REQUIRES = ["config"]
    FORBIDDEN = ["root", "window", "theme_manager", "chat_panel"]

    def __init__(self, app):
        self.app = app
        self.config = app.config

        self.flake8_path = getattr(self.config, "flake8_path", "flake8")
        self.black_path = getattr(self.config, "black_path", "black")
        self.isort_path = getattr(self.config, "isort_path", "isort")

    def _run(self, cmd: list[str]):
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

    def flake8(self, path: str):
        return self._run([self.flake8_path, path])

    def black(self, path: str):
        return self._run([self.black_path, path])

    def isort(self, path: str):
        return self._run([self.isort_path, path])
