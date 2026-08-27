# tools/web_search.py

"""The web_search tool, backed by providers that can answer the question.

DuckDuckGo's Instant Answer API was the whole backend, and it cannot serve
the questions this assistant is most often asked. Measured against it:

    stock price of Microsoft        -> {"Abstract":"","AbstractSource":"",...}
    latest news about AI regulation -> {"Abstract":"","AbstractSource":"",...}
    current EUR/USD exchange rate   -> {"Abstract":"","AbstractSource":"",...}

An empty envelope every time. Worse, that envelope was returned as the
summary, so an empty result arrived downstream looking like a finding --
the shape the evidence layer later had to learn to reject.

Now each query is offered to providers that declare whether they can serve
it, and only they make a call. If none of them returns anything, that is a
hard miss and it is reported as one. Nothing here writes prose about what
was not found.

The return shape is unchanged, so every existing consumer keeps working:
heading / summary / source_url / related / raw, plus an `items` list
carrying the full per-item structure for the normalizer that knows how to
read it.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from logger import get_logger

logger = get_logger(__name__)

# What a hard miss says. Deliberately plain and deliberately not a
# sentence about the subject: the one thing that must not happen when a
# lookup finds nothing is text that reads like a finding.
NO_RESULTS = "No results found."


def _tiers():
    """Imported lazily so a broken provider cannot break tool import."""
    from .providers import FALLBACK_PROVIDERS, PRIMARY_PROVIDERS

    return PRIMARY_PROVIDERS, FALLBACK_PROVIDERS


def _run(providers, query: str, collected: list, seen_urls: set, seen_titles: set) -> None:
    """Append each provider's items to `collected`, in provider order.

    A provider that raises is logged and skipped. One misbehaving backend
    must not cost the user the answers the others found -- and it must not
    turn a partial success into an exception either.
    """
    for provider in providers:
        name = getattr(provider, "PROVENANCE", getattr(provider, "__name__", "?"))
        try:
            items = provider.lookup(query) or []
        except Exception:
            logger.exception("web_search: provider %s failed", name)
            continue

        for item in items:
            if not isinstance(item, dict):
                continue
            snippet = str(item.get("snippet") or "").strip()
            if not snippet:
                # A result with no text is not a result. Carrying it would
                # put an empty bullet under a heading claiming findings.
                continue

            # One story, syndicated, reaches several providers. Keeping
            # every copy spends the answer's budget saying the same thing
            # three times, and reads to a model as three sources agreeing.
            # First occurrence wins, which is why provider order is the
            # preference order.
            link = (item.get("url") or "").strip().rstrip("/").lower()
            title_key = " ".join(str(item.get("title") or snippet).lower().split())
            if (link and link in seen_urls) or (title_key and title_key in seen_titles):
                continue
            if link:
                seen_urls.add(link)
            if title_key:
                seen_titles.add(title_key)

            collected.append({
                "title": (item.get("title") or None),
                "snippet": snippet,
                "url": item.get("url") or None,
                "timestamp": item.get("timestamp") or None,
                "provenance": item.get("provenance") or name,
                # Renumbered across providers so ranks are unique and
                # ordering is total; each provider's own order is kept.
                "rank": len(collected) + 1,
            })


def _collect(query: str) -> list[dict]:
    """The specialists' items, or the general web when they found none.

    Two tiers, not one list. The specialists run first and answer the
    questions they own; the general-web tier runs only if they came back
    with nothing at all.

    That ordering is the whole news fallback. A news question the Times
    answered does not also get a page of search results -- one dated
    abstract from a named publisher is better evidence than three links
    about the same story. A news question nobody answered, because no key
    is configured, reaches the web instead of failing outright.

    Finance and FX never fall through: their questions have a better
    answer available, so the general provider declines them itself
    (web_langsearch.applies_to) rather than being gated here. A quote
    that failed to arrive is reported as missing, not replaced with an
    article about the price.
    """
    collected: list[dict] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()

    primary, fallback = _tiers()
    _run(primary, query, collected, seen_urls, seen_titles)

    if not collected:
        logger.info("web_search: no specialist answered %s; trying the web", query)
        _run(fallback, query, collected, seen_urls, seen_titles)

    return collected


def web_search(query: str) -> Dict[str, Any]:
    """Look `query` up with whichever providers can serve it.

    Always returns a dict, never raises. `status` is "ok" whether or not
    anything was found -- the tool ran successfully either way, and a
    lookup that found nothing is not an error. Emptiness is reported by
    `items` being empty and `summary` being the hard-miss line, which the
    evidence layer already treats as unusable.
    """
    start = time.monotonic()
    logger.debug("web_search invoked: query=%s", query)

    items = _collect(query)
    elapsed_ms = (time.monotonic() - start) * 1000

    if not items:
        logger.info(
            "web_search: no provider returned results for %s (%.0fms)", query, elapsed_ms
        )
        return {
            "status": "ok",
            "tool": "web_search",
            "query": query,
            "heading": None,
            "summary": NO_RESULTS,
            "source_url": None,
            "related": [],
            "items": [],
            "providers": [],
            "raw": None,
        }

    primary = items[0]
    logger.info(
        "web_search completed: query=%s items=%d elapsed_ms=%.2f",
        query, len(items), elapsed_ms,
    )

    return {
        "status": "ok",
        "tool": "web_search",
        "query": query,
        # The legacy fields, kept so nothing that reads them has to change.
        "heading": primary.get("title"),
        "summary": primary.get("snippet"),
        "source_url": primary.get("url"),
        "timestamp": primary.get("timestamp"),
        "related": [
            {"text": item["snippet"], "url": item.get("url")}
            for item in items[1:]
        ],
        # The full structure, for the normalizer that reads per-item
        # titles and timestamps rather than reconstructing them.
        "items": items,
        "providers": sorted({item["provenance"] for item in items}),
        "raw": None,
    }
