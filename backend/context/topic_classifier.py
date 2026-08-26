"""ARIA Lite Phase 7.1 - what a message is about.

Deterministic keyword classification into a small set of stable topic
labels. No model is consulted: a message that classified as "unity" on one
turn and "backend" on the next would silently change which half of a
conversation retrieval is allowed to read, and nothing in the transcript
would explain why.

Five of the seven labels are the domains query_expansion already knows, and
their vocabularies are imported from it rather than retyped. That matters
more than it looks: expansion decides which synonyms a Unity query gets,
this decides which messages count as the Unity conversation, and two
independently-maintained lists of Unity words would drift into disagreeing
about what Unity means. "coding" and "travel" have no expansion table of
their own, so their markers are defined here.

Priority when a message matches more than one topic:

    A multi-word marker wins first. "nl routing" is two words and belongs
    to aria_internal; "routing" is one word and belongs to backend. Ranking
    by priority alone would send "what does nl routing do" to backend, which
    is both wrong and, in a spec that lists "nl routing" as the example of
    an aria_internal message, obviously not the intent.

    The stated order decides everything else. Among markers of the same
    length in words, priority alone rules: a message naming a prefab and a
    provider is a Unity message that mentions a provider, because unity
    outranks backend. Character length deliberately plays no part -- it
    looks like a specificity signal and is not, and using it sends "the
    shader will not compile" to coding on the strength of one extra letter.
"""

from __future__ import annotations

import re

try:
    from backend.context.topic_segments import GENERAL_TOPIC
    from backend.llm.query_expansion import DOMAIN_MARKERS, ExpansionDomain
except ImportError:  # running from inside the backend directory
    from context.topic_segments import GENERAL_TOPIC
    from llm.query_expansion import DOMAIN_MARKERS, ExpansionDomain

__all__ = [
    "TOPICS",
    "TOPIC_MARKERS",
    "TOPIC_PRIORITY",
    "classify_topic",
    "topic_matches",
]

# Markers unique to this layer. The other five topics take theirs from
# query_expansion, below.
_CODING_MARKERS = (
    "python", "javascript", "typescript", "rust", "sql", "regex",
    "debug", "debugging", "stack trace", "traceback", "exception",
    "refactor", "unit test", "pytest", "compile", "syntax", "lint",
    "git", "commit", "merge conflict", "pull request",
)

_TRAVEL_MARKERS = (
    "flight", "flights", "hotel", "hotels", "trip", "itinerary",
    "airport", "boarding", "passport", "visa", "booking", "check in",
    "layover", "airline", "train ticket", "car rental",
)

# One vocabulary per topic. The five shared with query_expansion are taken
# by reference, so adding a Unity marker there teaches this too.
TOPIC_MARKERS: dict[str, tuple[str, ...]] = {
    "unity": DOMAIN_MARKERS[ExpansionDomain.UNITY],
    "backend": DOMAIN_MARKERS[ExpansionDomain.BACKEND],
    "weather": DOMAIN_MARKERS[ExpansionDomain.WEATHER],
    "aria_internal": DOMAIN_MARKERS[ExpansionDomain.ARIA_INTERNAL],
    "coding": _CODING_MARKERS,
    "travel": _TRAVEL_MARKERS,
}

# Tie-break order, as specified. Only consulted when two matches are equally
# specific; a longer marker always wins first.
TOPIC_PRIORITY = (
    "unity",
    "backend",
    "weather",
    "aria_internal",
    "coding",
    "travel",
    GENERAL_TOPIC,
)

TOPICS = frozenset(TOPIC_PRIORITY)

_RANK = {topic: index for index, topic in enumerate(TOPIC_PRIORITY)}


def _normalized(text: str) -> str:
    """Lowercase, punctuation-flattened and padded for whole-word matching.

    Padding both ends is what makes the match whole-word: " rain " cannot be
    found inside "training", and " aria " cannot be found inside "area".
    Punctuation becomes whitespace so "prefab," and "prefab" are the same
    word, and apostrophes are dropped so "doesn't" survives as one token.
    """
    lowered = str(text or "").lower().replace("'", "")
    return f" {' '.join(re.sub(r'[^a-z0-9]+', ' ', lowered).split())} "


def topic_matches(text: str) -> dict[str, str]:
    """Every topic the text matches, mapped to its longest matching marker.

    Exposed because the classifier's answer is more useful when you can see
    what it was decided on -- a message classified as backend on the word
    "provider" is worth knowing about when it turns out to have been about a
    healthcare provider.
    """
    normalized = _normalized(text)
    found: dict[str, str] = {}
    for topic in TOPIC_PRIORITY:
        best = ""
        for marker in TOPIC_MARKERS.get(topic, ()):
            if f" {marker} " in normalized and len(marker) > len(best):
                best = marker
        if best:
            found[topic] = best
    return found


def classify_topic(text: str) -> str:
    """The single topic a message is about.

    Returns one of TOPIC_PRIORITY. "general" is the answer for anything that
    matches nothing, which is most ordinary conversation and not a failure:
    a general thread is a real thread, and forcing every "thanks, that
    worked" into a domain would scatter the conversation across topics it
    was never about.
    """
    matches = topic_matches(text)
    if not matches:
        return GENERAL_TOPIC

    def specificity(item) -> tuple:
        topic, marker = item
        # Word count, then the stated priority. Deliberately not character
        # length: "compile" is longer than "shader", and letting that decide
        # sends "the shader will not compile" to coding when the stated
        # order says unity. Length is a proxy for specificity only across
        # word counts, where "nl routing" really is more specific than
        # "routing"; within one word count the priority list is the answer,
        # and it is the answer the spec gives.
        return (len(marker.split()), -_RANK[topic])

    return max(matches.items(), key=specificity)[0]
