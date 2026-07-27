# core/architecture_adapter.py

"""
ARIA Lite Unified Architecture Auto-Correction Layer

This module provides:
- Contract-aware verification
- Safe auto-correction for common mismatches
- Unified logging for what was fixed and why

It is designed to be called from:
- ARIALiteApp (for backend/LLM/diagnostics/tools modules)
- ARIAWindow (for UI modules)
"""

from types import ModuleType


def log_header(title: str, module_name: str):
    print(f"\n[{title}]")
    print(f"Module: {module_name}")


def log_footer(success: bool):
    if success:
        print("[Module successfully adapted]\n")
    else:
        print("[Resolution Required]\n")


# ============================================================
# APP-LEVEL CONTRACT ENFORCEMENT / AUTO-CORRECTION
# ============================================================

def verify_app_module(app, module: ModuleType, module_name: str) -> bool:
    """
    Check that a module can safely integrate with ARIALiteApp.
    Returns True if compliant, False otherwise.
    """
    errors = []

    contract = getattr(app, "contract", None)
    if contract is None:
        # No contract defined, nothing to verify
        return True

    # Required app attributes
    for attr in contract.get("required_app", []):
        if not hasattr(app, attr):
            errors.append(f"App missing required attribute '{attr}'")

    # Forbidden attributes on module
    for forbidden in contract.get("forbidden", []):
        if hasattr(module, forbidden):
            errors.append(
                f"Module '{module_name}' illegally accesses forbidden attribute '{forbidden}'"
            )

    if errors:
        log_header("ARIA Lite Contract Violation", module_name)
        for err in errors:
            print(f" - {err}")
        log_footer(False)
        return False

    return True


def adapt_app_module(app, module: ModuleType, module_name: str) -> bool:
    """
    Attempt safe auto-corrections for common app-level mismatches.
    Returns True if any correction was applied, False otherwise.
    """
    corrections = []

    # 1) Legacy backend_adapter → backend
    if hasattr(module, "backend_adapter") and not hasattr(module, "backend"):
        module.backend = app.backend
        delattr(module, "backend_adapter")
        corrections.append("Mapped 'backend_adapter' → 'backend' and removed legacy attribute")

    # 2) Missing apply_theme (generic module)
    if not hasattr(module, "apply_theme"):
        module.apply_theme = lambda theme: None
        corrections.append("Injected default 'apply_theme'")

    # 3) Legacy backend send_packet → backend.send_to
    if hasattr(module, "send_packet") and callable(getattr(module, "send_packet")):
        original = module.send_packet

        def _send_packet(route, payload=None):
            # Try to use unified backend adapter
            return app.backend.send_to(route, payload)

        module.send_packet = _send_packet
        corrections.append("Mapped legacy 'send_packet' → 'backend.send_to'")

    # 4) Legacy theme list_themes → theme_manager.get_available_themes
    if hasattr(module, "list_themes") and callable(getattr(module, "list_themes", None)):
        module.list_themes = app.theme_manager.get_available_themes
        corrections.append("Redirected 'list_themes' → 'theme_manager.get_available_themes'")

    # 5) Inject default REQUIRES/FORBIDDEN if missing
    if not hasattr(module, "REQUIRES"):
        module.REQUIRES = ["backend", "theme_manager", "window"]
        corrections.append("Injected default REQUIRES list: ['backend', 'theme_manager', 'window']")

    if not hasattr(module, "FORBIDDEN"):
        module.FORBIDDEN = ["interface", "backend_adapter"]
        corrections.append("Injected default FORBIDDEN list: ['interface', 'backend_adapter']")

    # 6) Remove forbidden attributes if present
    contract = getattr(app, "contract", None)
    if contract is not None:
        for forbidden in contract.get("forbidden", []):
            if hasattr(module, forbidden):
                delattr(module, forbidden)
                corrections.append(f"Removed forbidden attribute '{forbidden}' from module")

    if corrections:
        log_header("ARIA Lite Auto-Correction", module_name)
        for c in corrections:
            print(f" - {c}")
        log_footer(True)
        return True

    return False


# ============================================================
# WINDOW-LEVEL CONTRACT ENFORCEMENT / AUTO-CORRECTION
# ============================================================

def verify_window_module(window, module: ModuleType, module_name: str) -> bool:
    """
    Check that a UI module integrates safely with ARIAWindow.
    Returns True if compliant, False otherwise.
    """
    errors = []

    ui_contract = getattr(window, "ui_contract", None)
    if ui_contract is None:
        return True

    # Required window attributes
    for attr in ui_contract.get("required", []):
        if not hasattr(window, attr):
            errors.append(f"Window missing required attribute '{attr}'")

    # Forbidden attributes on module
    for forbidden in ui_contract.get("forbidden", []):
        if hasattr(module, forbidden):
            errors.append(
                f"Module '{module_name}' illegally accesses forbidden window attribute '{forbidden}'"
            )

    if errors:
        log_header("ARIA Lite UI Contract Violation", module_name)
        for err in errors:
            print(f" - {err}")
        log_footer(False)
        return False

    return True


def adapt_window_module(window, module: ModuleType, module_name: str) -> bool:
    """
    Attempt safe auto-corrections for common UI mismatches.
    Returns True if any correction was applied, False otherwise.
    """
    corrections = []

    # 1) Missing apply_theme
    if not hasattr(module, "apply_theme"):
        module.apply_theme = lambda theme: None
        corrections.append("Injected default 'apply_theme' on UI module")

    # 2) Missing apply_scale
    if not hasattr(module, "apply_scale"):
        module.apply_scale = lambda scale: None
        corrections.append("Injected default 'apply_scale' on UI module")

    # 3) Redirect forbidden root access to window.root
    if hasattr(module, "root") and module.root is None:
        module.root = window.root
        corrections.append("Redirected 'root' → window.root")

    # 4) Remove forbidden attributes if present
    ui_contract = getattr(window, "ui_contract", None)
    if ui_contract is not None:
        for forbidden in ui_contract.get("forbidden", []):
            if hasattr(module, forbidden):
                delattr(module, forbidden)
                corrections.append(f"Removed forbidden window attribute '{forbidden}' from module")

    if corrections:
        log_header("ARIA Lite UI Auto-Correction", module_name)
        for c in corrections:
            print(f" - {c}")
        log_footer(True)
        return True

    return False
