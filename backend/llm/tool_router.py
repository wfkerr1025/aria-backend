from __future__ import annotations
from typing import Dict, Any


class ToolRouter:
    """
    Interprets LLM output and decides whether it is:
    - A tool envelope (to be executed by backend dispatcher)
    - A normal assistant message
    """

    SAFE_TASKS = {
        "file_ops",
        "patch",
        "fs",
        "metadata",
        "binary",
        "transaction",
        "security",
        "context",
        "run_python_tests",
        "test_workspace",
        "test_sandbox",
        "test_router",
        "test_contract",
        "test_registry",
        "test_toolchain",
    }

    REQUIRED_FIELDS = {"task"}

    def is_tool_envelope(self, msg: Any) -> bool:
        """
        Detects whether the LLM output is a valid tool envelope.
        """

        if not isinstance(msg, dict):
            return False

        # Must contain required fields
        if not all(field in msg for field in self.REQUIRED_FIELDS):
            return False

        task = msg.get("task")
        if not isinstance(task, str):
            return False

        # Must be a SAFE task
        return task in self.SAFE_TASKS

    def route(self, llm_output: Any) -> Dict[str, Any]:
        """
        Main decision function:
        - If LLM output is a tool envelope → wrap as tool_request
        - Otherwise → wrap as assistant text
        """

        if self.is_tool_envelope(llm_output):
            return {
                "type": "tool_request",
                "mode": "tool",
                "content": llm_output,
            }

        return {
            "type": "assistant",
            "mode": "text",
            "content": llm_output,
        }


tool_router = ToolRouter()
