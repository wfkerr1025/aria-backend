"""ARIA Lite Phase 7.4 - what a long conversation's shape means for the answer.

By this point five separate things know something about scope: the bundle
knows which topics its evidence came from, the goal stack knows what is
being worked on, conflict detection knows which disagreements cross a
boundary, and selection knows how much history it skipped. Each of them was
saying so in its own line of the prompt, in its own words, wherever it
happened to be computed.

This module is where that becomes one analysis. Not because five lines is
too many, but because they were five *independent* judgements about the same
question -- how far this answer is reaching beyond what was asked -- and a
reader trying to work out why an answer wandered had to assemble them
mentally. One function, one output, one section.

Pure and deterministic: it reads a bundle, some turns, a topic and a goal,
and returns strings. No model, no clock, no database.

The output is data rather than prose-for-the-prompt. Every field is a plain
string or list of strings, so a caller can log the analysis, diff two of
them, or assert on one without parsing a prompt back apart -- and the prompt
builder stays free to render only what it has room for.
"""

from __future__ import annotations

__all__ = [
    "GAP_TURNS",
    "LongContextRules",
    "apply_long_context_rules",
]

# How many unselected turns between two selected ones counts as a gap worth
# warning about. Twenty is roughly ten exchanges -- long enough that the
# earlier turn is very likely talking about a different state of the world,
# short enough that an ordinary digression does not trip it.
GAP_TURNS = 20


class LongContextRules:
    """Reads the shape of a long conversation into a synthesis analysis.

    Stateless. Instantiated rather than called as a function because the
    thresholds are constructor arguments -- a caller running very long
    sessions may want a different gap -- and because `LongContextRules()`
    reads as the thing being applied.
    """

    def __init__(self, gap_turns: int = GAP_TURNS) -> None:
        self.gap_turns = gap_turns

    # ------------------------------------------------------------------
    def topic_focus(self, bundle, current_topic: str) -> str | None:
        """A note that the evidence reaches past the current topic, or None.

        None when every piece of evidence is on topic, because there is
        nothing to say: a line reporting that things are normal costs the
        same attention as one reporting that they are not, and a model that
        reads "most evidence relates to unity" on every single turn learns
        to skip the sentence on the turn it matters.
        """
        topic = current_topic or bundle.current_topic
        if not topic:
            return None

        others = sorted({item.topic for item in bundle.off_topic_items})
        if not others:
            return None
        return (
            f"Most evidence relates to {topic}; "
            f"some comes from other topics ({', '.join(others)})."
        )

    def goal_focus(self, bundle, current_goal) -> str | None:
        """A note that the evidence reaches past the current goal, or None.

        Same omission rule, and the same reason.
        """
        goal = getattr(current_goal, "goal", None) or bundle.current_goal
        if not goal:
            return None
        if not bundle.off_goal_items:
            return None
        return (
            f"Most evidence supports the goal: {goal}; "
            "some relates to other goals."
        )

    # ------------------------------------------------------------------
    def cross_topic_conflicts(self, conflicts) -> list[str]:
        """One line per disagreement whose sides come from different topics.

        These are the disagreements most likely to be illusory. Two sources
        on one subject that contradict each other are a fact in dispute; two
        sources on different subjects that appear to contradict each other
        are usually describing different systems that share a word.
        """
        return [
            f"Sources from different topics disagree on {conflict.topic}."
            for conflict in conflicts or []
            if getattr(conflict, "cross_topic", False)
        ]

    def cross_goal_conflicts(self, conflicts) -> list[str]:
        """One line per disagreement reaching outside the current goal."""
        return [
            f"Sources outside the current goal disagree on {conflict.topic}."
            for conflict in conflicts or []
            if getattr(conflict, "cross_goal", False)
        ]

    # ------------------------------------------------------------------
    def continuity_warnings(self, selected_turns, current_goal) -> list[str]:
        """Where the carried context is thinner than it looks.

        Both warnings are about the same illusion. A list of turns reads as
        a conversation, and a model given four turns will treat them as
        consecutive and as being about one thing -- so it is worth saying
        when they are neither.
        """
        warnings: list[str] = []
        turns = list(selected_turns or [])
        if not turns:
            return warnings

        ordered = sorted(
            (turn for turn in turns if getattr(turn, "position", -1) >= 0),
            key=lambda turn: turn.position,
        )
        skipped = max(
            (
                later.position - earlier.position - 1
                for earlier, later in zip(ordered, ordered[1:])
            ),
            default=0,
        )
        if skipped >= self.gap_turns:
            warnings.append(
                f"Large gap in conversation history ({skipped} turns not carried); "
                "continuity may be limited."
            )

        goal = getattr(current_goal, "goal", None) or None
        if goal and any(turn.goal and turn.goal != goal for turn in turns):
            warnings.append(
                "Some context comes from earlier goals; relevance may vary."
            )
        return warnings

    # ------------------------------------------------------------------
    def apply(
        self,
        bundle,
        selected_turns=None,
        current_topic: str = "",
        current_goal=None,
        conflicts=None,
    ) -> dict:
        """The whole analysis for one answer.

        conflicts is an extra optional argument rather than being read off
        the bundle, because the bundle deliberately does not hold them --
        detection is a reading of the evidence and lives in the step that
        acts on it, not in the record of what was retrieved. Passing None
        falls back to whatever the bundle happens to carry, so a caller that
        does keep conflicts on it still works.

        Every field is present in the result whether or not it has content,
        so a consumer never has to guard on a missing key; empty means
        nothing to say.
        """
        if conflicts is None:
            conflicts = getattr(bundle, "conflicts", None) or []

        turns = selected_turns if selected_turns is not None else getattr(bundle, "turns", [])

        return {
            "topic_focus": self.topic_focus(bundle, current_topic),
            "goal_focus": self.goal_focus(bundle, current_goal),
            "cross_topic_conflicts": self.cross_topic_conflicts(conflicts),
            "cross_goal_conflicts": self.cross_goal_conflicts(conflicts),
            "continuity_warnings": self.continuity_warnings(turns, current_goal),
        }


def apply_long_context_rules(
    bundle,
    selected_turns=None,
    current_topic: str = "",
    current_goal=None,
    conflicts=None,
) -> dict:
    """Convenience wrapper around a default LongContextRules."""
    return LongContextRules().apply(
        bundle, selected_turns, current_topic, current_goal, conflicts
    )
