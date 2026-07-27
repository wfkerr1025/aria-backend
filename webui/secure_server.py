#!/usr/bin/env python3
import os
import logging
import signal
import threading
from http.server import SimpleHTTPRequestHandler, HTTPServer
from functools import partial
from urllib.parse import urlparse

# Configuration
HOST = "0.0.0.0"
PORT = 3000

# Document root is the webui folder (script directory)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logging.info("Starting secure_server with document root: %s", SCRIPT_DIR)

class CSPHandler(SimpleHTTPRequestHandler):
    # Add Content Security Policy header and simple request logging
    def end_headers(self):
        self.send_header("Content-Security-Policy", "script-src 'self'")
        super().end_headers()

    def log_message(self, format, *args):
        # Use logging module instead of printing
        logging.info("%s - - %s", self.client_address[0], format % args)

    def do_GET(self):
        # Health endpoint
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        return super().do_GET()

def run_server():
    Handler = partial(CSPHandler, directory=SCRIPT_DIR)
    server = HTTPServer((HOST, PORT), Handler)

    def _shutdown(signum, frame):
        logging.info("Shutdown signal received (%s). Stopping server.", signum)
        # Shutdown in a separate thread to avoid blocking signal handler
        threading.Thread(target=server.shutdown).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        logging.info("Listening on http://%s:%d", HOST, PORT)
        server.serve_forever()
    except Exception as e:
        logging.exception("Server error: %s", e)
    finally:
        server.server_close()
        logging.info("Server stopped")

if __name__ == "__main__":
    run_server()
