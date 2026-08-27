"""Headlines from the New York Times Article Search API.

Optional, like Mediastack: no key means [] and no request. Listed first
among the news providers when a key is present, because a named
publisher's own abstract is better evidence than a headline with no body
text -- and because deduplication keeps the first occurrence of a story,
so ordering decides which version of a shared story survives.

`source` is fixed rather than read from the payload. Article Search does
carry a "source" field, and for this endpoint it is always the Times;
reading it back would look like provenance while being a constant.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

from ..http_fetch import http_fetch
from .provider_keys import module_key
from .news_common import NEWS_TIMEOUT_SECONDS, SNIPPET_CHARS, TITLE_CHARS, is_news_query, iso_timestamp, news_search_terms, trim

from logger import get_logger

logger = get_logger(__name__)

PROVENANCE = "nytimes"
MODULE_NAME = "nytimes"
ENV_VAR = "NYTIMES_KEY"
SOURCE_NAME = "The New York Times"

_ENDPOINT = (
    "https://api.nytimes.com/svc/search/v2/articlesearch.json"
    "?q={query}&sort=newest&api-key={key}"
)

MAX_ARTICLES = 5



def api_key() -> str | None:
    """The configured key, or None. Read fresh, never cached."""
    return module_key(MODULE_NAME, ENV_VAR)


def applies_to(query: str) -> bool:
    return is_news_query(query)


def search_terms(query: str) -> str:
    return news_search_terms(query)


def _docs(payload: Any) -> list:
    if not isinstance(payload, dict):
        return []
    response = payload.get("response")
    if not isinstance(response, dict):
        return []
    docs = response.get("docs")
    return docs if isinstance(docs, list) else []


def _headline(doc: dict) -> str | None:
    headline = doc.get("headline")
    if isinstance(headline, dict):
        return trim(headline.get("main") or headline.get("print_headline"), TITLE_CHARS)
    return trim(headline, TITLE_CHARS)


def lookup(query: str) -> list[dict]:
    """Recent Times articles on this subject, or []."""
    if not applies_to(query):
        return []

    key = api_key()
    if not key:
        logger.info("nytimes: no key configured; skipping")
        return []

    terms = search_terms(query)
    if not terms.strip():
        return []

    url = _ENDPOINT.format(query=quote_plus(terms), key=key)
    try:
        response = http_fetch(url, timeout=NEWS_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("nytimes: request failed")
        return []

    if not isinstance(response, dict) or response.get("status") != "ok":
        return []

    items = []
    for doc in _docs(response.get("data"))[:MAX_ARTICLES]:
        if not isinstance(doc, dict):
            continue

        title = _headline(doc)
        link = doc.get("web_url")
        if not (title and link):
            continue

        # The abstract first, the lead paragraph as a fallback, and the
        # headline only if neither exists -- a story with no body text is
        # still a story, and repeating the headline says so plainly rather
        # than leaving an empty snippet the evidence layer would discard.
        body = trim(doc.get("abstract"), SNIPPET_CHARS) or trim(
            doc.get("lead_paragraph"), SNIPPET_CHARS
        )

        items.append({
            "title": title,
            "snippet": f"{body} ({SOURCE_NAME})" if body else title,
            "url": link,
            "source": SOURCE_NAME,
            "published_at": iso_timestamp(doc.get("pub_date")),
            "timestamp": iso_timestamp(doc.get("pub_date")),
            "provenance": PROVENANCE,
            "rank": len(items) + 1,
        })
    return items
