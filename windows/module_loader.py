# windows/module_loader.py
import importlib
import traceback


class ModuleLoader:
    """
    Unified module loader for ARIA Lite (window subsystem).
    - Safe import of modules and attributes
    - Deterministic error reporting
    - Logger-aware (optional)
    - Zero-crash import pipeline
    """

    def __init__(self, logger=None, label="ModuleLoader"):
        self.logger = logger
        self.label = label

    # ============================================================
    # SAFE IMPORT
    # ============================================================
    def safe_import(self, module_path: str, attr_name: str = None):
        """
        Safely import a module (and optionally an attribute/class from it).
        Returns None on failure instead of crashing ARIA.
        Unified behavior:
          - deterministic error output
          - optional logger integration
          - consistent return semantics
        """
        try:
            module = importlib.import_module(module_path)

            if attr_name:
                return getattr(module, attr_name)

            return module

        except Exception as e:
            tb = traceback.format_exc()
            msg = f"Failed to import {module_path}.{attr_name or ''}: {e}"

            print(f"[{self.label} ERROR] {msg}")
            print(tb)

            if self.logger:
                self.logger.log_error(self.label, msg, tb)

            return None
