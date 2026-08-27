"""General web search, from LangSearch.

The one general-web provider, replacing Brave and Tavily. It answers the
questions no specialist claims -- documentation, release notes, how-tos,
forums, general knowledge -- and it also backs up the news path: when
NYTimes, Mediastack and GDELT all come back empty, this runs with the same
query rather than letting a news question fail for want of a key.

That fallback is conditional, not additive. A news question that the
structured providers *did* answer does not also get a page of search
results: a named publisher's abstract with a publication date is better
evidence than a link about the same story, and stacking both spends the
answer's budget twice on one fact.

What it will not do is ask for a generated answer. LangSearch can return
one; requesting it would put a summary written elsewhere into the evidence
section, untraceable to any single page -- the exact shape the evidence
pipeline exists to reject. This layer returns sources.

Verified against the live API on 2026-08-27: /v1/web-search answers,
/v1/search answers 404, and the body is the shape _results() reads. The
snippets come back pre-tokenized, with punctuation spaced out and the
occasional U+FFFD where their extraction could not decode a character,
which is what _repair_snippet exists to undo.

LOG_RAW_RESPONSE is what found that. It puts the request, the transport
status and the entire response body in the log at INFO, before anything
is parsed, so a 404 reads as a 404 rather than as an empty result. It is
temporary and noisy by design -- turn it off now that the shape is known.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..http_fetch import http_fetch
from .provider_keys import module_key
from .news_common import SNIPPET_CHARS, TITLE_CHARS, iso_timestamp, trim

from logger import get_logger

logger = get_logger(__name__)

PROVENANCE = "langsearch"
MODULE_NAME = "langsearch"
ENV_VAR = "LANGSEARCH_API_KEY"

# /v1/search answers 404 -- verified live, with a key:
#     {"code":"404","message":"Not Found","log_id":"5d45d614..."}
# /v1/web-search is the one that serves, and its response is the shape
# _results() reads. Confirmed against the live API on 2026-08-27.
_ENDPOINT = "https://api.langsearch.com/v1/web-search"

MAX_RESULTS = 10

# TEMPORARY. Logs the request and the whole response body at INFO on
# every call, so the live payload can be read off the running server's
# console without changing a log level or attaching a debugger.
#
# This must come out once the shape is confirmed. It is INFO-level, it is
# unbounded, and a search response is large -- left on, it will bury the
# rest of the log. It also prints whatever the provider sent back, which
# is fine for search results and would not be fine for anything else.
LOG_RAW_RESPONSE = True

# Weather never reaches a search provider -- the router short-circuits a
# weather turn to the fusion engine before Phase 9 runs -- so this is a
# guard rather than the mechanism. Kept local because there is no shared
# table to read it from.
WEATHER_MARKERS = (
    "weather", "temperature", "forecast", "raining", "snowing",
    "humidity", "wind speed", "is it cold", "is it hot",
)


def _words(query: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]+", " ", str(query or "").lower()).split()


def api_key() -> str | None:
    """The configured key, or None. Read fresh, never cached."""
    return module_key(MODULE_NAME, ENV_VAR)


def applies_to(query: str) -> bool:
    """Anything a specialist does not answer better.

    Finance and FX are excluded by reading search_domain rather than by
    restating what counts as a money question -- a second copy of that
    vocabulary is a second copy to drift, which this codebase has already
    had to fix twice.

    News is deliberately NOT excluded: this provider is the news path's
    fallback, and web_search only reaches it when the structured news
    providers have already come back empty.
    """
    text = " ".join(_words(query))
    if not text:
        return False

    if any(marker in text for marker in WEATHER_MARKERS):
        return False

    try:
        from backend.core.search_intent import search_domain
    except ImportError:  # pragma: no cover - running outside the backend
        return True

    return search_domain(query) is None


def _results(payload: Any) -> list:
    """The result rows out of a LangSearch response body.

    The documented shape, which is the one this reads first:

        {"code": 200, "log_id": "...", "msg": null,
         "data": {"_type": "SearchResponse",
                  "queryContext": {"originalQuery": "..."},
                  "webPages": {"webSearchUrl": "...",
                               "totalEstimatedMatches": 100,
                               "value": [ {...}, {...} ],
                               "someResultsRemoved": false}}}

    A row inside `value` carries id / name / url / displayUrl / snippet /
    summary / siteName / siteIcon / datePublished / dateLastCrawled.

    The looser branches below are a landing net, not a second contract:
    they exist because the shape has not been seen live yet, and they
    come out with the raw logging. What is never done is guessing at the
    *fields* -- a row without a title and a URL is skipped, not
    reconstructed from whatever else is lying around.
    """
    if not isinstance(payload, dict):
        return []

    data = payload.get("data")

    if isinstance(data, dict):
        pages = data.get("webPages")
        if isinstance(pages, dict) and isinstance(pages.get("value"), list):
            return pages["value"]
        if isinstance(data.get("results"), list):
            return data["results"]
        if isinstance(data.get("value"), list):
            return data["value"]
    if isinstance(data, list):
        return data

    if isinstance(payload.get("results"), list):
        return payload["results"]
    return []


def _log_raw(request_body: dict, response: Any) -> None:
    """TEMPORARY. The request and the whole response, at INFO.

    Logged before the status check, so an endpoint that answers 404 or
    401 shows up as the 404 or 401 it is rather than as an empty result
    indistinguishable from "found nothing".
    """
    if not LOG_RAW_RESPONSE:
        return

    logger.info("langsearch RAW REQUEST: POST %s body=%s",
                _ENDPOINT, json.dumps(request_body))

    if not isinstance(response, dict):
        logger.info("langsearch RAW RESPONSE: not a dict: %r", response)
        return

    logger.info("langsearch RAW RESPONSE: status=%s http_code=%s error=%s",
                response.get("status"), response.get("code"), response.get("error"))

    body = response.get("data")
    if isinstance(body, dict):
        logger.info("langsearch RAW BODY keys: %s", sorted(body.keys()))
    try:
        logger.info("langsearch RAW BODY: %s",
                    json.dumps(body, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        logger.info("langsearch RAW BODY (unserializable): %r", body)


def _host(value) -> str | None:
    """The host out of a URL, a bare host, or a host with a path.

    displayUrl arrives as a whole URL -- "https://docs.pytest.org/en/..."
    -- not as the bare host the name suggests, so the scheme has to come
    off before the first path segment is taken. Splitting on "/" alone
    yields "https:", which then reads to the user as the publisher.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if "//" in text:
        text = text.split("//", 1)[1]
    host = text.split("/")[0].split("?")[0].strip().lower()
    return host or None


