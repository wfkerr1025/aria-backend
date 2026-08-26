# backend/tools/http_fetch.py
import time
import requests

from logger import get_logger

logger = get_logger(__name__)

def http_fetch(url):
    """
    Generic HTTP GET tool for ARIA Lite.
    Fetches JSON or text from any public API endpoint.
    """

    start = time.monotonic()
    logger.debug("http_fetch invoked: url=%s", url)

    try:
        response = requests.get(url, timeout=10)
        content_type = response.headers.get("Content-Type", "")

        # Parse JSON if possible
        if "application/json" in content_type:
            data = response.json()
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
