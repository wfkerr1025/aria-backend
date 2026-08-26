"""ARIA Lite Phase 7.2 - spotting a message that carries on the last one.

"continue", "next", "fix this", "why is it still failing" -- messages that
say nothing about their own subject because they are leaning on the
previous turn for it. They are the most common shape a follow-up takes and
the hardest for retrieval to handle, since there is nothing in them to
search for.

Detecting one does not decide anything on its own. It tells the goal
tracker that the user has not started something new, so the goal already in
play should stay in play rather than being replaced by whatever a keyword
classifier makes of six words with no content in them.

Markers split into two kinds, and the difference matters:

    CONTINUATION markers ("continue", "next", "keep going") say to carry on
    with the same work. If the surrounding topic looks different, the topic
    is wrong -- "next" in the middle of a Unity task is still the Unity
    task -- so the goal keeps its own topic.

    Resumption markers ("anyway", "okay now", "fix this") re-open or
    redirect. They are equally a sign that no new goal was stated, but they
    do not insist the subject is unchanged, so a topic change is taken at
    face value.

Whole-word matching throughout: "next" must not fire inside "nextcloud",
and "continue" is a word rather than a substring of one.
"""

from __future__ import annotations

import re

__all__ = [
    "CONTINUATION_MARKERS",
    "IMMEDIATE_MARKERS",
    "MARKERS",
    "detect_continuity",
    "has_immediate_marker",
    "implies_continuation",
    "points_at_immediate_context",
]

# Every marker, longest first so "fix the rest" is reported rather than the
# "fix this"-shaped fragment of it, and "keep going" beats "going".
MARKERS = (
    "why is it still failing",
    "why is it still broken",
    "why does it still fail",
    "what about the rest",
    "fix the rest",
    "keep going",
    "carry on",
    "go on",
    "fix this",
    "fix it",
    "fix that",
    "try again",
    "okay now",
    "ok now",
    "and now",
    "still failing",
    "still broken",
    "continue",
    "anyway",
    "next",
    "more",
)

# The subset that asserts the work has not changed. The rest signal only
# that nothing new was started.
CONTINUATION_MARKERS = frozenset({
    "continue",
    "next",
    "keep going",
    "carry on",
    "go on",
    "more",
})

# Markers that point at whatever was said immediately before -- either by
# saying to carry on with it, or by pointing at it with a pronoun. These are
# the ones a resumption marker must not override: "anyway, continue" and
# "anyway, fix this" are both asking to carry on with the thing in front of
# us, whatever the "anyway" was doing there.
#
# The rest of the continuity markers ("why is it still failing", "still
# broken") are anaphoric questions with no such anchor. They say the user is
# not starting something new, and nothing about which existing work they
# mean, which leaves a resumption marker free to answer that.
IMMEDIATE_MARKERS = CONTINUATION_MARKERS | {
    "fix this",
    "fix it",
    "fix that",
    "fix the rest",
    "what about the rest",
    "try again",
}

_MARKERS_LONGEST_FIRST = tuple(sorted(MARKERS, key=len, reverse=True))


def _normalized(text: str) -> str:
    """Lowercase, punctuation-flattened and padded for whole-word matching.

    Padding is what makes "next" a word rather than a substring, and
    flattening punctuation is what lets "okay, now..." match "okay now".
    """
    lowered = str(text or "").lower().replace("'", "")
    return f" {' '.join(re.sub(r'[^a-z0-9]+', ' ', lowered).split())} "


def detect_continuity(text: str) -> str | None:
    """The continuity marker a message carries, or None.

    Longest match wins, so "why is it still failing" is reported as itself
    rather than as the "still failing" inside it -- the longer marker is the
    more specific description of what the user did.
    """
    normalized = _normalized(text)
    for marker in _MARKERS_LONGEST_FIRST:
        if f" {marker} " in normalized:
            return marker
    return None


def implies_continuation(marker: str | None) -> bool:
    """Whether this marker asserts the work itself has not changed."""
    return bool(marker) and marker in CONTINUATION_MARKERS


def points_at_immediate_context(marker: str | None) -> bool:
    """Whether this marker names the turn immediately before as its subject."""
    return bool(marker) and marker in IMMEDIATE_MARKERS


def has_immediate_marker(text: str) -> bool:
    """Whether the text contains any marker pointing at the previous turn.

    Scans for all of them rather than asking whether the *detected* marker
    is one, because detection reports the longest match and "anyway" is
    longer than "next", "more" and "go on". Reading "anyway, next" through
    detect_continuity alone reports "anyway" and hides the instruction to
    move on -- which is exactly the case this guard exists to catch.
    """
    normalized = _normalized(text)
    return any(f" {marker} " in normalized for marker in IMMEDIATE_MARKERS)
