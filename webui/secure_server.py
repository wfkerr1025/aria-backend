#!/usr/bin/env python3
import os
import sys
import signal
import threading
from http.server import SimpleHTTPRequestHandler, HTTPServer
from functools import partial
from urllib.parse import urlparse

# ======================================================
# Configuration
# ======================================================
HOST = "0.0.0.0"
PORT = 3000

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from logger import get_logger

logger = get_logger(__name__)

logger.info("Secure server starting with document root: %s", SCRIPT_DIR)

# ======================================================
# CSP Handler
# ======================================================
class CSPHandler(SimpleHTTPRequestHandler):

    def end_headers(self):
        logger.debug("Injecting Content-Security-Policy header.")
        self.send_header("Content-Security-Policy", "script-src 'self'")
        super().end_headers()

    def log_message(self, format, *args):
        msg = format % args
        logger.info("%s - - %s", self.client_address[0], msg)

    def do_GET(self):
        parsed = urlparse(self.path)
        logger.debug("GET %s", parsed.path)

        if parsed.path == "/healthz":
            logger.debug("Health check endpoint hit.")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        return super().do_GET()

# ======================================================
# Server Runner
# ======================================================
def run_server():
    logger.info("Binding HTTP server on http://%s:%s", HOST, PORT)

    Handler = partial(CSPHandler, directory=SCRIPT_DIR)
    server = HTTPServer((HOST, PORT), Handler)

    def _shutdown(signum, frame):
        logger.info("Shutdown signal received (%s). Stopping server.", signum)
        threading.Thread(target=server.shutdown).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        logger.info("Listening on http://%s:%s", HOST, PORT)
        server.serve_forever()
    except Exception:
        logger.exception("Server error.")
    finally:
        server.server_close()
        logger.info("Server stopped cleanly.")

# ======================================================
# Main Entry
# ======================================================
if __name__ == "__main__":
    logger.info("Secure server main entry invoked.")
    run_server()
