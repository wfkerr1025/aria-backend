# backend/backend_contract.py

import time
import traceback
import os


class BackendContractViolation(Exception):
    """Raised when a backend module violates the ARIA Lite backend contract."""
    pass


# ============================================================
# LOGGING HELPERS
# ============================================================

def _log_backend_contract_error(module_name: str, message: str, tb: str | None = None):
    print("\n" + "=" * 60)
    print("[ARIA Backend Contract Violation]")
    print(f"Module: {module_name}")
    print(f" - {message}")
    if tb:
        print(tb)
    print("[Resolution Required]")
    print("=" * 60 + "\n")

    os.makedirs("logs", exist_ok=True)
    with open("logs/backend_contract_errors.log", "a", encoding="utf-8") as f:
        f.write(f"\n[{time.ctime()}] BACKEND CONTRACT VIOLATION in '{module_name}': {message}\n")
        if tb:
            f.write(tb + "\n")


def _log_backend_autocorrection(module_name: str, messages: list[str]):
    print("\n" + "=" * 60)
    print("[ARIA Backend Auto-Correction]")
    print(f"Module: {module_name}")
    for msg in messages:
        print(f" - {msg}")
    print("[Module successfully adapted]")
    print("=" * 60 + "\n")

    os.makedirs("logs", exist_ok=True)
    with open("logs/backend_contract_errors.log", "a", encoding="utf-8") as f:
        f.write(f"\n[{time.ctime()}] BACKEND AUTO-CORRECTION for '{module_name}':\n")
        for msg in messages:
            f.write(f" - {msg}\n")


# ============================================================
# CONTRACT VERIFICATION
# ============================================================

def verify_backend_module(app, module, module_name: str) -> bool:
    """
    Verify that a backend module satisfies the ARIA Lite backend contract.

    Contract (if module declares it):
        - REQUIRES: list of required attributes on app or module
        - FORBIDDEN: list of forbidden attributes on module

    If REQUIRES/FORBIDDEN are not declared, module is considered legacy and passes.
    """
    try:
        requires = getattr(module, "REQUIRES", None)
        forbidden = getattr(module, "FORBIDDEN", None)

        # No explicit contract → treat as legacy, allow
        if requires is None and forbidden is None:
            return True

        ok = True
        messages = []

        # Check required attributes
        if isinstance(requires, (list, tuple)):
            for attr in requires:
                has_on_module = hasattr(module, attr)
                has_on_app = hasattr(app, attr)
                if not (has_on_module or has_on_app):
                    ok = False
                    messages.append(f"Missing required attribute '{attr}' on app or module")

        # Check forbidden attributes
        if isinstance(forbidden, (list, tuple)):
            for attr in forbidden:
                if hasattr(module, attr):
                    ok = False
                    messages.append(f"Module illegally accesses forbidden attribute '{attr}'")

        if not ok:
            _log_backend_contract_error(module_name, "; ".join(messages))
        return ok

    except Exception as e:
        tb = traceback.format_exc()
        _log_backend_contract_error(module_name, f"Contract verification error: {e}", tb)
        return False


# ============================================================
# CONTRACT AUTO-CORRECTION
# ============================================================

def adapt_backend_module(app, module, module_name: str):
    """
    Attempt to auto-correct a backend module that violates the backend contract.

    Strategy:
        - Inject default REQUIRES/FORBIDDEN if missing
        - Remove forbidden attributes from module
        - Attach required attributes from app if possible
    """
    messages: list[str] = []

    try:
        # Ensure contract lists exist
        if not hasattr(module, "REQUIRES"):
            module.REQUIRES = ["backend_adapter", "router"]
            messages.append("Injected default REQUIRES list: ['backend_adapter', 'router']")

        if not hasattr(module, "FORBIDDEN"):
            module.FORBIDDEN = ["root", "window", "theme_manager", "chat_panel"]
            messages.append("Injected default FORBIDDEN list: ['root', 'window', 'theme_manager', 'chat_panel']")

        # Remove forbidden attributes
        forbidden = getattr(module, "FORBIDDEN", [])
        for attr in forbidden:
            if hasattr(module, attr):
                delattr(module, attr)
                messages.append(f"Removed forbidden attribute '{attr}' from module")

        # Attach required attributes from app if available
        requires = getattr(module, "REQUIRES", [])
        for attr in requires:
            if not hasattr(module, attr) and hasattr(app, attr):
                setattr(module, attr, getattr(app, attr))
                messages.append(f"Attached required attribute '{attr}' from app to module")

        _log_backend_autocorrection(module_name, messages)
        return module

    except Exception as e:
        tb = traceback.format_exc()
        _log_backend_contract_error(module_name, f"Contract adaptation error: {e}", tb)
        return module
