# backend/toolchain/python_tools.py

import traceback
import time


class PythonTools:
    """
    High-level Python tooling for ARIA Lite.
    - Uses PythonExecutor under the hood
    - Provides helpers for running modules, scripts, and inline code
    """

    REQUIRES = ["config"]
    FORBIDDEN = ["root", "window", "theme_manager", "chat_panel"]

    def __init__(self, app):
        self.app = app
        self.config = app.config

        from backend.sandbox.python_executor import PythonExecutor
        self.executor = PythonExecutor(app)

    def run_script(self, script_path: str, args: list[str] | None = None):
        return self.executor.run_script(script_path, args)

    def run_inline(self, code: str):
        """
        Runs inline Python code by writing it to a temp file.
        """
        try:
            import tempfile
            with tempfile.NamedTemporaryFile("w", delete=False, suffix=".py") as tmp:
                tmp.write(code)
                tmp_path = tmp.name

            return self.executor.run_script(tmp_path)

        except Exception as e:
            tb = traceback.format_exc()
            return {
                "error": str(e),
                "traceback": tb,
                "exit_code": -1,
            }
