import asyncio
import os
import sys
import argparse
import websockets
from websockets.server import WebSocketServerProtocol

# ======================================================
# Working directory setup
# ======================================================
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from logger import get_logger

logger = get_logger(__name__)

# ======================================================
# UTF‑8 output for console logs
# ======================================================
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

logger.info("=== PYTHON BACKEND START ===")
logger.info("ROOT = %s", ROOT)
logger.info("cwd = %s", os.getcwd())
logger.info("Python version = %s", sys.version)

# ======================================================
# Phase 3 memory & knowledge system (backend/aria_memory_db.py)
#
# Creates aria_memory.db and every Phase 3 table/index if they do not
# exist yet. Runs here, before the handler import below, so that any
# memory read/write reached from a client request finds a ready schema.
# init_db() is idempotent (all CREATEs are IF NOT EXISTS), so a re-exec
# by BackendWatchdog simply no-ops through it.
# ======================================================
try:
    from backend.aria_memory_db import init_db, DB_PATH
    init_db()
    logger.info("Memory database ready at %s", DB_PATH)
except Exception:
    logger.exception("Failed to initialize memory database — memory features will be unavailable.")

# ======================================================
# Load cloud provider API keys from OS secure storage
#
# Must run BEFORE anything imports backend.llm.providers.provider_registry
# (WebSocketHandler below does, transitively, via provider_router) —
# provider_registry instantiates every Provider() once at import time,
# reading its API key via os.getenv(...) in __init__. Populating
# os.environ here first means providers configured through the settings
# UI (backend.core.key_manager) are live from the very first request.
# ======================================================
try:
    from backend.core.key_manager import sync_provider_keys_to_env
    sync_provider_keys_to_env()
    from backend.core.module_manager import sync_module_keys_to_env
    sync_module_keys_to_env()
    logger.info("Synced provider/module API keys from OS secure storage.")
except Exception:
    logger.exception("Failed to sync provider/module keys from secure storage — continuing without them.")

# ======================================================
# Hot-load plugins (plugins/*, via backend.core.plugin_registry)
#
# This is a DIFFERENT process/lifecycle from the desktop app's own
# plugin loading (core/plugin_loader.py, called from root app.py) — this
# backend process has never loaded plugins/plugin_manager.py's plugins
# automatically before now. Non-fatal on any failure, same convention as
# the key-sync block above: a broken/missing plugin must never prevent
# this backend from starting and printing WS_SERVER_READY.
# ======================================================
try:
    from backend.core import plugin_registry
    _plugin_load_result = plugin_registry.load_all()
    logger.info("Plugins loaded: %s", _plugin_load_result)
except Exception:
    logger.exception("Failed to load plugins — continuing without them.")

# ======================================================
# Batch 4 — start the backend watchdog (backend.core.backend_watchdog).
# Captures the current (already self-healed — see mode_manager.py)
# persisted routing state as this process's starting last-known-good
# snapshot. Every other backend.core module this process depends on
# (provider_config, tool_registry's built-in tools, model_selector,
# weather_provider) already reinitializes naturally just by this file
# importing them above/below — a real process restart
# (BackendWatchdog.restart_backend_process(), an os.execv() re-exec) is
# a brand-new interpreter run of this exact script from the top, so
# nothing here needs its own separate "on restart" hook.
# ======================================================
try:
    from backend.core.backend_watchdog import watchdog as _backend_watchdog
    _backend_watchdog.start()
    logger.info("BackendWatchdog started.")
except Exception:
    logger.exception("Failed to start BackendWatchdog — continuing without it.")

# ======================================================
# Import WebSocket handler
# ======================================================
try:
    from backend.websocket.handlers import WebSocketHandler
    logger.info("Imported WebSocketHandler successfully.")
except Exception:
    logger.exception("Crash during import of WebSocketHandler.")
    raise

# ======================================================
# Parse CLI args (dynamic port support)
# ======================================================
def parse_args() -> int:
    parser = argparse.ArgumentParser(description="ARIA-Lite WebSocket server")
    parser.add_argument(
        "--port",
        type=int,
        default=8766,
        help="Port to bind WebSocket server on (default: 8766)",
    )
    args = parser.parse_args()
    logger.info("CLI parsed port = %s", args.port)
    return args.port

WS_HOST = "127.0.0.1"
WS_PORT = parse_args()

# ======================================================
# Handle each client connection
# ======================================================
async def handle_client(websocket: WebSocketServerProtocol) -> None:
    logger.info("Client connected.")

    handler = WebSocketHandler(websocket)

    try:
        await handler.handle()
    except websockets.exceptions.ConnectionClosed:
        logger.info("Client disconnected.")
    except Exception:
        logger.exception("Exception in client handler.")

# ======================================================
# Start WebSocket server
# ======================================================
async def start_ws_server() -> None:
    logger.info("Starting WebSocket server on ws://%s:%s", WS_HOST, WS_PORT)

    try:
        # compression="deflate" is already this library's default (permessage-
        # deflate) — passed explicitly so it stays true regardless of future
        # library defaults, and so it's visible here rather than implicit.
        # Negotiation is per the WebSocket extension handshake: a peer that
        # doesn't support it (e.g. an older `ws` client) simply connects
        # without compression instead of failing, so this has no fallback
        # code to write by hand.
        server = await websockets.serve(handle_client, WS_HOST, WS_PORT, compression="deflate")
        logger.debug("websockets.serve() context entered.")
    except OSError:
        logger.exception("Failed to bind port %s.", WS_PORT)
        raise

    # Readiness signal for the launcher (AriaLauncher watches stdout for this
    # exact string) — must stay a plain print to stdout, not go through logger.
    print("WS_SERVER_READY", flush=True)
    logger.info("WS_SERVER_READY printed.")

    # Keep server alive until event loop stops
    try:
        await asyncio.Future()
    finally:
        logger.info("Shutting down WebSocket server...")
        server.close()
        await server.wait_closed()
        logger.info("Server shut down cleanly.")

# ======================================================
# MAIN ENTRY POINT
# ======================================================
if __name__ == "__main__":
    try:
        logger.info("Starting backend bootstrap...")
        asyncio.run(start_ws_server())
    except Exception:
        logger.exception("Backend crashed during startup.")
