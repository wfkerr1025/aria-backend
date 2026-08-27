"""Shared normalization for the news providers.

Three providers, three payload shapes, one evidence schema. Everything
here is about getting from one to the other without inventing anything:
a field the publisher did not send comes out None, not guessed.

The HTML stripping matters more than it looks. Article descriptions
routinely arrive with markup in them, and a snippet carrying raw tags
reaches a model as text it will try to make sense of -- and reaches the
user, on the raw-evidence path, as tags on the screen.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

__all__ = [
    "NEWS_MARKERS",
    "NEWS_TIMEOUT_SECONDS",
    "SNIPPET_CHARS",
    "TITLE_CHARS",
    "clean_text",
    "is_news_query",
    "iso_timestamp",
    "news_search_terms",
    "news_words",
    "trim",
]

TITLE_CHARS = 120
SNIPPET_CHARS = 300

_TAG = re.compile(r"<[^>]+>")
_ENTITY = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&#39;": "'", "&apos;": "'", "&nbsp;": " ", "&hellip;": "…",
}

# GDELT's own timestamp format: 20260827T024500Z.
_GDELT_STAMP = re.compile(r"^(\d{8})T(\d{6})Z$")


def clean_text(value) -> str:
    """Markup out, entities decoded, whitespace collapsed."""
    text = _TAG.sub(" ", str(value or ""))
    for entity, char in _ENTITY.items():
        text = text.replace(entity, char)
    text = " ".join(text.split())

    # A tag is replaced by a space so "a<b>b</b>" does not become "ab" --
    # which leaves "tightening ." wherever a tag closed before
    # punctuation. Closing the gap here rather than leaving the artifact
    # in front of a reader.
    return re.sub(r"\s+([.,;:!?%])", r"\1", text)


def trim(value, limit: int) -> str | None:
    """Cut at a word boundary, or None when there is nothing left.

    None rather than "" on purpose: the evidence layer treats an empty
    snippet as "this result said nothing", and it should reach that
    conclusion the same way whether the field was absent or blank.
    """
    text = clean_text(value)
    if not text:
        return None
    if len(text) <= limit:
        return text

    cut = text[:limit]
    boundary = cut.rfind(" ")
    if boundary > limit // 2:
        cut = cut[:boundary]
    return cut.rstrip(" ,;:") + "…"


def iso_timestamp(value) -> str | None:
    """A publication time in ISO 8601, or None.

    None is a perfectly good answer. An article whose timestamp cannot be
    parsed is still an article, and stamping it with "now" would date a
    week-old story to this minute -- which is worse than admitting the
    time is unknown, because a reader cannot tell the difference.
    """
    if isinstance(value, datetime):
        return value.isoformat()

    text = str(value or "").strip()
    if not text:
        return None

    match = _GDELT_STAMP.match(text)
    if match:
        try:
            stamp = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
            return stamp.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            return None

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass

    # Mediastack sends RFC 3339 with an offset; NYTimes sends the same.
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue
    return None


# ------------------------------------------------------
# What counts as a news question, and what it is about
# ------------------------------------------------------
# This vocabulary lived in news_gdelt, and the other two providers
# imported it from there -- which was fine while GDELT was the keyless
# floor that always ran, and became wrong the moment it was removed.
# Shared vocabulary belongs in the shared module; a provider owning the
# definition of the category it belongs to is a provider the others
# cannot outlive.

NEWS_MARKERS: tuple[str, ...] = (
    "news", "headline", "headlines", "breaking", "reported", "coverage",
    "whats happening", "what is happening", "latest on", "story", "stories",
)

# Words that ask the question rather than name its subject. Removed so
# "show me the latest news about AI regulation" searches for "AI
# regulation" and not for the sentence someone typed it in.
_FILLER = {
    "show", "me", "the", "latest", "news", "about", "on", "a", "an", "of",
    "please", "give", "any", "whats", "what", "is", "are", "happening",
    "in", "with", "for", "to", "get", "find", "tell", "today", "todays",
    "headline", "headlines", "breaking", "current", "recent", "story",
    "stories", "search", "look", "up",
}

# The news tier is best-effort: providers answering the same question,
# none of them required. A shorter budget than http_fetch's default, so
# one unreachable host cannot spend the turn the others -- and the
# LangSearch fallback behind them -- still need. GDELT was exactly that
# host: unreachable, and at the old shared 10s it took the whole tool
# budget with it and the turn gave up before the fallback had run.
NEWS_TIMEOUT_SECONDS = 3


def news_words(query) -> list[str]:
    return re.sub(r"[^a-z0-9 ]+", " ", str(query or "").lower()).split()


def is_news_query(query) -> bool:
    """Whether this question is asking for news."""
    text = " ".join(news_words(query))
    return any(marker in text for marker in NEWS_MARKERS)


def news_search_terms(query) -> str:
    """The subject, with the asking-words stripped off."""
    kept = [word for word in news_words(query) if word not in _FILLER]
    return " ".join(kept) if kept else " ".join(news_words(query))
