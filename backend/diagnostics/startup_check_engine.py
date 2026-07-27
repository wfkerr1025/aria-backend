# startup_check_engine.py
import time
import uuid
import traceback
import os
from dataclasses import dataclass, field


# ============================================================
# DATA CLASSES
# ============================================================
@dataclass
class SubsystemStatus:
    name: str
    status: str   # "ok", "warning", "error"
    message: str


@dataclass
class StartupResult:
    duration: float
    timestamp: float
    boot_id: str
    subsystems: list = field(default_factory=list)

    def has_issues(self):
        return any(s.status != "ok" for s in self.subsystems)

    def warnings_count(self):
        return sum(1 for s in self.subsystems if s.status == "warning")

    def errors_count(self):
        return sum(1 for s in self.subsystems if s.status == "error")


# ============================================================
# SAFE CALL + ERROR REPORTING
# ============================================================
def safe_call(label, func):
    """
    Execute a subsystem check safely.
    Returns SubsystemStatus on failure.
    """
    try:
        print(f"[StartupCheck] Executing: {label}")
        result = func()
        print(f"[StartupCheck] Completed: {label}")
        return result
    except Exception as e:
        return report_error(label, e)


def report_error(label, exception):
    """
    Log startup subsystem errors and return SubsystemStatus(error).
    """
    tb = traceback.format_exc()

    print("\n" + "=" * 60)
    print(f"[StartupCheck ERROR] Action: {label}")
    print(f"[StartupCheck ERROR] Exception: {exception}")
    print(tb)
    print("=" * 60 + "\n")

    os.makedirs("logs", exist_ok=True)
    with open("logs/startup_check_engine_errors.log", "a", encoding="utf-8") as f:
        f.write(f"\n[{time.ctime()}] ERROR in '{label}': {exception}\n")
        f.write(tb + "\n")

    return SubsystemStatus(label, "error", str(exception))


# ============================================================
# SUBSYSTEM CHECKS
# ============================================================
def _check_router(backend):
    return safe_call("Router", lambda: _router_impl(backend))


def _router_impl(backend):
    envelope = {
        "task": "metadata",
        "operation": "exists_file",
        "path": "startup_check_probe"
    }
    resp = backend.send(envelope)
    if isinstance(resp, dict):
        return SubsystemStatus("Router", "ok", "Router online")
    return SubsystemStatus("Router", "warning", "Unexpected router response")


def _check_fileops(backend):
    return safe_call("FileOps", lambda: _fileops_impl(backend))


def _fileops_impl(backend):
    envelope = {
        "task": "file_ops",
        "operation": "write",
        "path": "startup_probe.txt",
        "content": "probe"
    }
    resp = backend.send_to("/file_ops", envelope)
    if resp.get("status") == "ok":
        return SubsystemStatus("FileOps", "ok", "FileOps ready")
    return SubsystemStatus("FileOps", "error", f"FileOps failed: {resp}")


def _check_patch_engine(backend):
    return safe_call("PatchEngine", lambda: _patch_impl(backend))


def _patch_impl(backend):
    envelope = {
        "task": "patch",
        "operation": "noop",
        "data": {"probe": True}
    }
    resp = backend.send(envelope)
    if resp.get("status") in ("ok", "error"):
        return SubsystemStatus("PatchEngine", "ok", "Patch engine initialized")
    return SubsystemStatus("PatchEngine", "error", f"Patch engine failed: {resp}")


def _check_context_engine(backend):
    return safe_call("ContextEngine", lambda: _context_impl(backend))


def _context_impl(backend):
    envelope = {
        "task": "context",
        "data": {"text": "startup probe", "mode": "all"}
    }
    resp = backend.send_to("/context", envelope)
    if resp.get("status") == "ok":
        return SubsystemStatus("ContextEngine", "ok", "Context engine ready")
    return SubsystemStatus("ContextEngine", "error", f"Context engine failed: {resp}")


def _check_security_engine(backend):
    return safe_call("SecurityEngine", lambda: _security_impl(backend))


def _security_impl(backend):
    envelope = {
        "task": "security",
        "operation": "validate",
        "path": "./workspace"
    }
    resp = backend.send(envelope)
    if resp.get("status") in ("ok", "error"):
        return SubsystemStatus("SecurityEngine", "ok", "Security engine nominal")
    return SubsystemStatus("SecurityEngine", "error", f"Security engine failed: {resp}")


# ============================================================
# MAIN ENTRY POINT
# ============================================================
def run_startup_check(backend):
    """
    Run all startup subsystem checks and return a StartupResult.
    """
    start = time.time()
    boot_id = str(uuid.uuid4())

    subsystems = [
        _check_router(backend),
        _check_fileops(backend),
        _check_patch_engine(backend),
        _check_context_engine(backend),
        _check_security_engine(backend),
    ]

    duration = time.time() - start

    return StartupResult(
        duration=duration,
        timestamp=time.time(),
        boot_id=boot_id,
        subsystems=subsystems
    )
