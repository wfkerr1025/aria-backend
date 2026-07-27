from __future__ import annotations
from typing import Dict, Any
import os
import shutil


class FSEngine:
    """
    Unified filesystem workflow engine for ARIA Lite.
    Handles higher-level operations:
      - copy_file
      - move_file
      - rename_file
      - list_dir
      - make_dir
      - delete_dir
    All operations return unified result dictionaries.
    """

    # ---------------------------------------------------------
    # COPY FILE
    # ---------------------------------------------------------
    def copy_file(self, src: str, dst: str) -> Dict[str, Any]:
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            return {
                "status": "ok",
                "operation": "copy_file",
                "src": src,
                "dst": dst
            }
        except FileNotFoundError:
            return {
                "status": "error",
                "operation": "copy_file",
                "src": src,
                "dst": dst,
                "detail": "Source not found"
            }
        except Exception as e:
            return {
                "status": "error",
                "operation": "copy_file",
                "src": src,
                "dst": dst,
                "detail": str(e)
            }

    # ---------------------------------------------------------
    # MOVE FILE
    # ---------------------------------------------------------
    def move_file(self, src: str, dst: str) -> Dict[str, Any]:
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            return {
                "status": "ok",
                "operation": "move_file",
                "src": src,
                "dst": dst
            }
        except FileNotFoundError:
            return {
                "status": "error",
                "operation": "move_file",
                "src": src,
                "dst": dst,
                "detail": "Source not found"
            }
        except Exception as e:
            return {
                "status": "error",
                "operation": "move_file",
                "src": src,
                "dst": dst,
                "detail": str(e)
            }

    # ---------------------------------------------------------
    # RENAME FILE
    # ---------------------------------------------------------
    def rename_file(self, src: str, new_name: str) -> Dict[str, Any]:
        try:
            directory = os.path.dirname(src)
            dst = os.path.join(directory, new_name)
            os.rename(src, dst)
            return {
                "status": "ok",
                "operation": "rename_file",
                "src": src,
                "dst": dst
            }
        except FileNotFoundError:
            return {
                "status": "error",
                "operation": "rename_file",
                "src": src,
                "detail": "Source not found"
            }
        except Exception as e:
            return {
                "status": "error",
                "operation": "rename_file",
                "src": src,
                "detail": str(e)
            }

    # ---------------------------------------------------------
    # LIST DIRECTORY
    # ---------------------------------------------------------
    def list_dir(self, path: str) -> Dict[str, Any]:
        try:
            items = os.listdir(path)
            return {
                "status": "ok",
                "operation": "list_dir",
                "path": path,
                "items": items
            }
        except FileNotFoundError:
            return {
                "status": "error",
                "operation": "list_dir",
                "path": path,
                "detail": "Directory not found"
            }
        except Exception as e:
            return {
                "status": "error",
                "operation": "list_dir",
                "path": path,
                "detail": str(e)
            }

    # ---------------------------------------------------------
    # MAKE DIRECTORY
    # ---------------------------------------------------------
    def make_dir(self, path: str) -> Dict[str, Any]:
        try:
            os.makedirs(path, exist_ok=True)
            return {
                "status": "ok",
                "operation": "make_dir",
                "path": path
            }
        except Exception as e:
            return {
                "status": "error",
                "operation": "make_dir",
                "path": path,
                "detail": str(e)
            }

    # ---------------------------------------------------------
    # DELETE DIRECTORY
    # ---------------------------------------------------------
    def delete_dir(self, path: str, recursive: bool = False) -> Dict[str, Any]:
        try:
            if recursive:
                shutil.rmtree(path)
            else:
                os.rmdir(path)

            return {
                "status": "ok",
                "operation": "delete_dir",
                "path": path,
                "recursive": recursive
            }
        except FileNotFoundError:
            return {
                "status": "error",
                "operation": "delete_dir",
                "path": path,
                "detail": "Directory not found"
            }
        except Exception as e:
            return {
                "status": "error",
                "operation": "delete_dir",
                "path": path,
                "detail": str(e)
            }
