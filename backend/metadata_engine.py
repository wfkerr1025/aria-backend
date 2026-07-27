from __future__ import annotations
from typing import Dict, Any
import os
import hashlib


class MetadataEngine:
    """
    Unified file metadata engine for ARIA Lite.
    Provides:
      - file_exists
      - dir_exists
      - file_info (size, timestamps)
      - file_hash (sha256)
    All operations return unified result dictionaries.
    """

    # ---------------------------------------------------------
    # FILE EXISTS
    # ---------------------------------------------------------
    def file_exists(self, path: str) -> Dict[str, Any]:
        exists = os.path.isfile(path)
        return {
            "status": "ok",
            "operation": "file_exists",
            "path": path,
            "exists": exists
        }

    # ---------------------------------------------------------
    # DIRECTORY EXISTS
    # ---------------------------------------------------------
    def dir_exists(self, path: str) -> Dict[str, Any]:
        exists = os.path.isdir(path)
        return {
            "status": "ok",
            "operation": "dir_exists",
            "path": path,
            "exists": exists
        }

    # ---------------------------------------------------------
    # FILE INFO
    # ---------------------------------------------------------
    def file_info(self, path: str) -> Dict[str, Any]:
        if not os.path.isfile(path):
            return {
                "status": "error",
                "operation": "file_info",
                "path": path,
                "detail": "File not found"
            }

        try:
            stat = os.stat(path)
            return {
                "status": "ok",
                "operation": "file_info",
                "path": path,
                "size": stat.st_size,
                "created_at": stat.st_ctime,
                "modified_at": stat.st_mtime
            }
        except Exception as e:
            return {
                "status": "error",
                "operation": "file_info",
                "path": path,
                "detail": str(e)
            }

    # ---------------------------------------------------------
    # FILE HASH (SHA256)
    # ---------------------------------------------------------
    def file_hash(self, path: str) -> Dict[str, Any]:
        if not os.path.isfile(path):
            return {
                "status": "error",
                "operation": "file_hash",
                "path": path,
                "detail": "File not found"
            }

        try:
            sha = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    sha.update(chunk)

            return {
                "status": "ok",
                "operation": "file_hash",
                "path": path,
                "hash": sha.hexdigest(),
                "algorithm": "sha256"
            }
        except Exception as e:
            return {
                "status": "error",
                "operation": "file_hash",
                "path": path,
                "detail": str(e)
            }
