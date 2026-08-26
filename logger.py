"""Centralized logging configuration for ARIA-Lite.

Usage:
    from logger import get_logger
    logger = get_logger(__name__)
"""

import logging
import os
from logging.handlers import RotatingFileHandler

_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_LOG_FILE = os.path.join(_LOG_DIR, "aria.log")

_ROOT_NAME = "aria"
_configured = False


def _configure_root():
    global _configured
    if _configured:
        return

    root = logging.getLogger(_ROOT_NAME)
    root.setLevel(logging.DEBUG)
    root.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        file_handler = RotatingFileHandler(
            _LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        root.warning("Could not open log file %s; file logging disabled.", _LOG_FILE)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger nested under the shared 'aria' root logger."""
    _configure_root()
    if name == "__main__" or not name:
        name = "main"
    return logging.getLogger(f"{_ROOT_NAME}.{name}")
