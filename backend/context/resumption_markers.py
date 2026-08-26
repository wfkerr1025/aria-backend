"""ARIA Lite Phase 7.2 refinement - spotting a return to earlier work.

"anyway", "back to that", "as I was saying" -- the words people use when a
conversation has wandered and they are pulling it back. They are the mirror
image of a continuation marker, and the difference is which direction they
point:

    "continue" points forward from the last thing said. Whatever was just
    being discussed, carry on with it.

    "anyway" points back over it. The last thing being discussed was a
    detour, and the work being resumed is the one it interrupted.

Getting these two confused is what made "anyway, why is it still failing"
resolve to a weather forecast: the message was read as continuing whatever
came immediately before, which was precisely the thing the user had just
signalled they were done with.

A resumption marker is only ever a hint about *which* goal, never about
whether there is one. It resumes something already on the stack or it does
nothing at all -- there is no reading of "anyway" that invents work the user
never mentioned.
"""

from __future__ import annotations

import re

__all__ = ["MARKERS", "detect_resumption"]

# Longest first, so "back to that" is reported rather than the "that" inside
# it and "as i was saying" is not shadowed by a shorter fragment.
MARKERS = (
    "as i was saying",
    "as i said earlier",
    "returning to that",
    "returning to this",
    "getting back to",
    "back to that",
    "back to this",
    "where were we",
    "anyway",
)

_MARKERS_LONGEST_FIRST = tuple(sorted(MARKERS, key=len, reverse=True))


def _normalized(text: str) -> str:
    """Lowercase, punctuation-flattened and padded for whole-word matching.

    Padding keeps "anyway" from firing inside "anyways" -- which is the same
    word, but matching it as a prefix would also match words that are not --
    and flattening punctuation is what lets "anyway," and "anyway" be the
    same marker.
    """
    lowered = str(text or "").lower().replace("'", "")
    return f" {' '.join(re.sub(r'[^a-z0-9]+', ' ', lowered).split())} "


def detect_resumption(text: str) -> str | None:
    """The resumption marker a message carries, or None."""
    normalized = _normalized(text)
    for marker in _MARKERS_LONGEST_FIRST:
        if f" {marker} " in normalized:
            return marker
    return None
