# utils/module_loader.py
import importlib
import traceback

from logger import get_logger

logger = get_logger(__name__)


class ModuleLoader:
    """
    Unified module loader for ARIA Lite.
    - Safe import of modules and attributes
    - Deterministic error reporting
    - Plug‑n‑play logger integration
    - Zero‑crash import pipeline
    """

    def __init__(self, logger=None, label="ModuleLoader"):
        self.logger = logger
        self.label = label

    # ============================================================
    # SAFE IMPORT
    # ============================================================
    def safe_import(self, module_path: str, attr_name: str | None = None):
        """
        Safely import a module and optionally an attribute/class.
        Unified behavior:
          - returns None on failure
          - logs error if logger is provided
          - prints deterministic error output
        """
        try:
            module = importlib.import_module(module_path)

            if attr_name:
                return getattr(module, attr_name)

            return module

        except Exception as e:
            tb = traceback.format_exc()
            msg = f"Failed to import {module_path}.{attr_name or ''}: {e}"

            logger.exception(f"[{self.label} ERROR] {msg}")

            if self.logger:
                self.logger.log_error(self.label, msg, tb)

            return None
