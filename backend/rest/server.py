# backend/rest/server.py

"""
REST API v1 entrypoint — Phase 1 scaffolding.

A standalone HTTP server, isolated from:
  - backend/ws_server.py's WebSocket server (unmodified, unaffected —
    this process doesn't import it and doesn't share its event loop)
  - backend/server.py's existing FastAPI app on port 5000 (unmodified,
    left exactly as it was)

Runs on its own port (default 8767) so it can be started, stopped, and
reasoned about independently of both. Wired into the launcher via
AriaLauncher/AriaLauncher/RestApiManager.cs (new, sibling to
BackendManager.cs) — see that file for the process-management side;
this file only needs to actually signal readiness, which it does the
same way backend/ws_server.py and backend/logging_server.py already do:
print a fixed READY token to stdout once actually listening, via a
FastAPI lifespan hook (mirroring backend/logging_server.py's identical
LOG_SERVER_READY pattern) rather than printing it before uvicorn.run()
is even called, which would fire regardless of whether the bind
actually succeeded.
"""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import asynccontextmanager

# Repo root on sys.path — same bootstrap backend/ws_server.py uses, needed
# because this file lives two directories under the repo root
# (backend/rest/server.py) and is meant to be runnable directly
# (`python backend/rest/server.py`).
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import uvicorn
from fastapi import FastAPI

from backend.rest.router import router as v1_router
from backend.logger import log as unified_log
from logger import get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # This is a SEPARATE process from backend/ws_server.py (see module
    # docstring) — Phase 2's plugin_registry/tool_registry module-level
    # state doesn't cross that process boundary, so GET /v1/plugins would
    # otherwise always report zero plugins from this process even after
    # ws_server.py's own startup loaded them. Same non-fatal convention
    # as ws_server.py's own plugin-load block.
    try:
        from backend.core import plugin_registry
        plugin_registry.load_all()
        logger.info("Plugins loaded for REST API process.")
    except Exception:
        logger.exception("Failed to load plugins for REST API process — continuing without them.")

    # AriaLauncher/AriaLauncher/RestApiManager.cs watches stdout for this
    # exact string (same convention as backend/ws_server.py's
    # WS_SERVER_READY and backend/logging_server.py's LOG_SERVER_READY) —
    # must stay a plain print, not go through logger, and must fire from
    # here (server actually listening) rather than before uvicorn.run().
    print("REST_API_READY", flush=True)
    logger.info("REST_API_READY printed.")
    yield


app = FastAPI(title="ARIA Lite REST API", version="1.0.0", lifespan=lifespan)
app.include_router(v1_router)


def parse_args() -> int:
    parser = argparse.ArgumentParser(description="ARIA-Lite REST API server")
    parser.add_argument(
        "--port",
        type=int,
        default=8767,
        help="Port to bind REST API server on (default: 8767)",
    )
    args = parser.parse_args()
    logger.info("CLI parsed port = %s", args.port)
    return args.port


if __name__ == "__main__":
    port = parse_args()
    logger.info("Starting REST API server on http://127.0.0.1:%s", port)
    unified_log("rest", "INFO", f"REST API server starting on port {port}")
    uvicorn.run(app, host="127.0.0.1", port=port)
