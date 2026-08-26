"""ARIA Lite Phase 7.1 - which topic the conversation is on right now.

The user decides the topic, not the assistant. ARIA's own last message is
frequently about something the user did not ask for -- a clarifying
question, an aside, an apology for the previous answer -- and letting it set
the topic would mean the assistant could walk the conversation somewhere the
user never went, then scope retrieval to the place it walked to.

So the current topic is the topic of the most recent *user* message, and
assistant messages after it are ignored for this purpose however recent
they are.
"""

from __future__ import annotations

try:
    from backend.context.topic_segments import GENERAL_TOPIC, TopicThread
    from backend.context.topic_segmentation import segment_history, thread_for
except ImportError:  # running from inside the backend directory
    from context.topic_segments import GENERAL_TOPIC, TopicThread
    from context.topic_segmentation import segment_history, thread_for

__all__ = [
    "current_topic_of",
    "current_topic_thread",
    "resolve_current_topic",
]


def _latest_user_segments(threads) -> list:
    """Every user message across all threads, oldest first.

    Threads are flattened rather than the most recently updated one being
    trusted, because the newest thread may have been started by the
    assistant. A history whose last user message is about Unity is a Unity
    conversation even if ARIA has since said three things about the weather.
    """
    users = [
        segment
        for thread in threads or []
        for segment in thread.segments
        if segment.is_user
    ]
    # Timestamp first, then the order segmentation saw them in, so a history
    # with no clock at all still resolves to a definite "most recent".
    return sorted(users, key=lambda segment: segment.timestamp)


def resolve_current_topic(threads, carry_forward: bool = True) -> str:
    """The topic the conversation is currently on.

    The base rule is the specified one: the topic of the most recent user
    message, ignoring anything the assistant has said since, and "general"
    when the user has not spoken at all.

    carry_forward handles the case that base rule gets wrong, which is the
    one this whole phase exists for. A follow-up rarely restates its
    subject -- "why is it still failing", "and the other one?", "try again"
    -- so it classifies as general, and scoping retrieval to general is
    precisely the loss of thread that segmentation was meant to prevent. So
    an unclassifiable message inherits the topic of the last user message
    that had one, and a message that names a topic still changes it
    immediately.

    The cost is a real topic change phrased in general words: someone who
    stops talking about Unity to ask "what is for lunch" stays on unity for
    that turn. That is a mild wrong -- a few unhelpful synonyms -- against a
    frequent and much worse one, and it corrects itself the moment anything
    identifiable is said.

    Pass carry_forward=False for the literal rule, with no inheritance.
    """
    users = _latest_user_segments(threads)
    if not users:
        return GENERAL_TOPIC

    latest = users[-1]
    if latest.topic != GENERAL_TOPIC or not carry_forward:
        return latest.topic

    for segment in reversed(users[:-1]):
        if segment.topic != GENERAL_TOPIC:
            return segment.topic
    return GENERAL_TOPIC


def current_topic_of(messages, carry_forward: bool = True) -> str:
    """Segment a raw conversation and return the topic it is on."""
    return resolve_current_topic(segment_history(messages), carry_forward=carry_forward)


def current_topic_thread(messages, carry_forward: bool = True) -> TopicThread:
    """The messages of the current topic, and nothing from any other.

    This is the scoped context: everything a later step is allowed to read
    when it wants to know what the conversation has said about what is being
    discussed now. Handing back a thread rather than the raw history is what
    makes the scoping the default rather than a rule someone has to remember.
    """
    threads = segment_history(messages)
    return thread_for(
        threads, resolve_current_topic(threads, carry_forward=carry_forward)
    )