# LangSearch returns snippets pre-tokenized, with punctuation spaced out
# as its own token: "python 3 . 12 . 14 , 3 . 11 . 16 are now available".
# news_common.clean_text closes the space before punctuation, which gets
# most of the way there, but leaves version numbers and decimals split:
# "3. 12. 14". That reaches the model as three numbers where the page had
# one, and a model asked which version shipped can answer "3".
#
# Only digits are rejoined. A wider rule would run "sentence. Next" into
# "sentence.Next", trading a real defect for a cosmetic one everywhere
# else in the snippet.
_SPLIT_NUMBER = re.compile(r"(?<=\d)\.\s+(?=\d)")

# LangSearch's own text extraction leaves U+FFFD behind where it could
# not decode a character -- "Changelog <?> pytest documentation" for an
# em dash. It arrives that way in their JSON, so there is nothing to
# recover; dropping it beats showing the reader a black diamond and
# beats handing the model a character that means "decoding failed".
_REPLACEMENT_CHAR = "�"


def _repair_snippet(value) -> str:
    text = _SPLIT_NUMBER.sub(".", str(value or ""))
    text = text.replace(_REPLACEMENT_CHAR, " ")
    return " ".join(text.split())


def _domain(row: dict, url: str) -> str | None:
    """The publisher's domain.

    siteName is deliberately not used here: it is a human-readable name
    ("pytest documentation"), and this field is a domain -- the thing a
    reader can check against the link sitting next to it.
    """
    return _host(row.get("displayUrl")) or _host(url)


def lookup(query: str) -> list[dict]:
    """Web results for this query, or []."""
    if not applies_to(query):
        return []

    key = api_key()
    if not key:
        logger.info("langsearch: no key configured; skipping")
        return []

    request_body = {
        "query": query.strip(),
        "count": MAX_RESULTS,
        "num_results": MAX_RESULTS,
        "include_images": False,
        "include_videos": False,
        # Semantic rerank: the ordering is the provider's relevance
        # judgement, and it is kept as the rank rather than re-sorted here.
        "rerank": True,
        # No provider-written answer. See the module docstring.
        "summary": False,
    }

    try:
        response = http_fetch(
            _ENDPOINT,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json_body=request_body,
        )
    except Exception:
        logger.exception("langsearch: request failed")
        _log_raw(request_body, None)
        return []

    _log_raw(request_body, response)

    if not isinstance(response, dict) or response.get("status") != "ok":
        return []

    body = response.get("data")

    # LangSearch carries its own status inside a 200. A body that says
    # code 403 is a failure whatever the transport thought, and reading
    # past it would turn a rejected request into "found nothing".
    if isinstance(body, dict) and body.get("code") not in (None, 200, "200"):
        logger.warning("langsearch: request rejected: code=%s msg=%s",
                       body.get("code"), body.get("msg"))
        return []

    items = []
    for row in _results(body)[:MAX_RESULTS]:
        if not isinstance(row, dict):
            continue

        # After trim, not before: the raw text is "3 . 14 . 7", and the
        # space in front of the dot is one clean_text closes up. Repairing
        # first leaves "3. 14. 7" -- which is what shipped, and what the
        # first live query showed in the title while the snippet beside
        # it read correctly.
        title = trim(row.get("name") or row.get("title"), TITLE_CHARS)
        title = _repair_snippet(title) if title else title
        link = row.get("url") or row.get("link")
        if not (title and link):
            continue

        # trim() cleans and cuts; the number repair runs on its output
        # because the space it closes up is one clean_text creates.
        body = trim(row.get("snippet") or row.get("description") or row.get("content"),
                    SNIPPET_CHARS)
        body = _repair_snippet(body) if body else body
        source = _domain(row, link)
        # Only when the provider actually sent one. An undated page
        # stamped with the moment it was fetched carries a date it never
        # had, and a reader cannot tell the difference.
        published = iso_timestamp(row.get("datePublished") or row.get("published_at"))

        items.append({
            "title": title,
            "snippet": f"{body} ({source})" if body and source else body or title,
            "url": link,
            "source": source,
            "published_at": published,
            "timestamp": published,
            "provenance": PROVENANCE,
            "rank": len(items) + 1,
        })
    return items
