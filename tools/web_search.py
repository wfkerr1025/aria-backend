# backend/tools/web_search.py

from __future__ import annotations
import time
from typing import Dict, Any
from .http_fetch import http_fetch

from logger import get_logger

logger = get_logger(__name__)


def _normalize_data(raw_data: Any) -> Dict[str, Any]:
    """
    DuckDuckGo's Instant Answer API is expected to return a JSON object,
    but a malformed response — or a conversational, non-search-shaped
    query like "can you search the web?" — can come back with a bare
    string, list, number, or null at the top level instead. Calling
    .get() on any of those crashes with "'str' object has no attribute
    'get'" (the exact crash seen in production logs). This normalizes
    every possible shape into a real dict BEFORE any .get() call ever
    runs, so web_search() can never crash on a malformed response.
    """
    if isinstance(raw_data, dict):
        return raw_data
    if isinstance(raw_data, str) and raw_data.strip():
        # Still real text DuckDuckGo sent back — treat it as the
        # abstract rather than discarding it outright.
        return {"AbstractText": raw_data.strip()}
    # list, number, None, empty string, or anything else — nothing
    # usable to extract; format_search_reply() (backend/core/
    # tool_executor.py) already turns an empty dict here into a safe
    # "didn't get a clear answer" reply, never a crash.
    return {}


def web_search(query: str) -> Dict[str, Any]:
    """
    Web search tool for ARIA Lite.
    Uses DuckDuckGo Instant Answer API (no key required).
    Returns a normalized structure that the LLM engine
    and tool_execution pipeline can summarize cleanly.
    """

    start = time.monotonic()
    logger.debug("web_search invoked: query=%s", query)

    # ---------------------------------------------------------
    # Build API URL
    # ---------------------------------------------------------
    url = (
        f"https://api.duckduckgo.com/?q={query}"
        f"&format=json&no_redirect=1&no_html=1"
    )

    # ---------------------------------------------------------
    # Perform the HTTP request
    # ---------------------------------------------------------
    result = http_fetch(url)

    # If http_fetch failed, return the error directly
    if result.get("status") != "ok":
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.error(
            "web_search failed: query=%s elapsed_ms=%.2f error=%s",
            query, elapsed_ms, result.get("error", "Unknown error"),
        )
        return {
            "status": "error",
            "tool": "web_search",
            "query": query,
            "error": result.get("error", "Unknown error"),
        }

    # ---------------------------------------------------------
    # Extract useful fields
    # ---------------------------------------------------------
    raw_data = result.get("data")
    data = _normalize_data(raw_data)

    abstract = data.get("AbstractText") or None
    heading = data.get("Heading") or None
    related = data.get("RelatedTopics", []) or []
    source_url = data.get("AbstractURL") or None

    # ---------------------------------------------------------
    # Normalize related topics
    # DuckDuckGo returns nested structures; flatten them.
    # ---------------------------------------------------------
    normalized_related = []
    for item in related:
        if isinstance(item, dict):
            text = item.get("Text")
            first_url = item.get("FirstURL")
            if text or first_url:
                normalized_related.append({
                    "text": text,
                    "url": first_url,
                })

    elapsed_ms = (time.monotonic() - start) * 1000
    logger.info("web_search completed: query=%s elapsed_ms=%.2f", query, elapsed_ms)

    return {
        "status": "ok",
        "tool": "web_search",
        "query": query,

        # Core search fields
        "heading": heading,
        "summary": abstract,
        "source_url": source_url,
        "related": normalized_related,

        # Raw API response for debugging or future use — the true
        # unmodified value, not the normalized `data` above, so a
        # malformed response is still visible exactly as DuckDuckGo sent
        # it rather than hidden behind normalization.
        "raw": raw_data,

        # HTTP metadata from http_fetch
        "http": {
            "url": result.get("url"),
            "code": result.get("code"),
            "ok": result.get("ok"),
            "headers": result.get("headers"),
            "elapsed_ms": result.get("elapsed_ms"),
        },
    }
