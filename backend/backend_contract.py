# backend/backend_contract.py

import time
import traceback
import os

from logger import get_logger

logger = get_logger(__name__)


class BackendContractViolation(Exception):
    """Raised when a backend module violates the ARIA Lite backend contract."""
    pass


# ============================================================
# LOGGING HELPERS
# ============================================================

def _log_backend_contract_error(module_name: str, message: str, tb: str | None = None):
    logger.error(f"ERROR in {module_name}: {message}")

    logger.error("\n" + "=" * 60)
    logger.error("[ARIA Backend Contract Violation]")
    logger.error(f"Module: {module_name}")
    logger.error(f" - {message}")
    if tb:
        logger.error(tb)
    logger.error("[Resolution Required]")
    logger.error("=" * 60 + "\n")

    os.makedirs("logs", exist_ok=True)
    with open("logs/backend_contract_errors.log", "a", encoding="utf-8") as f:
        f.write(f"\n[{time.ctime()}] BACKEND CONTRACT VIOLATION in '{module_name}': {message}\n")
        if tb:
            f.write(tb + "\n")


def _log_backend_autocorrection(module_name: str, messages: list[str]):
    logger.debug(f"AUTOCORRECT {module_name}: {messages}")

    logger.info("\n" + "=" * 60)
    logger.info("[ARIA Backend Auto-Correction]")
    logger.info(f"Module: {module_name}")
    for msg in messages:
        logger.info(f" - {msg}")
    logger.info("[Module successfully adapted]")
    logger.info("=" * 60 + "\n")

    os.makedirs("logs", exist_ok=True)
    with open("logs/backend_contract_errors.log", "a", encoding="utf-8") as f:
        f.write(f"\n[{time.ctime()}] BACKEND AUTO-CORRECTION for '{module_name}':\n")
        for msg in messages:
            f.write(f" - {msg}\n")


# ============================================================
# CONTRACT VERIFICATION
# ============================================================

def verify_backend_module(app, module, module_name: str) -> bool:
    logger.debug(f"verify_backend_module() → {module_name}")

    try:
        requires = getattr(module, "REQUIRES", None)
        forbidden = getattr(module, "FORBIDDEN", None)

        # No explicit contract → treat as legacy, allow
        if requires is None and forbidden is None:
            logger.debug(f"{module_name} is legacy → allowed")
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
    logger.debug(f"adapt_backend_module() → {module_name}")

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
