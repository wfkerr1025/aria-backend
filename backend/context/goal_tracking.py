"""ARIA Lite Phase 7.2 - keeping track of what the user is working on.

Walks a conversation and maintains a stack of goals, so that a turn saying
only "why is it still failing" can still be answered against the thing the
user has been trying to do for the last six messages.

The rules, in the order they are applied to each message:

    A stated goal pushes. "Help me fix the shader error" starts new work,
    and the goal it displaces is suspended rather than discarded -- the user
    may come back to it, and a stack is what makes coming back possible.

    A resumption marker goes back. "anyway", "as I was saying", "back to
    that" say the last thing discussed was a detour, so a goal the user
    actually stated is resumed and the implicit goals stacked above it are
    dropped. Two ways in: a message that names no topic returns to the
    nearest stated goal, and a message that names the topic stated work was
    left on returns to that. A message naming any other subject is a change
    of subject and falls through. Never over a marker pointing at the turn
    just gone, because "anyway, continue" is an instruction to carry on.

    A continuity marker updates. "continue", "next", "fix this" say the
    work has not changed, so the goal on top stays and is only marked as
    still current. A continuation marker keeps the goal's own topic even
    when the message classifies elsewhere; other markers accept a new
    topic, because "anyway" and "okay now" genuinely can redirect -- but
    only a topic that was actually identified, never "general".

    Anything else follows the topic. A user message on the current goal's
    topic is more of the same work. One on a different topic starts implicit
    work -- a goal named after the topic, marked explicit=False so a reader
    can see it was inferred rather than stated.

    Assistant messages change nothing. ARIA does not get to decide what the
    user is trying to do, for the same reason it does not get to set the
    topic: an assistant that could set the goal could walk the conversation
    somewhere the user never asked to go and then scope retrieval to it.

Nothing is mutated. Every step returns a new stack holding new states, so a
stack captured at one turn still describes that turn afterwards.
"""

from __future__ import annotations

try:
    from backend.context.continuity_markers import (
        detect_continuity,
        has_immediate_marker,
        implies_continuation,
    )
    from backend.context.goal_classifier import classify_goal
    from backend.context.goal_state import (
        SOURCE_CARRY_FORWARD,
        SOURCE_EXPLICIT,
        SOURCE_IMPLICIT,
        SOURCE_RESUMPTION,
        GoalStack,
        GoalState,
    )
    from backend.context.resumption_markers import detect_resumption
    from backend.context.topic_segmentation import resolve_segments
    from backend.context.topic_segments import GENERAL_TOPIC, message_field
except ImportError:  # running from inside the backend directory
    from context.continuity_markers import (
        detect_continuity,
        has_immediate_marker,
        implies_continuation,
    )
    from context.goal_classifier import classify_goal
    from context.goal_state import (
        SOURCE_CARRY_FORWARD,
        SOURCE_EXPLICIT,
        SOURCE_IMPLICIT,
        SOURCE_RESUMPTION,
        GoalStack,
        GoalState,
    )
    from context.resumption_markers import detect_resumption
    from context.topic_segmentation import resolve_segments
    from context.topic_segments import GENERAL_TOPIC, message_field

__all__ = ["current_goal_of", "goal_timeline", "track_goals", "update_goal_stack"]


def _text_of(message) -> str:
    return str(message_field(message, "content", "text", "message", default="") or "")


def _role_of(message) -> str:
    return str(message_field(message, "role", default="user") or "user")


def _stamp_of(message, at: float | None) -> float:
    if at is not None:
        return float(at)
    return float(message_field(message, "timestamp", "ts", "created_at", default=0.0) or 0.0)


