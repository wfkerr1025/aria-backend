# backend/toolchain/registry.py

import traceback
import time


class ToolchainRegistry:
    """
    Unified backend toolchain registry for ARIA Lite.
    - Provides access to executors and tools
    - Backend-only (no UI leakage)
    - Contract-aligned
    """

    REQUIRES = ["config"]
    FORBIDDEN = ["root", "window", "theme_manager", "chat_panel"]

    def __init__(self, app):
        self.app = app
        self.config = app.config

        # Lazy-loaded tools
        self._python = None
        self._powershell = None
        self._shell = None

        self._git = None
        self._lint = None

    # ---------------------------------------------------------
    # EXECUTORS
    # ---------------------------------------------------------
    def python(self):
        if self._python is None:
            from backend.sandbox.python_executor import PythonExecutor
            self._python = PythonExecutor(self.app)
        return self._python

    def powershell(self):
        if self._powershell is None:
            from backend.sandbox.powershell_executor import PowerShellExecutor
            self._powershell = PowerShellExecutor(self.app)
        return self._powershell

    def shell(self):
        if self._shell is None:
            from backend.sandbox.shell_executor import ShellExecutor
            self._shell = ShellExecutor(self.app)
        return self._shell

    # ---------------------------------------------------------
    # TOOL HELPERS
    # ---------------------------------------------------------
    def git(self):
        if self._git is None:
            from backend.toolchain.git_tools import GitTools
            self._git = GitTools(self.app)
        return self._git

    def lint(self):
        if self._lint is None:
            from backend.toolchain.lint_tools import LintTools
            self._lint = LintTools(self.app)
        return self._lint

    # ---------------------------------------------------------
    # GENERIC TOOL RUNNER
    # ---------------------------------------------------------
    def run(self, tool_name: str, *args, **kwargs):
        """
        Generic tool runner.
        Example:
            registry.run("python", script_path="myscript.py")
        """
        try:
            tool = getattr(self, tool_name, None)
            if tool is None:
                return {
                    "error": f"Unknown tool '{tool_name}'",
                    "exit_code": -1,
                }

            executor = tool()
            return executor.run(*args, **kwargs)

        except Exception as e:
            tb = traceback.format_exc()
            return {
                "error": str(e),
                "traceback": tb,
                "exit_code": -1,
            }
