"""ARIA Lite Phase 7.2 - reading a goal out of what the user said.

Turns "Help me fix the shader error" into "fix the shader error", and
returns None for everything that is not somebody stating what they want.

Two kinds of marker, because they need opposite treatment:

    Prefixes are removed. "help me", "let's", "can you", "I want to" are
    politeness and framing around the goal, not part of it -- keeping them
    would make "help me fix the shader" and "fix the shader" two different
    goals, and the user would rightly say they had asked for one thing.

    Verbs are kept. "fix", "implement", "explain" are the goal's first
    word. A request has to start with one of these, after any prefix comes
    off, or it is not a goal statement at all.

The second rule is what keeps this from labelling everything a goal. "The
shader is broken" is a report, "why is it still failing" is a question, and
neither begins with an action the user is asking for -- so both return None
and leave whatever goal is already in play alone. Returning a goal for
every message would make the goal stack a log of the conversation rather
than a record of what is being worked on.

An action with nothing but a pronoun after it is also not a goal. "Fix
this" begins with a verb but names no object; it is a continuity marker
pointing at whatever is already being discussed, and continuity_markers is
the module that handles it.

Rule-based and deterministic. A goal that came out differently on two reads
of the same sentence would silently re-scope retrieval mid-conversation.
"""

from __future__ import annotations

import re

__all__ = [
    "GOAL_CHARS",
    "OBJECTLESS",
    "PREFIX_MARKERS",
    "VERB_MARKERS",
    "classify_goal",
]

# Removed from the front, longest first so "i would like to" comes off whole
# rather than leaving "like to" behind.
PREFIX_MARKERS = (
    "i would like to", "i would like you to", "i want you to", "id like to",
    "i need you to", "i want to", "i need to", "i would like", "id like",
    "could you please", "can you please", "would you please",
    "could you", "can you", "would you", "will you",
    "please help me", "help me to", "help me", "lets", "let us",
    "we should", "we need to", "i am trying to", "im trying to",
    "please", "now", "next",
)

# A goal starts with one of these. Deliberately a closed list of actions
# somebody asks for, rather than any verb: "the build takes ten minutes"
# starts with a noun and is a report, and no amount of parsing should turn
# it into a request.
VERB_MARKERS = (
    "fix", "build", "implement", "explain", "add", "remove", "delete",
    "create", "write", "debug", "refactor", "review", "test", "check",
    "set up", "setup", "configure", "install", "update", "upgrade",
    "migrate", "optimise", "optimize", "investigate", "find", "show",
    "summarize", "summarise", "compare", "document", "rename", "move",
    "deploy", "release", "profile", "trace", "clean up", "finish",
)

# Words that are not an object. A verb followed only by these is pointing at
# something already under discussion rather than naming a new goal.
OBJECTLESS = frozenset({
    "this", "that", "it", "them", "these", "those", "everything",
    "the rest", "all of it", "all of them", "the others", "the other one",
    "them all", "the whole thing", "mine", "one", "some",
})

# A goal is a label, not a paragraph. Long enough to distinguish two pieces
# of work, short enough to sit in a prompt line without crowding it.
GOAL_CHARS = 120

# A resumption marker at the front of a request is framing too. "Anyway,
# help me build the api" states a goal, and leaving the "anyway" attached
# means the sentence no longer begins with an action and stops reading as a
# goal at all -- which would hand the turn to resumption handling and lose
# the thing the user just asked for. Taken from resumption_markers rather
# than relisted, so the two cannot drift.
try:
    from backend.context.resumption_markers import MARKERS as _RESUMPTION_MARKERS
except ImportError:  # running from inside the backend directory
    from context.resumption_markers import MARKERS as _RESUMPTION_MARKERS

_ALL_PREFIXES = PREFIX_MARKERS + tuple(_RESUMPTION_MARKERS) + ("so", "okay", "ok", "right")

_PREFIXES_LONGEST_FIRST = tuple(sorted(_ALL_PREFIXES, key=len, reverse=True))
_VERBS_LONGEST_FIRST = tuple(sorted(VERB_MARKERS, key=len, reverse=True))


def _normalized(text: str) -> str:
    """Lowercase, punctuation-flattened, whitespace-collapsed."""
    lowered = str(text or "").lower().replace("'", "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", lowered).split())


def _strip_prefixes(text: str) -> str:
    """Remove leading politeness and framing, repeatedly.

    Repeated because they stack: "can you please help me fix the shader" has
    three of them in a row, and stopping after the first would leave a goal
    that begins "please help me".
    """
    working = text
    while True:
        for prefix in _PREFIXES_LONGEST_FIRST:
            if working == prefix:
                return ""
            if working.startswith(f"{prefix} "):
                working = working[len(prefix) + 1:]
                break
        else:
            return working


def classify_goal(text: str) -> str | None:
    """The goal a message states, or None if it states none.

    The returned string is canonical: lowercased, stripped of punctuation
    and framing, beginning with the action asked for. Two phrasings of the
    same request -- "help me fix the shader error", "Could you fix the
    shader error?" -- normalize to the same goal, which is what lets the
    tracker recognise that no new goal was started.
    """
    normalized = _normalized(text)
    if not normalized:
        return None

    body = _strip_prefixes(normalized)
    if not body:
        return None

    for verb in _VERBS_LONGEST_FIRST:
        if body == verb:
            return None  # a bare verb names no object
        if not body.startswith(f"{verb} "):
            continue

        remainder = body[len(verb) + 1:].strip()
        if not remainder or remainder in OBJECTLESS:
            # "fix this" -- an action pointed at whatever is already being
            # discussed. That is continuity, not a new goal.
            return None
        return body[:GOAL_CHARS].strip()

    return None
