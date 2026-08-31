# backend/tools/http_fetch.py
import json
import time

import requests

from logger import get_logger

logger = get_logger(__name__)

# A browser-shaped User-Agent, because several public JSON endpoints
# refuse anonymous clients outright. Yahoo's edge answers a request with
# no User-Agent with HTTP 429 "Edge: Too Many Requests" -- not a rate
# limit at all, despite the wording, since the very same request with a
# User-Agent returns 200 immediately.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

# How long one request may take.
#
# This was 10 seconds, and web_search's whole tool budget in
# tool_registry is also 10 seconds -- so a single unreachable host
# consumed the entire turn and the sandbox abandoned the tool before the
# remaining providers had run at all. Measured: GDELT was unreachable,
# took its full 10s, and web_search was cancelled 2.1 seconds before
# LangSearch returned ten good results into a turn that had already
# given up and answered "I could not retrieve current data".
#
# So one request's budget must stay well under the tool's. A provider
# that is merely slow still gets a fair chance; a provider that is down
# no longer spends everyone else's time.
DEFAULT_TIMEOUT_SECONDS = 6


def http_fetch(url, *, headers=None, json_body=None, timeout=DEFAULT_TIMEOUT_SECONDS):
    """
    Generic HTTP tool for ARIA Lite.
    Fetches JSON or text from any public API endpoint.

    The keyword arguments are additive; `headers` and `json_body` default
    to the previous behaviour exactly, so every existing call site is
    unaffected.

    `timeout` is per request, in seconds. A caller that is one of several
    providers racing to answer the same question should pass something
    shorter than the default -- being unreachable is not a reason to cost
    the others their turn.

    `headers` merges over the defaults, for APIs that authenticate with
    one -- Brave Search takes its key as X-Subscription-Token rather than
    a query parameter, which is the better design and which a URL-only
    fetcher cannot express.

    `json_body` switches the request to POST with that body, for APIs that
    take their query that way; Tavily's search endpoint does. The
    alternative was a provider reaching for `requests` directly, which
    would duplicate the User-Agent and the HTTP-error handling here -- and
    would quietly miss the next fix made to either.
    """

    start = time.monotonic()
    logger.debug("http_fetch invoked: url=%s method=%s", url,
                 "POST" if json_body is not None else "GET")

    try:
        request_headers = dict(_HEADERS)
        if headers:
            request_headers.update(headers)

        if json_body is not None:
            response = requests.post(url, timeout=timeout, headers=request_headers, json=json_body)
        else:
            response = requests.get(url, timeout=timeout, headers=request_headers)
        content_type = response.headers.get("Content-Type", "")

        # An HTTP error is not a successful fetch, however well-formed the
        # body is. Without this, Yahoo's "Edge: Too Many Requests" came
        # back as status "ok" with the refusal as the payload, and every
        # caller downstream had to work out for itself that a string
        # saying "Too Many Requests" was not data.
        if response.status_code >= 400:
            logger.warning(
                "http_fetch refused: url=%s code=%s body=%s",
                url, response.status_code, response.text[:120],
            )
            return {
                "status": "error",
                "url": url,
                "code": response.status_code,
                "error": f"HTTP {response.status_code}: {response.text[:200]}",
                "data": None,
            }

        # Parse JSON if possible.
        #
        # AN EMPTY BODY IS AN ANSWER, NOT A PARSE FAILURE
        # A 200 with no content and a JSON content-type is legal and
        # common -- it is how an endpoint says "yes" when there is
        # nothing to say. response.json() raises on it, and because
        # that raise happened inside the try below, a SUCCESSFUL
        # request came back as {"status": "error"}.
        #
        # Found the hard way: Ludo.ai's /auth/validate-api-key answers
        # a valid key with exactly this, so ARIA told a user with a
        # working key that its server could not be reached.
        #
        # A body that is present but malformed is still an error. That
        # is a server saying something broken, which is worth knowing.
        if "application/json" in content_type:
            body = response.text or ""
            data = json.loads(body) if body.strip() else None
        else:
            data = response.text

        elapsed_ms = (time.monotonic() - start) * 1000
        logger.info(
            "http_fetch completed: url=%s code=%s elapsed_ms=%.2f",
            url, response.status_code, elapsed_ms,
        )

        return {
            "status": "ok",
            "url": url,
            "code": response.status_code,
            "data": data
        }

    except Exception as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.exception(
            "http_fetch failed: url=%s elapsed_ms=%.2f error=%s",
            url, elapsed_ms, e,
        )
        return {
            "status": "error",
            "url": url,
            "error": str(e)
        }