def update_goal_stack(
    goal_stack: GoalStack,
    message,
    topic: str,
    at: float | None = None,
) -> GoalStack:
    """Apply one message to the goal stack and return the new stack.

    topic is what topic segmentation made of this message. at fixes the
    timestamp recorded on the goal, and defaults to the message's own; it is
    a parameter so a caller replaying a history gets reproducible stamps
    rather than a clock reading.

    The input stack is never modified.
    """
    stack = goal_stack if isinstance(goal_stack, GoalStack) else GoalStack()
    text = _text_of(message)
    topic = topic or GENERAL_TOPIC
    when = _stamp_of(message, at)

    # 4.3, taken first because it is a rule about who may speak: an
    # assistant message is never allowed to start, change or refresh a goal.
    if _role_of(message) != "user":
        return stack

    stated = classify_goal(text)
    if stated:
        current = stack.current
        if current is not None and current.goal == stated:
            # The same request in different words. Refreshing beats pushing
            # a duplicate, which would leave two identical goals on the
            # stack and make "go back to the previous goal" a no-op.
            return stack.replaced_top(current.touched(when, SOURCE_EXPLICIT))
        return stack.pushed(
            GoalState(
                goal=stated,
                topic=topic,
                created_at=when,
                last_updated=when,
                explicit=True,
                continuity_source=SOURCE_EXPLICIT,
            )
        )

    marker = detect_continuity(text)
    current = stack.current

    # A return to suspended work, checked before the continuity rules
    # because it answers a question they cannot: which goal. Never over a
    # marker in the same message that points at the turn just gone --
    # "anyway, continue" is an instruction to carry on.
    if detect_resumption(text) and not has_immediate_marker(text):
        # A message that names no topic is leaning entirely on the marker,
        # so the nearest stated goal is what it must mean.
        if topic == GENERAL_TOPIC:
            resumed = stack.resume()
            if resumed is not None:
                return stack.resumed_to(resumed, when)

        # A message that does name one resumes only if that topic is where
        # stated work was left. "Anyway, the prefab is broken too" is a
        # return; "anyway, what is the forecast" names a subject nothing was
        # suspended on, so it is a change of subject and falls through.
        suspended = stack.find_suspended_explicit_goal_by_topic(topic)
        if suspended is not None:
            return stack.resumed_to(suspended, when)

    if marker:
        if current is None:
            # Nothing to continue. A marker on its own names no work, so no
            # goal is invented from it.
            return stack
        if (
            topic == current.topic
            or implies_continuation(marker)
            # "general" is not a topic, it is the absence of one. A
            # follow-up like "why is it still failing" names nothing, so
            # reading it as a move would strip the goal of the topic it was
            # created with at exactly the moment the user is continuing.
            # A goal only moves to a topic that was positively identified.
            or topic == GENERAL_TOPIC
        ):
            return stack.replaced_top(current.touched(when, marker))
        return stack.replaced_top(current.moved_to(topic, when, marker))

    if current is not None and topic == current.topic:
        return stack.replaced_top(current.touched(when, SOURCE_CARRY_FORWARD))

    if current is not None and topic == GENERAL_TOPIC:
        # An unclassifiable message during active work is not a new subject;
        # it is the same work described without keywords. Treated as
        # carry-forward for the same reason resolve_current_topic carries a
        # topic across one: reading it as a change loses the thread.
        return stack.replaced_top(current.touched(when, SOURCE_CARRY_FORWARD))

    return stack.pushed(
        GoalState(
            goal=topic,
            topic=topic,
            created_at=when,
            last_updated=when,
            explicit=False,
            continuity_source=SOURCE_IMPLICIT,
        )
    )


def track_goals(messages, goal_stack: GoalStack | None = None) -> GoalStack:
    """Replay a whole conversation into a goal stack.

    Each message is applied with the topic segmentation resolved for it, so
    an assistant reply filed under its question's topic and a follow-up
    carrying a topic forward are both seen here the same way they are seen
    by retrieval. Timestamps fall back to message position, which keeps a
    history with no clock reproducible.

    goal_stack seeds the walk for a caller that keeps one across turns;
    omitted, the whole history is replayed from empty.
    """
    stack = goal_stack if isinstance(goal_stack, GoalStack) else GoalStack()
    for index, (message, segment) in enumerate(
        zip(messages or [], resolve_segments(messages))
    ):
        stack = update_goal_stack(stack, message, segment.topic, at=segment.timestamp or index)
    return stack


def goal_timeline(messages, goal_stack: GoalStack | None = None) -> list:
    """The goal in force at each message, aligned with the input.

    Returns one entry per message -- (TopicSegment, GoalState or None) --
    recording what the user was working on *as of* that message, after it
    was applied. Selecting past turns by goal needs this: "which turns
    belong to the current goal" is unanswerable from a final stack, which
    only knows what is being worked on now.

    Replays through the same update_goal_stack every other caller uses, so
    the timeline and the final stack can never disagree about a message.
    """
    stack = goal_stack if isinstance(goal_stack, GoalStack) else GoalStack()
    timeline = []
    for index, (message, segment) in enumerate(
        zip(messages or [], resolve_segments(messages))
    ):
        stack = update_goal_stack(stack, message, segment.topic, at=segment.timestamp or index)
        timeline.append((segment, stack.current))
    return timeline


def current_goal_of(messages, goal_stack: GoalStack | None = None) -> GoalState | None:
    """The goal a conversation is currently pursuing, or None."""
    return track_goals(messages, goal_stack).current
