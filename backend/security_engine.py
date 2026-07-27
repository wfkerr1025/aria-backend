from __future__ import annotations
from typing import Dict, Any
import os


class SecurityEngine:
    """
    Unified ARIA Lite Security Engine
    ---------------------------------
    Responsible for:
      - path normalization
      - workspace boundary enforcement
      - directory traversal prevention
      - safe validation for all backend operations
    All operations return unified result dictionaries.
    """

    def __init__(self, workspace_root: str = "workspace"):
        # Always store absolute workspace root
        self.workspace_root = os.path.abspath(workspace_root)

    # ---------------------------------------------------------
    # NORMALIZE PATH
    # ---------------------------------------------------------
    def normalize(self, path: str) -> str:
        if not path:
            return ""
        return os.path.abspath(path)

    # ---------------------------------------------------------
    # CHECK WORKSPACE MEMBERSHIP
    # ---------------------------------------------------------
    def is_in_workspace(self, path: str) -> bool:
        abs_path = self.normalize(path)
        return abs_path.startswith(self.workspace_root)

    # ---------------------------------------------------------
    # VALIDATE PATH (internal)
    # ---------------------------------------------------------
    def validate(self, path: str) -> Dict[str, Any]:
        if not path:
            return {
                "status": "error",
                "operation": "validate",
                "detail": "No path provided"
            }

        abs_path = self.normalize(path)

        if not self.is_in_workspace(abs_path):
            return {
                "status": "error",
                "operation": "validate",
                "detail": f"Path '{path}' is outside workspace",
                "path": abs_path
            }

        return {
            "status": "ok",
            "operation": "validate",
            "path": abs_path
        }

    # ---------------------------------------------------------
    # PUBLIC VALIDATION ENTRY POINT
    # ---------------------------------------------------------
    def validate_path(self, path: str) -> Dict[str, Any]:
        try:
            return self.validate(path)
        except Exception as e:
            return {
                "status": "error",
                "operation": "validate_path",
                "detail": str(e)
            }

    # ---------------------------------------------------------
    # ROUTER ENTRY POINT
    # ---------------------------------------------------------
    def execute(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        """
        Expected envelope:
        {
            "task": "security",
            "operation": "validate" | "in_workspace",
            "path": "workspace/somefile.txt"
        }
        """

        op = envelope.get("operation")
        path = envelope.get("path")

        if op == "validate":
            return self.validate_path(path)

        if op == "in_workspace":
            abs_path = self.normalize(path)
            return {
                "status": "ok",
                "operation": "in_workspace",
                "path": abs_path,
                "in_workspace": self.is_in_workspace(abs_path)
            }

        return {
            "status": "error",
            "operation": "security",
            "detail": f"Unknown security operation '{op}'"
        }
