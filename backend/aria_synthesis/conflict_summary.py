"""ARIA Lite Phase 6.2 - putting a detected conflict into words.

Turns Conflict objects into one neutral sentence each, for the prompt to
show the model before it starts answering.

Neutral is the whole job. These lines are read by a model that is about to
decide what to tell the user, and a summary that leans -- "the correct value
is 4" or "an outdated note claims 3" -- resolves the disagreement here,
silently, using no evidence at all. The templates below can only report that
sources differ and what they differ about.

No provenance appears in a summary, deliberately. The evidence section of
the prompt already carries every citation, and repeating them here would
invite the model to cite the summary rather than the source it came from.
The summary says what is disputed; the evidence says who said what.

Two templates, one per detected kind. Both are fixed strings with the
evidence's own words dropped in, so the same conflict always summarizes
identically -- no model, no paraphrase, no drift between two runs of the
same query.
"""

from __future__ import annotations

try:
    from backend.aria_synthesis.conflict_detection import (
        KIND_NUMERIC,
        Conflict,
    )
    from backend.aria_synthesis.evidence_bundle import shorten
except ImportError:  # running from inside the backend directory
    from aria_synthesis.conflict_detection import KIND_NUMERIC, Conflict
    from aria_synthesis.evidence_bundle import shorten

__all__ = [
    "CROSS_GOAL_LEAD",
    "CROSS_TOPIC_LEAD",
    "SAME_TOPIC_LEAD",
    "SUMMARY_CHARS",
    "summarize_conflict",
    "summarize_conflicts",
]

# How a summary opens. The cross-topic wording is a hedge, and deliberately
# so: when two sources are not even about the same subject, "they disagree"
# overstates it. Very often they are describing different systems that share
# a word, and the useful thing to tell the model is that the sources are
# from different topics, not that one of them is wrong.
SAME_TOPIC_LEAD = "Sources"
CROSS_TOPIC_LEAD = "Sources from different topics"

# Used when the disagreement reaches outside the work in hand. Checked
# first, because "some of this is not about what you are doing" is the more
# actionable warning of the two -- a reader can decide to ignore the
# outside source, where "these are different topics" only says they differ.
CROSS_GOAL_LEAD = "Sources from outside the current goal"

# A summary is a signpost, not a paragraph. Long enough to name the
# proposition in dispute, short enough that several of them do not crowd out
# the evidence they are about.
SUMMARY_CHARS = 140


def _clause(text: str) -> str:
    """Reshape a sentence so it can follow "whether".

    Trailing punctuation comes off and the first letter is lowered, which
    turns "The pipeline requires a rebuild." into something that reads
    correctly mid-sentence. An all-caps opening word is left alone so
    "NOAA reports..." does not become "nOAA reports...".
    """
    cleaned = " ".join(str(text or "").split()).rstrip(".!?;,")
    if not cleaned:
        return ""

    head, _, tail = cleaned.partition(" ")
    if not head.isupper():
        head = head[:1].lower() + head[1:]
    return shorten(f"{head} {tail}".strip(), SUMMARY_CHARS)


def summarize_conflict(conflict: Conflict) -> str:
    """One neutral sentence describing what a single conflict is about."""
    detail = conflict.detail or {}
    if getattr(conflict, "cross_goal", False):
        lead = CROSS_GOAL_LEAD
    elif getattr(conflict, "cross_topic", False):
        lead = CROSS_TOPIC_LEAD
    else:
        lead = SAME_TOPIC_LEAD

    if conflict.kind == KIND_NUMERIC:
        values = [str(value) for value in detail.get("values") or []]
        noun = detail.get("noun") or conflict.topic
        if len(values) >= 2:
            listed = ", ".join(values[:-1]) + f" and {values[-1]}"
            return f"{lead} give different values for {noun}: {listed}."
        return f"{lead} give different values for {noun}."

    clause = _clause(detail.get("affirmative") or conflict.topic)
    if clause:
        return f"{lead} disagree on whether {clause}."
    return f"{lead} disagree about {conflict.topic}."


def summarize_conflicts(conflicts) -> list[str]:
    """Summarize every conflict, in order, without repeating a line.

    Duplicates are dropped rather than deduplicated by conflict, because two
    differently-grouped conflicts can legitimately reduce to the same
    sentence -- and the same warning twice reads as two problems.
    """
    summaries: list[str] = []
    for conflict in conflicts or []:
        summary = summarize_conflict(conflict)
        if summary not in summaries:
            summaries.append(summary)
    return summaries
