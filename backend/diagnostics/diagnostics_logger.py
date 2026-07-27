# backend/diagnostics/diagnostics_logger.py
import logging
from pathlib import Path

LOG_PATH = Path("logs/aria_lite.log")

# =========================================================
# LOGGER INITIALIZATION (SAFE + IDEMPOTENT)
# =========================================================
logger = logging.getLogger("ARIA.Diagnostics")
logger.setLevel(logging.INFO)

if not logger.handlers:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"
    )
    handler.setFormatter(formatter)

    logger.addHandler(handler)


# =========================================================
# STARTUP RESULT LOGGING
# =========================================================
def log_startup_result(result):
    """
    Log the outcome of ARIA Lite's startup self-test.
    Accepts a StartupResult instance.
    """

    # Determine severity
    if result.errors_count() > 0:
        level = logging.ERROR
        status = "ERROR"
    elif result.warnings_count() > 0:
        level = logging.WARNING
        status = "WARNING"
    else:
        level = logging.INFO
        status = "OK"

    # Summary line
    msg = (
        f"Startup {status} | boot_id={result.boot_id} | "
        f"duration={result.duration:.2f}s | "
        f"subs={len(result.subsystems)} | "
        f"warnings={result.warnings_count()} | "
        f"errors={result.errors_count()}"
    )
    logger.log(level, msg)

    # Subsystem details
    for subsystem in result.subsystems:
        logger.info(
            f"Subsystem {subsystem.name}: "
            f"status={subsystem.status} "
            f"message={subsystem.message} "
            f"boot_id={result.boot_id}"
        )
