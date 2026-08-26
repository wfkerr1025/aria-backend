"""ARIA Lite Phase 7.3 - choosing which past turns are worth carrying.

A long conversation is mostly irrelevant to its own latest question. Fifty
turns in, the user asking "why is it still failing" is served by four of
them and actively harmed by the other forty-six: every off-topic turn is
another chance for retrieval to search the wrong vocabulary and for
synthesis to answer the wrong question.

So the history is scored and cut rather than truncated. Truncation -- keep
the last N turns -- is the usual approach and it is wrong in the one case
that matters: the turn that stated the goal is the oldest one in the
conversation, and a window big enough to keep it is a window big enough to
keep everything that displaced it.

The weights encode a strict ordering rather than a blend:

    topic     4    is this even the same subject
    goal      3    was it part of this piece of work
    recency   2    is it still likely to be true
    semantic  1    does it happen to share words

Strict because 4 > 3 + 2 + 1 - 3: a turn on the current topic outranks any
turn that is not, whatever else it has going for it. That is the property
worth having. A blended score would let three weak signals outvote the one
strong one, and "recent, wordy, and about something else" is exactly the
turn that poisons a long context.

The threshold falls out of the same arithmetic. At 3.0, a turn needs the
topic (4) or the goal (3) to survive; recency and word overlap together cap
at 3.0, so a turn from neither the current subject nor the current task
gets in only by being both brand new and a word-for-word match -- which is
the one case where it deserves to.

No embeddings, no model, no clock. The reference time is taken from the
conversation itself, so replaying a history a week later selects exactly
what it selected the first time.
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    from backend.aria_synthesis.evidence_bundle import shorten
    from backend.context.evidence_filters import (
        goal_filter,
        recency_filter,
        semantic_filter,
        topic_filter,
    )
    from backend.context.goal_tracking import goal_timeline
except ImportError:  # running from inside the backend directory
    from aria_synthesis.evidence_bundle import shorten
    from context.evidence_filters import (
        goal_filter,
        recency_filter,
        semantic_filter,
        topic_filter,
    )
    from context.goal_tracking import goal_timeline

__all__ = [
    "DEFAULT_TOP_K",
    "SCORE_THRESHOLD",
    "TURN_SNIPPET_CHARS",
    "WEIGHT_GOAL",
    "WEIGHT_RECENCY",
    "WEIGHT_SEMANTIC",
    "WEIGHT_TOPIC",
    "EvidenceSelector",
    "EvidenceTurn",
    "score_turn",
    "select_turns",
]

WEIGHT_TOPIC = 4.0
WEIGHT_GOAL = 3.0
WEIGHT_RECENCY = 2.0
WEIGHT_SEMANTIC = 1.0

# A turn must reach this to be carried. See the module docstring for why
# this number is the topic-or-goal line rather than an arbitrary cut.
SCORE_THRESHOLD = 3.0

# How many turns are carried at most. Ten is roughly five exchanges, which
# is as much conversation as an answer can take into account before the
# retrieved evidence stops being the thing it is built from.
DEFAULT_TOP_K = 10

# Turns are shown to the model as snippets, not in full: a carried turn is
# there to remind, and a paragraph of it would compete with the evidence.
TURN_SNIPPET_CHARS = 160


@dataclass(frozen=True)
class EvidenceTurn:
    """One past message, with everything selection judged it on."""

    message_id: str
    role: str
    text: str
    timestamp: float
    topic: str
    goal: str | None = None
    score: float = 0.0
    # Where this turn sat in the original conversation. Kept because
    # selection deliberately returns turns out of order, and "how much was
    # skipped between the turns we kept" is a question only the original
    # positions can answer.
    position: int = -1

    @property
    def snippet(self) -> str:
        return shorten(self.text, TURN_SNIPPET_CHARS)

    @property
    def label(self) -> str:
        return f"[{self.role}] {self.snippet}"


def score_turn(turn: EvidenceTurn, current_topic: str, current_goal, now: float, query: str):
    """Score one turn, returning the total and each signal that made it.

    The parts come back alongside the total because a selection nobody can
    explain is a selection nobody will trust: when the wrong four turns are
    carried, the useful question is which signal put them there.
    """
    parts = {
        "topic": topic_filter(turn, current_topic),
        "goal": goal_filter(turn, current_goal),
        "recency": recency_filter(turn, now),
        "semantic": semantic_filter(turn, query),
    }
    total = (
        WEIGHT_TOPIC * parts["topic"]
        + WEIGHT_GOAL * parts["goal"]
        + WEIGHT_RECENCY * parts["recency"]
        + WEIGHT_SEMANTIC * parts["semantic"]
    )
    return total, parts


class EvidenceSelector:
    """Picks the past turns worth carrying into the current question.

    Stateless -- the thresholds and weights are the only configuration, and
    they are constructor arguments so a caller can widen or narrow selection
    without editing the module. The default instance is what the rest of the
    stack uses.
    """

    def __init__(
        self,
        top_k: int = DEFAULT_TOP_K,
        threshold: float = SCORE_THRESHOLD,
    ) -> None:
        self.top_k = top_k
        self.threshold = threshold

    def turns_of(self, messages) -> list[EvidenceTurn]:
        """Every message as an unscored turn, with topic and goal attached.

        Both come from replaying the conversation through the same
        segmentation and goal tracking retrieval uses, so a turn is judged
        against the topic and goal it was actually part of rather than
        against a fresh reading of its own text.
        """
        turns = []
        for index, (segment, goal) in enumerate(goal_timeline(messages)):
            turns.append(
                EvidenceTurn(
                    message_id=segment.message_id,
                    role=segment.role,
                    text=segment.text,
                    timestamp=segment.timestamp,
                    topic=segment.topic,
                    goal=goal.goal if goal is not None else None,
                    position=index,
                )
            )
        return turns

    def select(
        self,
        messages,
        current_topic: str,
        current_goal=None,
        query: str = "",
        now: float | None = None,
    ) -> list[EvidenceTurn]:
        """The turns worth carrying, best first.

        now defaults to the latest timestamp in the conversation rather than
        to the wall clock. Age is measured against the end of the history,
        which is both reproducible and the right reference: a turn is old
        relative to the conversation it sits in, not relative to whenever
        somebody happens to replay it.

        The input messages are never modified, and the returned turns are
        frozen records rather than views onto them.
        """
        turns = self.turns_of(messages)
        if not turns:
            return []

        reference = now if now is not None else max(turn.timestamp for turn in turns)

        scored = []
        for position, turn in enumerate(turns):
            total, _parts = score_turn(turn, current_topic, current_goal, reference, query)
            if total >= self.threshold:
                scored.append((total, position, turn))

        # Sorted by score, then by position so that turns scoring equally --
        # the common case, since most survivors match on topic alone -- come
        # back in the order they were said. A stable, explainable order beats
        # one that depends on how the list happened to be built.
        scored.sort(key=lambda entry: (-entry[0], entry[1]))

        from dataclasses import replace

        return [replace(turn, score=total) for total, _position, turn in scored[: self.top_k]]


def select_turns(
    messages,
    current_topic: str,
    current_goal=None,
    query: str = "",
    now: float | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> list[EvidenceTurn]:
    """Convenience wrapper around a default EvidenceSelector."""
    return EvidenceSelector(top_k=top_k).select(
        messages, current_topic, current_goal, query=query, now=now
    )
