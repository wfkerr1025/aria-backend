from __future__ import annotations
from typing import Dict, Any
import os


class FileOps:
    """
    Unified hardened filesystem operations for ARIA Lite.
    Predictable return structure:
      - {"status": "ok", ...}
      - {"status": "error", ...}
    """

    # ---------------------------------------------------------
    # READ FILE
    # ---------------------------------------------------------
    def read_file(self, path: str) -> Dict[str, Any]:
        """
        Safely read a file.
        Returns:
          {"status": "ok", "content": "..."}
          {"status": "error", "detail": "..."}
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                return {
                    "status": "ok",
                    "path": path,
                    "content": f.read()
                }
        except FileNotFoundError:
            return {
                "status": "error",
                "path": path,
                "detail": "File not found"
            }
        except Exception as e:
            return {
                "status": "error",
                "path": path,
                "detail": str(e)
            }

    # ---------------------------------------------------------
    # WRITE FILE
    # ---------------------------------------------------------
    def write_file(self, path: str, content: str) -> Dict[str, Any]:
        """
        Safely write content to a file.
        Creates directories automatically.
        Returns:
          {"status": "ok", "path": path}
          {"status": "error", "detail": "..."}
        """
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)

            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

            return {
                "status": "ok",
                "path": path
            }
        except Exception as e:
            return {
                "status": "error",
                "path": path,
                "detail": str(e)
            }

    # ---------------------------------------------------------
    # DELETE FILE
    # ---------------------------------------------------------
    def delete_file(self, path: str) -> Dict[str, Any]:
        """
        Safely delete a file.
        Returns:
          {"status": "ok", "path": path}
          {"status": "error", "detail": "..."}
        """
        try:
            os.remove(path)
            return {
                "status": "ok",
                "path": path
            }
        except FileNotFoundError:
            return {
                "status": "error",
                "path": path,
                "detail": "File not found"
            }
        except Exception as e:
            return {
                "status": "error",
                "path": path,
                "detail": str(e)
            }
