from __future__ import annotations
from typing import Dict, Any
import os
import tempfile
import shutil


class TransactionEngine:
    """
    Unified atomic write and rollback engine for ARIA Lite.
    Provides:
      - atomic_write(path, content)
      - safe_replace(path, new_content)
    All operations return unified result dictionaries.
    """

    # ---------------------------------------------------------
    # ATOMIC WRITE
    # ---------------------------------------------------------
    def atomic_write(self, path: str, content: str) -> Dict[str, Any]:
        directory = os.path.dirname(path) or "."

        try:
            os.makedirs(directory, exist_ok=True)

            # Create temporary file in same directory
            fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".aria_tmp_")

            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                    tmp.write(content)

                # Atomic replace
                os.replace(temp_path, path)

            finally:
                # Cleanup if temp still exists
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass

            return {
                "status": "ok",
                "operation": "atomic_write",
                "path": path
            }

        except Exception as e:
            return {
                "status": "error",
                "operation": "atomic_write",
                "path": path,
                "detail": str(e)
            }

    # ---------------------------------------------------------
    # SAFE REPLACE
    # ---------------------------------------------------------
    def safe_replace(self, path: str, new_content: str) -> Dict[str, Any]:
        """
        Unified safe replace:
        - Reads existing file (optional)
        - Writes new content atomically
        """
        return self.atomic_write(path, new_content)
