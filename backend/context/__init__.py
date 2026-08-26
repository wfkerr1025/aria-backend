"""ARIA Lite Phase 7 - conversation context.

Topic segmentation: splitting a conversation into threads by what each
message is about, so retrieval and synthesis can read the part of the
history that is relevant and ignore the rest.

    topic_segments      the shapes -- TopicSegment, TopicThread
    topic_classifier    what one message is about (deterministic)
    topic_segmentation  a conversation -> threads
    current_topic       threads -> the topic being discussed now

Nothing here reaches into retrieval or synthesis; both of those take a
topic as an optional input. A conversation can be segmented, printed and
asserted on without a database, an embedding backend or a model.
"""

from __future__ import annotations

__all__ = [
    "TopicSegment",
    "TopicThread",
    "classify_topic",
    "current_topic_of",
    "current_topic_thread",
    "resolve_current_topic",
    "segment_history",
]


def __getattr__(name: str):
    """Re-export the public API without importing every submodule eagerly."""
    if name in ("TopicSegment", "TopicThread"):
        from backend.context import topic_segments

        return getattr(topic_segments, name)
    if name == "classify_topic":
        from backend.context.topic_classifier import classify_topic

        return classify_topic
    if name == "segment_history":
        from backend.context.topic_segmentation import segment_history

        return segment_history
    if name in ("current_topic_of", "current_topic_thread", "resolve_current_topic"):
        from backend.context import current_topic

        return getattr(current_topic, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
