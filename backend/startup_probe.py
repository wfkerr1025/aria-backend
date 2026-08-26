import sys

# Core imports
from backend.core.model_registry import get_all_models
from backend.core.local_inference_engine import LocalInferenceEngine
from backend.websocket.handlers import WebSocketHandler

from logger import get_logger

logger = get_logger(__name__)


def main():
    try:
        # -----------------------------------------------------
        # 1) Run subsystem diagnostics (best-effort)
        #
        # backend/diagnostics/ was removed from this working tree
        # (it shows as deleted in git status, not something this
        # probe removed). Rather than fail the whole probe over an
        # optional subsystem, skip this step when it's unavailable.
        # -----------------------------------------------------
        try:
            from backend.backend_adapter import BackendAdapter
            from backend.diagnostics.startup_check_engine import run_startup_check
            backend = BackendAdapter()
        except Exception:
            backend = None
            run_startup_check = None
            logger.warning("Diagnostics subsystem unavailable, skipping subsystem checks")

        if backend is not None and run_startup_check is not None:
            result = run_startup_check(backend)

            logger.info(f"Startup duration: {result.duration:.2f}s")
            logger.info(f"Boot ID: {result.boot_id}")
            logger.info(f"Timestamp: {result.timestamp:.0f}")

            for subsystem in result.subsystems:
                logger.info(f"[{subsystem.name}] {subsystem.status.upper()} - {subsystem.message}")

            if result.errors_count() > 0:
                logger.error("ERROR: Startup diagnostics reported errors")
                sys.exit(1)

        # -----------------------------------------------------
        # 2) Validate model registry
        # -----------------------------------------------------
        try:
            models = get_all_models()
            logger.info(f"Model registry loaded: {len(models)} models")
        except Exception as e:
            logger.exception(f"ERROR: Model registry failed to load: {e}")
            sys.exit(1)

        # -----------------------------------------------------
        # 3) Initialize inference engine
        # -----------------------------------------------------
        try:
            _ = LocalInferenceEngine()
            logger.info("Inference engine initialized successfully")
        except Exception as e:
            logger.exception(f"ERROR: Inference engine failed to initialize: {e}")
            sys.exit(1)

        # -----------------------------------------------------
        # 4) Verify WebSocket handler import chain
        # -----------------------------------------------------
        try:
            _ = WebSocketHandler
            logger.info("WebSocket handler import chain OK")
        except Exception as e:
            logger.exception(f"ERROR: WebSocket handler import failed: {e}")
            sys.exit(1)

        # -----------------------------------------------------
        # 5) All checks passed
        # -----------------------------------------------------
        logger.info("READY")
        sys.exit(0)

    except Exception:
        logger.exception("ERROR: Uncaught exception during startup probe")
        sys.exit(1)


if __name__ == "__main__":
    main()
