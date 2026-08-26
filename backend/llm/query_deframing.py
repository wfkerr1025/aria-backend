"""ARIA Lite - query de-framing.

Strips the conversational wrapper off a question before it is embedded, so
retrieval sees what the user is asking about rather than how they asked.

"search my documents for login credentials" and "login credentials" mean the
same thing to a person, but not to an embedding model: the four framing words
are a third of the first query's tokens, and they pull its vector toward
generic document-management language that matches nothing in particular. That
was measured directly on this stack -- the correct chunk scored 0.597 for the
framed form and 0.698 for the bare topic, which is the difference between
landing above and below the file-search floor.

Rule-based and deterministic on purpose. A model could paraphrase queries
more cleverly, but this runs in front of every retrieval, and a preprocessing
step that is occasionally creative is worse than one that is always
predictable: a query that de-frames differently on two calls would make
search results irreproducible for no visible reason.

Conservative by design. Only leading phrases are removed, only at a word
boundary, and if stripping would empty the query the original is returned
untouched -- a query reduced to nothing retrieves nothing, which is strictly
worse than a framed query that at least carries its topic.
"""

from __future__ import annotations

# Leading phrases that carry no topic. Multi-word wherever possible: single
# common words are far more likely to be part of what the user is actually
# asking about than to be framing.
FRAMING_PHRASES = (
    "search my documents for",
    "search my files for",
    "search my documents",
    "search my files",
    "what can you tell me about",
    "can you tell me about",
    "could you tell me about",
    "can you tell me",
    "could you tell me",
    "i want to know about",
    "i need information on",
    "find information about",
    "give me details on",
    "give me information on",
    "tell me about",
    "please explain",
    "look up",
    "explain",
)

# Removed after a framing phrase because an article carries no meaning of its
# own: "look up the pipeline architecture" is a question about pipeline
# architecture, not about "the".
LEADING_ARTICLES = ("the", "a", "an")

# Punctuation that can sit between a framing phrase and the topic.
_JOINERS = " \t,:;-—"

# Longest first, so "what can you tell me about" wins over "can you tell me"
# and the whole wrapper comes off in one pass rather than leaving a fragment.
_PHRASES_LONGEST_FIRST = tuple(sorted(FRAMING_PHRASES, key=len, reverse=True))


def _strip_one_prefix(text: str) -> tuple[str, bool]:
    """Remove a single leading framing phrase. Returns (text, removed)."""
    lowered = text.lower()
    for phrase in _PHRASES_LONGEST_FIRST:
        if not lowered.startswith(phrase):
            continue
        rest = text[len(phrase):]
        # The phrase has to end on a word boundary: "explain" must not fire
        # on "explaining", and "look up" must not fire on "look upward".
        if rest and rest[0] not in _JOINERS:
            continue
        return rest.strip(_JOINERS), True
    return text, False


def _strip_leading_article(text: str) -> str:
    head, _, tail = text.partition(" ")
    if tail and head.lower() in LEADING_ARTICLES:
        return tail
    return text


def deframe_query(raw: str) -> str:
    """Return the query with its conversational framing removed.

    Whitespace is collapsed, leading framing phrases are stripped repeatedly
    (so "please explain what can you tell me about X" reduces cleanly), and a
    leading article is dropped. Falls back to the original string whenever
    the result would be empty.
    """
    text = " ".join(str(raw or "").split())
    if not text:
        return raw

    working = text
    removed_any = False
    while True:
        working, removed = _strip_one_prefix(working)
        if not removed:
            break
        removed_any = True

    if removed_any:
        working = _strip_leading_article(working)

    working = " ".join(working.split())
    return working if working else raw
