"""Headlines from Mediastack.

Optional. Without a key this returns [] without calling anything -- a
missing key is a configuration state, not a search failure, and the two
should not look the same in the logs.

A caveat worth reading before enabling it: Mediastack's free tier does not
serve HTTPS. This module requests HTTPS anyway and does not fall back to
plaintext, because the fallback would put the API key and the user's query
on the wire in the clear, and silently downgrading someone's transport
security is not a decision a search provider gets to make. On a free key
that means requests fail and this provider returns [] -- GDELT still
answers. To use Mediastack, either take a paid tier that serves HTTPS, or
set MEDIASTACK_ALLOW_HTTP=1 to opt in deliberately.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote_plus

from ..http_fetch import http_fetch
from .provider_keys import module_key
from .news_common import NEWS_TIMEOUT_SECONDS, SNIPPET_CHARS, TITLE_CHARS, is_news_query, iso_timestamp, news_search_terms, trim

from logger import get_logger

logger = get_logger(__name__)

PROVENANCE = "mediastack"
MODULE_NAME = "mediastack"
ENV_VAR = "MEDIASTACK_KEY"
ALLOW_HTTP_VAR = "MEDIASTACK_ALLOW_HTTP"

_ENDPOINT = (
    "{scheme}://api.mediastack.com/v1/news"
    "?access_key={key}&keywords={query}&languages=en&limit={limit}"
    "&sort=published_desc"
)

MAX_ARTICLES = 5



def _words(query: str) -> list[str]:
    import re

    return re.sub(r"[^a-z0-9 ]+", " ", str(query or "").lower()).split()


def api_key() -> str | None:
    """The configured key, or None. Read fresh, never cached."""
    return module_key(MODULE_NAME, ENV_VAR)


def _scheme() -> str:
    """https unless someone has explicitly accepted plaintext."""
    return "http" if os.environ.get(ALLOW_HTTP_VAR, "").strip() in ("1", "true", "yes") else "https"


def applies_to(query: str) -> bool:
    return is_news_query(query)


def search_terms(query: str) -> str:
    return news_search_terms(query)


def _articles(payload: Any) -> list:
    if not isinstance(payload, dict):
        return []
    if payload.get("error"):
        logger.info("mediastack: %s", payload.get("error"))
        return []
    data = payload.get("data")
    return data if isinstance(data, list) else []


def lookup(query: str) -> list[dict]:
    """Recent articles on this subject, or []."""
    if not applies_to(query):
        return []

    key = api_key()
    if not key:
        logger.info("mediastack: no key configured; skipping")
        return []

    terms = search_terms(query)
    if not terms.strip():
        return []

    url = _ENDPOINT.format(
        scheme=_scheme(), key=key, query=quote_plus(terms), limit=MAX_ARTICLES,
    )
    try:
        response = http_fetch(url, timeout=NEWS_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("mediastack: request failed")
        return []

    if not isinstance(response, dict) or response.get("status") != "ok":
        return []

    items = []
    for article in _articles(response.get("data"))[:MAX_ARTICLES]:
        if not isinstance(article, dict):
            continue

        title = trim(article.get("title"), TITLE_CHARS)
        link = article.get("url")
        if not (title and link):
            continue

        source = trim(article.get("source"), TITLE_CHARS)
        description = trim(article.get("description"), SNIPPET_CHARS)

        items.append({
            "title": title,
            # The publisher is named in the snippet rather than replacing
            # the provenance: "who published it" and "which API returned
            # it" are different facts, and conflating them loses one.
            "snippet": f"{description} ({source})" if description and source
                       else description or title,
            "url": link,
            "source": source,
            "published_at": iso_timestamp(article.get("published_at")),
            "timestamp": iso_timestamp(article.get("published_at")),
            "provenance": PROVENANCE,
            "rank": len(items) + 1,
        })
    return items
