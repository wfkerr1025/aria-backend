from http.server import SimpleHTTPRequestHandler, HTTPServer

class CSPHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Content-Security-Policy", "script-src 'self'")
        super().end_headers()

HTTPServer(("0.0.0.0", 3000), CSPHandler).serve_forever()
