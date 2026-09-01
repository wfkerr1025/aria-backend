"""ARIA Lite - a stream whose life is longer than one turn.

WHAT "MULTI-TURN STREAMING" MEANS HERE, AND WHAT IT DOES NOT
------------------------------------------------------------
Three readings, and only two of them are reachable in this codebase:

  A. One bubble that survives a tool call -- the model streams, calls
     a tool, and keeps streaming into the same bubble. REACHABLE, and
     what this module is for.

  B. A follow-up turn that attaches to the same bubble -- "continue"
     appends rather than opening a new reply. REACHABLE, and the first
     real caller of A's machinery.

  C. A persistent MODEL session -- KV-cache reuse across turns. NOT
     reachable. None of the providers in backend.llm.providers exposes
     a resumable session handle, so a design that assumed C would
     produce an API nothing could implement behind it. The prompt is
     still rebuilt from `messages` every turn, exactly as it is today.

WHY THIS IS NOT BUILT ON streaming_engine_v2
--------------------------------------------
The plan said it had to be, and the plan was wrong. StreamHandle's
cancel() is a nice thing to own, but the three things a session
actually needs -- suppressing a second stream_start, holding back
stream_end, and stopping delivery mid-flight -- all live at the
HANDLER boundary, where _turn_stream_id and delivery_stopped() already
are. The engine never had to know.

Migrating to v2 also means moving provider resolution out of the
engine (v2 takes a resolved ProviderAdapter; v1 resolves internally),
which touches model selection and the safety gate. That is a separate
change with its own risk, and coupling it to this one would have made
both harder to trust.

SESSIONS ARE OPT-IN
-------------------
A turn that opens no session behaves exactly as it does today. That is
deliberate: the common path is one question and one answer, and making
every turn pay for machinery it does not use would be the wrong trade
for the case that happens most.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "SessionRegistry",
    "StreamSession",
    "TurnContext",
]

OPEN = "open"
CLOSED = "closed"

# How long an idle session may sit before it is no longer attachable.
# A session is a bubble the client still has open; one left from an
# hour ago is a bubble that has scrolled out of the world.
IDLE_SECONDS = 300.0


@dataclass
class TurnContext:
    """One request/response inside a session. PER-TURN state."""

    turn_id: str
    user_text: str
    started_at: float
    finished_at: Optional[float] = None
    outcome: Optional[str] = None          # answered | stopped | timeout | error
    tool_runs: list = field(default_factory=list)

    def close(self, outcome: str) -> None:
        self.finished_at = time.monotonic()
        self.outcome = outcome


@dataclass
class StreamSession:
    """A bubble that outlives the turn that opened it.

    PER-SESSION state -- everything here survives a turn boundary.
    Anything that must not is on the handler and cleared per turn.
    """

    session_id: str
    conversation_id: str
    model_id: Optional[str]
    stream_id: object                      # the requestId the client opened
    opened_at: float
    state: str = OPEN
    turns: List[TurnContext] = field(default_factory=list)

    @property
    def open(self) -> bool:
        return self.state == OPEN

    @property
    def idle_for(self) -> float:
        last = self.turns[-1].finished_at if self.turns else None
        return time.monotonic() - (last or self.opened_at)

    def begin_turn(self, user_text: str) -> TurnContext:
        turn = TurnContext(turn_id=uuid.uuid4().hex[:12],
                           user_text=str(user_text or ""),
                           started_at=time.monotonic())
        self.turns.append(turn)
        return turn

    def close(self, outcome: str = "answered") -> None:
        self.state = CLOSED
        if self.turns and self.turns[-1].finished_at is None:
            self.turns[-1].close(outcome)

    def as_dict(self) -> dict:
        return {
            "sessionId": self.session_id,
            "conversationId": self.conversation_id,
            "modelId": self.model_id,
            "streamId": self.stream_id,
            "state": self.state,
            "turns": len(self.turns),
            "idleFor": round(self.idle_for, 1),
        }


class SessionRegistry:
    """Every open session on one connection.

    Per-connection for the same reason ToolJobSet is: a session is a
    bubble in one client's window, and its life follows that socket.
    """

    def __init__(self) -> None:
        self._by_conversation: Dict[str, StreamSession] = {}

    def get(self, conversation_id: str) -> Optional[StreamSession]:
        """The open, attachable session for this conversation.

        Returns None for a session that is closed, stale, or belongs
        to another conversation. That last one is the ghost-stream
        guard: a session adopted across a conversation boundary would
        stream tokens into a bubble the client is no longer showing.
        """
        session = self._by_conversation.get(str(conversation_id or ""))
        if session is None:
            return None
        if not session.open:
            return None
        if session.idle_for > IDLE_SECONDS:
            logger.debug("session %s went stale after %.0fs",
                         session.session_id, session.idle_for)
            self.close(session.conversation_id, "stale")
            return None
        return session

    def open(self, conversation_id: str, *, model_id: Optional[str],
             stream_id: object) -> StreamSession:
        """Start a session, closing any it replaces.

        Replacing rather than reusing is what keeps a model change
        honest: a session pins one model, and attributing one model's
        tokens to another would be a lie told in the transcript.
        """
        conversation_id = str(conversation_id or "")
        self.close(conversation_id, "replaced")

        session = StreamSession(
            session_id=uuid.uuid4().hex[:12],
            conversation_id=conversation_id,
            model_id=model_id,
            stream_id=stream_id,
            opened_at=time.monotonic(),
        )
        self._by_conversation[conversation_id] = session
        logger.debug("opened stream session %s for %s",
                     session.session_id, conversation_id or "(none)")
        return session

    def attachable(self, conversation_id: str,
                   model_id: Optional[str]) -> Optional[StreamSession]:
        """The session a turn may join, or None.

        A model that differs is not attachable. A switch, an override
        or a capability-gate substitution all end the old session
        rather than continuing it under a new name.
        """
        session = self.get(conversation_id)
        if session is None:
            return None
        if model_id and session.model_id and session.model_id != model_id:
            logger.debug("session %s pinned %s; this turn is %s -- not attaching",
                         session.session_id, session.model_id, model_id)
            return None
        return session

    def close(self, conversation_id: str, outcome: str = "answered") -> bool:
        session = self._by_conversation.pop(str(conversation_id or ""), None)
        if session is None:
            return False
        session.close(outcome)
        logger.debug("closed stream session %s (%s)", session.session_id, outcome)
        return True

    def close_all(self, outcome: str = "disconnected") -> int:
        """Nothing outlives its connection."""
        count = 0
        for conversation_id in list(self._by_conversation):
            if self.close(conversation_id, outcome):
                count += 1
        return count

    def snapshot(self) -> List[dict]:
        return [session.as_dict() for session in self._by_conversation.values()]

    def __len__(self) -> int:
        return len(self._by_conversation)
