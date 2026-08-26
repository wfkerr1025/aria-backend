"""ARIA Lite - what the assistant is doing right now, as a packet.

A chat turn can spend several seconds between the user pressing enter and
the first token arriving: intent detection, retrieval, a plan, a web
lookup, then prompt assembly. Until now the UI showed one undifferentiated
"typing..." for all of it, which reads as a hang rather than as work.

The vocabulary below is the full set of states a turn passes through. Not
all of them can be *emitted* from the transport, and the gap is worth
stating plainly rather than papering over:

    listening / thinking / writing / idle      emitted live, accurate
    planning / searching / executing /
    synthesizing                               happen inside
                                               orchestrate_turn

Everything in the second group runs on a worker thread inside one
synchronous call. The transport hands that call a TurnRequest and gets a
TurnResult back; it learns what happened only once it is over, and
announcing "Searching..." after the search has finished would be a
progress indicator that lies about the present tense.

They are defined here anyway, because the vocabulary is the protocol and
the UI should be able to render a state the moment the backend can send
it. Emitting them live needs one optional progress callback on
orchestrate_turn -- deliberately not added here, since the orchestrator is
out of scope for this change.

What is sent instead is honest about both fact and tense: `thinking`
covers the whole orchestration phase, and the `writing` packet carries
`tool_runs`, so the UI can say what the turn actually did ("Writing --
searched the web") without claiming to be doing it now.
"""

from __future__ import annotations

__all__ = [
    "EXECUTING",
    "IDLE",
    "LISTENING",
    "PLANNING",
    "SEARCHING",
    "STATUS_PACKET_TYPE",
    "SYNTHESIZING",
    "THINKING",
    "TURN_STATUSES",
    "WRITING",
    "status_packet",
]

STATUS_PACKET_TYPE = "status"

# Waiting on the user. Owned by the UI, which is the only side that knows
# whether the composer has focus.
LISTENING = "listening"
# The orchestrator is running: intent, safety, routing, retrieval, and --
# for a reasoning turn -- planning, tools and synthesis.
THINKING = "thinking"
PLANNING = "planning"
SEARCHING = "searching"
EXECUTING = "executing"
SYNTHESIZING = "synthesizing"
# The model is producing the answer and tokens are on the wire.
WRITING = "writing"
# The turn is over. Always sent, including after an error or a refusal,
# so an indicator can never be left spinning.
IDLE = "idle"

TURN_STATUSES = frozenset({
    LISTENING, THINKING, PLANNING, SEARCHING, EXECUTING,
    SYNTHESIZING, WRITING, IDLE,
})


def status_packet(value: str, **fields) -> dict:
    """One status packet. Cheap enough to send on every transition."""
    if value not in TURN_STATUSES:
        raise ValueError(f"unknown turn status: {value!r}")
    return {"type": STATUS_PACKET_TYPE, "value": value, **fields}


def tool_runs_from(result) -> list:
    """Which tools this turn ran, from the result's own metadata.

    Read rather than inferred: TurnResult.metadata["tool_runs"] is the
    orchestrator's own record of what executed, so a UI built on this
    cannot drift from what really happened.
    """
    metadata = getattr(result, "metadata", None) or {}
    return list(metadata.get("tool_runs") or ())
