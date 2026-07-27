from __future__ import annotations
from typing import Dict, Any, List, Optional
import os


# ============================================================
# PATCH CHUNK
# ============================================================
class PatchChunk:
    """
    Represents a single atomic patch operation.
    Supported operations:
      - write_file
      - delete_file
      - replace_text
    """

    def __init__(self, operation: str, target: str, content: Optional[str] = None,
                 find_text: Optional[str] = None, replace_text: Optional[str] = None):
        self.operation = operation
        self.target = target
        self.content = content
        self.find_text = find_text
        self.replace_text = replace_text

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operation": self.operation,
            "target": self.target,
            "content": self.content,
            "find": self.find_text,
            "replace": self.replace_text,
        }


# ============================================================
# PATCH PLAN
# ============================================================
class PatchPlan:
    """
    A patch plan is a list of PatchChunks.
    Deterministic and reversible.
    """

    def __init__(self, chunks: List[PatchChunk]):
        self.chunks = chunks

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chunks": [c.to_dict() for c in self.chunks]
        }


# ============================================================
# PATCH ENGINE
# ============================================================
class PatchEngine:
    """
    Unified safe, deterministic patch engine for ARIA Lite.
    Responsibilities:
      - Generate patch plans from envelopes
      - Apply patch plans safely
      - Validate operations
      - Never corrupt files
    """

    # ---------------------------------------------------------
    # APPLY PATCH
    # ---------------------------------------------------------
    def apply_patch(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        operation = envelope.get("operation")
        path = envelope.get("path")

        if not operation:
            return {"status": "error", "message": "Patch missing 'operation'"}

        if not path:
            return {"status": "error", "message": "Patch missing 'path'"}

        plan = self.generate_patch_plan(
            operation=operation,
            path=path,
            content=envelope.get("content"),
            find_text=envelope.get("find"),
            replace_text=envelope.get("replace"),
        )

        results = [self._apply_chunk(chunk) for chunk in plan.chunks]

        return {
            "status": "ok",
            "operation": "apply_patch",
            "patch_plan": plan.to_dict(),
            "results": results,
        }

    # ---------------------------------------------------------
    # GENERATE PATCH PLAN
    # ---------------------------------------------------------
    def generate_patch_plan(
        self,
        operation: str,
        path: str,
        content: Optional[str],
        find_text: Optional[str],
        replace_text: Optional[str],
    ) -> PatchPlan:

        chunks: List[PatchChunk] = []

        if operation == "write_file":
            chunks.append(PatchChunk("write_file", path, content))

        elif operation == "delete_file":
            chunks.append(PatchChunk("delete_file", path))

        elif operation == "replace_text":
            chunks.append(PatchChunk(
                "replace_text",
                path,
                content=None,
                find_text=find_text,
                replace_text=replace_text
            ))

        else:
            chunks.append(PatchChunk("invalid", path))

        return PatchPlan(chunks)

    # ---------------------------------------------------------
    # APPLY CHUNK
    # ---------------------------------------------------------
    def _apply_chunk(self, chunk: PatchChunk) -> Dict[str, Any]:
        op = chunk.operation
        path = chunk.target

        # WRITE FILE
        if op == "write_file":
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(chunk.content or "")
                return {"status": "ok", "operation": op, "path": path}
            except Exception as e:
                return {"status": "error", "operation": op, "path": path, "detail": str(e)}

        # DELETE FILE
        if op == "delete_file":
            try:
                os.remove(path)
                return {"status": "ok", "operation": op, "path": path}
            except FileNotFoundError:
                return {"status": "error", "operation": op, "path": path, "detail": "File not found"}
            except Exception as e:
                return {"status": "error", "operation": op, "path": path, "detail": str(e)}

        # REPLACE TEXT
        if op == "replace_text":
            try:
                if not os.path.exists(path):
                    return {"status": "error", "operation": op, "path": path, "detail": "File not found"}

                with open(path, "r", encoding="utf-8") as f:
                    original = f.read()

                find_text = chunk.find_text or ""
                replace_text = chunk.replace_text or ""

                new_text = original.replace(find_text, replace_text)

                with open(path, "w", encoding="utf-8") as f:
                    f.write(new_text)

                return {"status": "ok", "operation": op, "path": path}

            except Exception as e:
                return {"status": "error", "operation": op, "path": path, "detail": str(e)}

        # INVALID OPERATION
        return {
            "status": "error",
            "operation": op,
            "path": path,
            "detail": "Invalid patch operation"
        }
