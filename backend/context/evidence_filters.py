"""ARIA Lite Phase 7.3 - the four signals a past turn is judged on.

Each answers one question about whether a message from earlier in the
conversation is worth carrying into this one:

    topic     is it about the same subject?
    goal      was it part of the same piece of work?
    recency   was it said recently enough to still be true?
    semantic  does it share words with what is being asked now?

Kept in their own module and their own functions because they are the part
most likely to be argued with. Someone tuning selection wants to read four
short rules and change one of them, not find them inlined in a scoring
loop; and each is separately testable, which is what makes "the topic
filter regressed" a sentence anyone can act on.

Deliberately no embeddings anywhere, including in the one called
"semantic". This runs on every turn of every conversation before retrieval
starts, and paying an encoder pass per past message to decide whether to
read it would cost more than reading it. Word overlap is a weak signal, and
it is weighted last precisely because it is weak.
"""

from __future__ import annotations

import re

try:
    from backend.llm.query_expansion import tokenize
except ImportError:  # running from inside the backend directory
    from llm.query_expansion import tokenize

__all__ = [
    "RECENCY_WINDOW_SECONDS",
    "goal_filter",
    "recency_filter",
    "semantic_filter",
    "topic_filter",
]

# How long a turn keeps any recency value at all. A turn from the start of
# the window scores 1.0 and one at its end scores 0.0, falling off linearly
# in between.
#
# The spec calls this a ten-minute half-life; the formula it gives is linear
# decay to zero, which is not a half-life -- an exponential one would still
# be worth 0.5 at ten minutes and never reach zero. The formula is what is
# implemented, because a filter whose behaviour matches its stated arithmetic
# is easier to reason about than one that matches a word in its name.
RECENCY_WINDOW_SECONDS = 600.0


def topic_filter(turn, current_topic: str) -> int:
    """1 when the turn is on the conversation's current topic, else 0.

    Deliberately binary. A partial credit for "related topics" would need a
    topic-similarity table, and the whole point of segmentation was to make
    this question have a yes-or-no answer.
    """
    return 1 if turn.topic and turn.topic == current_topic else 0


def goal_filter(turn, current_goal) -> int:
    """1 when the turn belongs to the goal now being pursued, else 0.

    A turn with no goal scores 0 rather than being excluded: it was said
    before any work was under way, which makes it weaker evidence than a
    turn from the current task but not disqualifying on its own.
    """
    if current_goal is None or turn.goal is None:
        return 0
    wanted = getattr(current_goal, "goal", current_goal)
    return 1 if turn.goal == wanted else 0


def recency_filter(turn, now: float) -> float:
    """How fresh a turn is, from 1.0 down to 0.0.

    Never negative -- an hour-old turn and a day-old turn are equally not
    recent, and letting the value go negative would let age cancel out a
    topic match, which is not a trade this scoring is meant to make.

    now is passed in rather than read from a clock. Selection has to be
    reproducible: the same history judged twice must select the same turns,
    and it cannot if the answer depends on how long the second call waited.
    """
    age = float(now) - float(turn.timestamp)
    if age <= 0:
        # Same instant, or a turn stamped after the reference point. Both
        # mean "as recent as it gets" rather than "from the future".
        return 1.0
    return max(0.0, 1.0 - age / RECENCY_WINDOW_SECONDS)


def semantic_filter(turn, query: str) -> float:
    """The share of the query's words that appear in the turn.

    Whole-word matching, so "build" is not satisfied by "rebuild" -- the
    same rule keyword coverage uses on notes, for the same reason.

    Returns 0.0 for a query with no content words rather than dividing by
    zero. A query of nothing but stopwords says nothing about which turns
    are relevant, and this signal should abstain rather than guess.
    """
    terms = tokenize(query)
    if not terms:
        return 0.0

    haystack = f" {' '.join(str(turn.text or '').lower().split())} "
    present = sum(
        1
        for term in terms
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", haystack) is not None
    )
    return present / len(terms)
