"""ARIA Lite Phase 7.1 - conversation segments and threads.

The shapes a segmented conversation is held in.

A long conversation is rarely about one thing. Someone asks about their
Unity build, then about the weather, then goes back to the build -- and by
the time they say "why is it still failing", the three most recent messages
are about rain. Retrieval that reads "recent context" as "the last few
messages" then searches for a weather-flavoured build failure, which is the
poisoning this phase exists to prevent.

So the conversation is split by what each message is about, not by when it
arrived. A thread collects every message on one topic, in the order they
were said, and "the current topic" becomes a question with an answer rather
than an assumption about recency.

Both structures are frozen. A thread is a reading of a conversation that
already happened; nothing downstream should be able to edit history by
appending to it, and "append-only" is easiest to guarantee when the object
cannot be appended to at all -- segmentation builds each thread once, whole.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "GENERAL_TOPIC",
    "TopicSegment",
    "TopicThread",
    "message_field",
]

# The topic a message gets when nothing else fits. Named here rather than
# spelled as a literal in four modules, because it is the one topic string
# that every layer has to agree on.
GENERAL_TOPIC = "general"


def message_field(message, *names, default=None):
    """Read the first present field from a message, whatever it is called.

    Conversation messages reach this code from several places -- the
    websocket handlers, conversation_manager, a test fixture -- and they do
    not agree on whether the body is "content" or "text", or whether an id
    is "id" or "message_id". Rather than force one spelling on callers who
    already have their own, every read goes through here.

    Objects are supported alongside dicts so a caller with its own Message
    class does not have to convert first.
    """
    for name in names:
        if isinstance(message, dict):
            if message.get(name) is not None:
                return message[name]
        elif getattr(message, name, None) is not None:
            return getattr(message, name)
    return default


@dataclass(frozen=True)
class TopicSegment:
    """One message, with the topic it was found to be about."""

    message_id: str
    role: str
    text: str
    timestamp: float
    topic: str

    @property
    def is_user(self) -> bool:
        return self.role == "user"


@dataclass(frozen=True)
class TopicThread:
    """Every message on one topic, oldest first.

    segments keep the order the messages were said in rather than being
    re-sorted by timestamp. A conversation is a sequence, and its order is
    the one thing about it that is never in doubt -- where timestamps can be
    absent, equal, or supplied by a client with a wrong clock.
    """

    topic: str
    segments: list[TopicSegment] = field(default_factory=list)
    last_updated: float = 0.0

    def __len__(self) -> int:
        return len(self.segments)

    @property
    def texts(self) -> list[str]:
        return [segment.text for segment in self.segments]

    @property
    def user_segments(self) -> list[TopicSegment]:
        return [segment for segment in self.segments if segment.is_user]

    def latest_user_text(self) -> str:
        """The most recent thing the user said on this topic, or ""."""
        users = self.user_segments
        return users[-1].text if users else ""
