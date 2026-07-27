from __future__ import annotations
from typing import Dict, Any
import os
import time
import subprocess

# ============================================================
# ENGINE IMPORTS
# ============================================================
from backend.file_ops import FileOps
from backend.context_engine import ContextEngine
from backend.patch_engine import PatchEngine

from backend.fs_engine import FSEngine
from backend.metadata_engine import MetadataEngine
from backend.binary_engine import BinaryEngine
from backend.transaction_engine import TransactionEngine
from backend.security_engine import SecurityEngine

# ============================================================
# ENGINE INSTANTIATION
# ============================================================
file_ops = FileOps()
context_engine = ContextEngine()
patch_engine = PatchEngine()
fs_engine = FSEngine()
metadata_engine = MetadataEngine()
binary_engine = BinaryEngine()
transaction_engine = TransactionEngine()
security_engine = SecurityEngine(workspace_root="./workspace")

# ============================================================
# HELPERS
# ============================================================
def _normalize(envelope: Dict[str, Any] | None) -> Dict[str, Any]:
    return envelope or {}

def _error(operation: str, detail: str) -> Dict[str, Any]:
    return {
        "status": "error",
        "operation": operation,
        "detail": detail,
        "timestamp": time.time(),
    }

def _ok(operation: str, extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    payload = {
        "status": "ok",
        "operation": operation,
        "timestamp": time.time(),
    }
    if extra:
        payload.update(extra)
    return payload

# ============================================================
# PYTHON TEST RUNNERS
# ============================================================
def _run_test_file(filename: str) -> Dict[str, Any]:
    try:
        test_path = os.path.join("backend", "tests", filename)
        result = subprocess.run(["py", test_path], capture_output=True, text=True)
        return {
            "status": "ok" if result.returncode == 0 else "fail",
            "operation": "python_test",
            "file": filename,
            "output": result.stdout,
            "errors": result.stderr,
            "returncode": result.returncode,
        }
    except Exception as e:
        return _error("python_test", str(e))

def _run_all_tests() -> Dict[str, Any]:
    try:
        result = subprocess.run(
            ["py", "backend/tests/run_all_tests.py"],
            capture_output=True,
            text=True,
        )
        return {
            "status": "ok" if result.returncode == 0 else "fail",
            "operation": "run_all_python_tests",
            "output": result.stdout,
            "errors": result.stderr,
            "returncode": result.returncode,
        }
    except Exception as e:
        return _error("run_all_python_tests", str(e))

# ============================================================
# TASK HANDLERS
# ============================================================

def handle_chat(envelope: Dict[str, Any]) -> Dict[str, Any]:
    return _ok("chat", {
        "reply": f"CHAT ROUTER RECEIVED: {envelope.get('content', '')}"
    })

def handle_file_ops(envelope: Dict[str, Any]) -> Dict[str, Any]:
    op = envelope.get("operation")
    path = envelope.get("path")
    content = envelope.get("content", "")

    if not path:
        return _error("file_ops", "Missing 'path'")

    ops = {
        "read": lambda: file_ops.read_file(path),
        "write": lambda: file_ops.write_file(path, content),
        "delete": lambda: file_ops.delete_file(path),
    }

    if op not in ops:
        return _error("file_ops", f"Unknown operation '{op}'")

    try:
        return ops[op]()
    except Exception as e:
        return _error("file_ops", str(e))

def handle_context(envelope: Dict[str, Any]) -> Dict[str, Any]:
    data = envelope.get("data", {}) or {}
    text = data.get("text", "")
    mode = data.get("mode", "all")

    try:
        result = {}

        if mode in ("entities", "all"):
            result["entities"] = context_engine.extract_entities(text)
        if mode in ("intent", "all"):
            result["intent"] = context_engine.detect_intent(text)
        if mode in ("topics", "all"):
            result["topics"] = context_engine.suggest_topics(text)
        if mode == "snapshot":
            result["snapshot"] = context_engine.get_snapshot()

        return _ok("context", {"result": result})
    except Exception as e:
        return _error("context", str(e))

def handle_patch(envelope: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return patch_engine.apply_patch(envelope)
    except Exception as e:
        return _error("patch", str(e))

def handle_fs(envelope: Dict[str, Any]) -> Dict[str, Any]:
    op = envelope.get("operation")
    src = envelope.get("src")
    dst = envelope.get("dst")
    path = envelope.get("path")
    recursive = envelope.get("recursive", False)

    ops = {
        "copy": lambda: fs_engine.copy_file(src, dst),
        "move": lambda: fs_engine.move_file(src, dst),
        "rename": lambda: fs_engine.rename_file(src, dst),
        "list": lambda: fs_engine.list_dir(path),
        "mkdir": lambda: fs_engine.make_dir(path),
        "rmdir": lambda: fs_engine.delete_dir(path, recursive),
    }

    if op not in ops:
        return _error("fs", f"Unknown operation '{op}'")

    try:
        return ops[op]()
    except Exception as e:
        return _error("fs", str(e))

def handle_metadata(envelope: Dict[str, Any]) -> Dict[str, Any]:
    op = envelope.get("operation")
    path = envelope.get("path")

    ops = {
        "exists_file": lambda: metadata_engine.file_exists(path),
        "exists_dir": lambda: metadata_engine.dir_exists(path),
        "info": lambda: metadata_engine.file_info(path),
        "hash": lambda: metadata_engine.file_hash(path),
    }

    if op not in ops:
        return _error("metadata", f"Unknown operation '{op}'")

    try:
        return ops[op]()
    except Exception as e:
        return _error("metadata", str(e))

def handle_binary(envelope: Dict[str, Any]) -> Dict[str, Any]:
    op = envelope.get("operation")
    path = envelope.get("path")
    data = envelope.get("data")

    ops = {
        "read": lambda: binary_engine.read_binary(path),
        "write": lambda: binary_engine.write_binary(path, data),
    }

    if op not in ops:
        return _error("binary", f"Unknown operation '{op}'")

    try:
        return ops[op]()
    except Exception as e:
        return _error("binary", str(e))

def handle_transaction(envelope: Dict[str, Any]) -> Dict[str, Any]:
    op = envelope.get("operation")
    path = envelope.get("path")
    content = envelope.get("content", "")

    ops = {
        "atomic_write": lambda: transaction_engine.atomic_write(path, content),
        "safe_replace": lambda: transaction_engine.safe_replace(path, content),
    }

    if op not in ops:
        return _error("transaction", f"Unknown operation '{op}'")

    try:
        return ops[op]()
    except Exception as e:
        return _error("transaction", str(e))

def handle_security(envelope: Dict[str, Any]) -> Dict[str, Any]:
    op = envelope.get("operation")
    path = envelope.get("path")

    ops = {
        "validate": lambda: security_engine.validate_path(path),
        "in_workspace": lambda: {
            "status": "ok",
            "operation": "in_workspace",
            "path": path,
            "in_workspace": security_engine.is_in_workspace(path),
        },
    }

    if op not in ops:
        return _error("security", f"Unknown operation '{op}'")

    try:
        return ops[op]()
    except Exception as e:
        return _error("security", str(e))

# ============================================================
# UNIVERSAL DISPATCHER
# ============================================================
TASK_MAP = {
    "chat": handle_chat,
    "file_ops": handle_file_ops,
    "context": handle_context,
    "patch": handle_patch,
    "fs": handle_fs,
    "metadata": handle_metadata,
    "binary": handle_binary,
    "transaction": handle_transaction,
    "security": handle_security,

    "ping": lambda e: _ok("ping", {"message": "Backend reachable"}),
    "health": lambda e: _ok("health", {"message": "Backend healthy"}),

    "run_python_tests": lambda e: _run_all_tests(),
    "test_workspace": lambda e: _run_test_file("workspace_tests.py"),
    "test_sandbox": lambda e: _run_test_file("sandbox_tests.py"),
    "test_router": lambda e: _run_test_file("router_tests.py"),
    "test_contract": lambda e: _run_test_file("contract_tests.py"),
    "test_registry": lambda e: _run_test_file("test_registry.py"),
    "test_toolchain": lambda e: _run_test_file("toolchain_tests.py"),

    "debug": lambda e: _ok("debug", {"message": "Debug logged"}),
    "stream": lambda e: _ok("stream", {
        "message": f"Stream accepted: {e.get('prompt','')[:40]}..."
    }),
}

def process_envelope(envelope: Dict[str, Any] | None) -> Dict[str, Any]:
    envelope = _normalize(envelope)
    task = envelope.get("task") or envelope.get("action")

    if not task:
        return _error("dispatch", "Envelope missing 'task' or 'action'")

    handler = TASK_MAP.get(task)
    if not handler:
        return _error("dispatch", f"Unknown task '{task}'")

    return handler(envelope)

# ============================================================
# REST-LIKE ENDPOINT COMPATIBILITY
# ============================================================
def send_to(endpoint: str, envelope: Dict[str, Any] | None) -> Dict[str, Any]:
    endpoint = endpoint.lstrip("/")

    endpoint_map = {
        "file_ops": "file_ops",
        "context": "context",
        "debug/log": "debug",
        "stream": "stream",
        "health": "health",
        "ping": "ping",
        "command": None,

        "tests/run_all": "run_python_tests",
        "tests/workspace": "test_workspace",
        "tests/sandbox": "test_sandbox",
        "tests/router": "test_router",
        "tests/contract": "test_contract",
        "tests/registry": "test_registry",
        "tests/toolchain": "test_toolchain",
    }

    if endpoint not in endpoint_map:
        return _error("send_to", f"Unknown endpoint '{endpoint}'")

    task = endpoint_map[endpoint]
    envelope = _normalize(envelope)

    if task:
        envelope["task"] = task

    return process_envelope(envelope)
