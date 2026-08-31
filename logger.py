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


class _SharedRotatingFileHandler(RotatingFileHandler):
    """A rotating log that more than one process can hold open.

    On Windows a file cannot be renamed while another process has it
    open, so when the app and a test run share aria.log, rotation
    raises WinError 32. RotatingFileHandler catches that internally and
    DROPS the record -- so the moment the log reaches 5MB, ARIA quietly
    stops recording anything while printing a stack trace per line.

    That is not hypothetical. A Ludo.ai failure was diagnosable only
    from the launcher's captured stderr, because the log that should
    have held it had been discarding lines for hours.

    So a rollover that cannot happen is not an error: the handler keeps
    writing to the file it already has. The log grows past its limit
    until the other process lets go, which is a far smaller problem
    than losing the lines. It is attempted once and then left alone,
    because retrying on every record turns one failed rename into
    thousands.
    """

    _rollover_blocked = False

    def shouldRollover(self, record):  # noqa: N802 - the base class's name
        if self._rollover_blocked:
            return False
        return super().shouldRollover(record)

    def doRollover(self):  # noqa: N802 - the base class's name
        try:
            super().doRollover()
        except OSError:
            self._rollover_blocked = True
            # Reopen if the failure closed the stream, so the next
            # record is written rather than lost.
            if self.stream is None:
                self.stream = self._open()


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
        file_handler = _SharedRotatingFileHandler(
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
