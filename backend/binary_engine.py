from __future__ import annotations
from typing import Dict, Any


class BinaryEngine:
    """
    Unified binary file engine for ARIA Lite.
    Handles:
      - read_binary(path)
      - write_binary(path, data)
    Returns unified result dictionaries compatible with PacketExecutor,
    BackendAdapter, and ARIAInterface.
    """

    # ---------------------------------------------------------
    # READ BINARY
    # ---------------------------------------------------------
    def read_binary(self, path: str) -> Dict[str, Any]:
        try:
            with open(path, "rb") as f:
                data = f.read()

            return {
                "status": "ok",
                "path": path,
                "data": data
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
    # WRITE BINARY
    # ---------------------------------------------------------
    def write_binary(self, path: str, data) -> Dict[str, Any]:
        try:
            # Allow list-of-bytes input
            if isinstance(data, list):
                data = bytes(data)

            with open(path, "wb") as f:
                f.write(data)

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
