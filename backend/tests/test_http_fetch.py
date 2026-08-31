"""The shared HTTP client, against a real server.

WHY A REAL SERVER
-----------------
The bug these were written for could not be caught by a mock. A mocked
requests.get returns whatever the test says it returns, so a test would
have asserted my assumption about what a 200-with-no-body looks like --
and my assumption was the thing that was wrong.

So these start an actual HTTP server on localhost and make actual
requests to it. It costs a few milliseconds and it tests the code.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tools.http_fetch import http_fetch


class _Handler(BaseHTTPRequestHandler):
    """Answers according to the path, so one server covers every case."""

    def do_GET(self):  # noqa: N802 - the name is BaseHTTPRequestHandler's
        cases = {
            # A valid-key answer: 200, JSON content type, no body at all.
            # This is what Ludo.ai's /auth/validate-api-key returns, and
            # what http_fetch used to turn into "status": "error".
            "/empty-json": (200, "application/json", b""),
            "/whitespace-json": (200, "application/json", b"   \n"),
            "/good-json": (200, "application/json", b'{"ok": true}'),
            "/broken-json": (200, "application/json", b"{not json"),
            "/plain-text": (200, "text/plain", b"hello"),
            "/refused": (403, "application/json", b'{"message": "no"}'),
            "/empty-204": (204, "application/json", b""),
        }
        code, content_type, body = cases.get(
            self.path, (404, "text/plain", b"nope"))

        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, *args):
        """Quiet. The suite's output is not a web server access log."""


@pytest.fixture(scope="module")
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


# ======================================================
# The bug
# ======================================================

def test_an_empty_json_body_is_a_success(server):
    """200 with no content is an endpoint saying "yes".

    Ludo.ai's key validator answers exactly this. http_fetch called
    response.json() on it, raised, and reported a successful request as
    an error -- so ARIA told a user with a working key that Ludo.ai
    could not be reached.
    """
    answer = http_fetch(f"{server}/empty-json")

    assert answer["status"] == "ok"
    assert answer["code"] == 200
    assert answer["data"] is None
    assert "error" not in answer


def test_a_whitespace_only_body_is_also_a_success(server):
    answer = http_fetch(f"{server}/whitespace-json")

    assert answer["status"] == "ok"
    assert answer["data"] is None


def test_a_204_with_no_content_is_a_success(server):
    answer = http_fetch(f"{server}/empty-204")

    assert answer["status"] == "ok"
    assert answer["data"] is None


# ======================================================
# What must not have changed
# ======================================================

def test_real_json_still_parses(server):
    answer = http_fetch(f"{server}/good-json")

    assert answer["status"] == "ok"
    assert answer["data"] == {"ok": True}


def test_a_malformed_body_is_still_an_error(server):
    """The fix is for an ABSENT body, not a broken one. A server saying
    something malformed is saying something is wrong, and swallowing
    that would trade one silent failure for another."""
    answer = http_fetch(f"{server}/broken-json")

    assert answer["status"] == "error"
    assert "error" in answer


def test_plain_text_still_comes_back_as_text(server):
    answer = http_fetch(f"{server}/plain-text")

    assert answer["status"] == "ok"
    assert answer["data"] == "hello"


def test_an_http_error_is_still_an_error(server):
    answer = http_fetch(f"{server}/refused")

    assert answer["status"] == "error"
    assert answer["code"] == 403


def test_a_host_that_is_not_there_reports_why(server):
    """The reason matters: it is what tells a DNS failure from a
    certificate failure from a refused connection."""
    answer = http_fetch("http://127.0.0.1:1/nothing", timeout=3)

    assert answer["status"] == "error"
    assert answer.get("code") is None
    assert answer["error"], "a failed request must say what failed"


def test_headers_are_merged_over_the_defaults(server):
    """The Ludo.ai key rides in a header, so this cannot quietly stop
    working."""
    seen = {}

    class Recorder(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.update({k.lower(): v for k, v in self.headers.items()})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        http_fetch(f"http://127.0.0.1:{httpd.server_port}/",
                   headers={"Authorization": "ApiKey secret-value"})
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert seen.get("authorization") == "ApiKey secret-value"
    # The default User-Agent survives a caller adding its own header.
    assert "mozilla" in seen.get("user-agent", "").lower()
