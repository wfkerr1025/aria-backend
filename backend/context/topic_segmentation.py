"""ARIA Lite Phase 7.1 - splitting a conversation into topic threads.

Walks the messages once, in order, asks what each is about, and collects
them into one thread per topic.

Pure: the messages that go in are never touched, and nothing is read except
their own fields. A caller can segment the same history twice, or hand the
same list to two subsystems, without either seeing the other's work.

Threads come back ordered by when each was last spoken on, most recent
first. That ordering is the useful one for the question this exists to
answer -- what is being talked about now -- and it falls back to message
position rather than relying on timestamps, which are optional and,
when a client supplies them, occasionally wrong.
"""

from __future__ import annotations

from dataclasses import replace

try:
    from backend.context.topic_classifier import classify_topic
    from backend.context.topic_segments import (
        GENERAL_TOPIC,
        TopicSegment,
        TopicThread,
        message_field,
    )
except ImportError:  # running from inside the backend directory
    from context.topic_classifier import classify_topic
    from context.topic_segments import (
        GENERAL_TOPIC,
        TopicSegment,
        TopicThread,
        message_field,
    )

__all__ = ["resolve_segments", "segment_history", "segment_of", "thread_for"]


def segment_of(message, index: int = 0) -> TopicSegment:
    """Read one conversation message into a segment.

    A missing id becomes the message's position, which is stable for a
    history that only ever grows and is enough to tell two segments apart.
    A missing timestamp becomes the position too, so a history with no clock
    at all still orders threads by how recently each was spoken on -- the
    numbers are meaningless as times, but their order is exactly right.
    """
    text = message_field(message, "content", "text", "message", default="")
    return TopicSegment(
        message_id=str(message_field(message, "message_id", "id", default=index)),
        role=str(message_field(message, "role", default="user")),
        text=str(text),
        timestamp=float(
            message_field(message, "timestamp", "ts", "created_at", default=index) or index
        ),
        topic=classify_topic(text),
    )


def resolve_segments(messages, attach_replies: bool = True) -> list[TopicSegment]:
    """Every message as a segment, in conversation order, topics resolved.

    Separated from segment_history because two callers want different
    shapes of the same work: threads are the useful form for asking what a
    conversation has been about, while goal tracking has to replay the
    messages in the order they were said. Deriving both from one pass is
    what stops them from disagreeing about which topic a given message was
    filed under.

    See segment_history for what attach_replies does.
    """
    segments: list[TopicSegment] = []
    open_topic = GENERAL_TOPIC

    for index, message in enumerate(messages or []):
        segment = segment_of(message, index)
        if segment.is_user:
            if segment.topic != GENERAL_TOPIC:
                open_topic = segment.topic
        elif attach_replies and segment.topic == GENERAL_TOPIC:
            segment = replace(segment, topic=open_topic)
        segments.append(segment)
    return segments


def segment_history(messages, attach_replies: bool = True) -> list[TopicThread]:
    """Split a conversation into one thread per topic, most recent first.

    Messages keep their original order inside each thread. Threads are
    ordered by last_updated, and ties -- which are the norm when a history
    carries no timestamps -- fall back to how recently the thread was spoken
    on, so the ordering is total and reproducible rather than dependent on
    which topic happened to be seen first.

    attach_replies files an assistant message under the topic of the
    question it answered, when its own words name no topic. Answers rarely
    repeat the subject -- "try reimporting that" is the reply to a question
    about a prefab and contains nothing about Unity -- so classifying them
    on their own text alone scatters ARIA's half of every conversation into
    a general thread. That matters because the thread is what gets read back
    as context, and a context holding the questions but not the answers has
    lost the more informative half.

    An assistant message that does name a topic keeps its own: a reply
    introducing a new subject is a real topic change, not an echo. Pass
    attach_replies=False to classify every message purely on its own text.
    """
    ordered: dict[str, list[TopicSegment]] = {}
    last_position: dict[str, int] = {}
    last_stamp: dict[str, float] = {}

    for index, segment in enumerate(resolve_segments(messages, attach_replies)):
        ordered.setdefault(segment.topic, []).append(segment)
        last_position[segment.topic] = index
        last_stamp[segment.topic] = segment.timestamp

    threads = [
        TopicThread(topic=topic, segments=list(segments), last_updated=last_stamp[topic])
        for topic, segments in ordered.items()
    ]
    threads.sort(
        key=lambda thread: (thread.last_updated, last_position[thread.topic]),
        reverse=True,
    )
    return threads


def thread_for(threads, topic: str) -> TopicThread:
    """The thread on one topic, or an empty thread when there is none.

    Returns an empty thread rather than None so a caller scoping context to
    the current topic gets "no messages" instead of an attribute error on a
    conversation that has not reached that topic yet.
    """
    for thread in threads or []:
        if thread.topic == topic:
            return thread
    return TopicThread(topic=topic or GENERAL_TOPIC, segments=[], last_updated=0.0)
