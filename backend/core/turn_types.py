"""ARIA Lite - the values passed across the transport/reasoning boundary.

A turn arrives as a TurnRequest and leaves as a TurnResult. Both are frozen,
and neither carries a socket, a connection, or a handler -- which is what
lets the orchestrator between them be exercised without either transport.

The two fields that make that possible are `session_updates` and
`telemetry`. The orchestrator never applies a session change or writes a
log line; it *describes* both, and the transport performs them. So
"orchestrate a weather turn and check the clarification window opened"
becomes an assertion on a returned dict rather than a mock of a websocket.

That is the same shape Engine B already uses for tools -- a ToolInvocation
describes a call and an executor makes it -- so nothing new is introduced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "KIND_CLARIFY",
    "KIND_ERROR",
    "KIND_INFERENCE",
    "KIND_MODEL_SWITCH",
    "KIND_SAFETY_WARNING",
    "KIND_TEXT",
    "RESULT_KINDS",
    "SessionState",
    "TurnRequest",
    "TurnResult",
]

# A reply that is already complete and needs no model.
KIND_TEXT = "text"
# A reply that needs a model; inference_request is populated.
KIND_INFERENCE = "inference"
# The safety gate refused; warning carries the packet verbatim.
KIND_SAFETY_WARNING = "safety_warning"
# A question back to the user (currently only the weather location prompt).
KIND_CLARIFY = "clarify"
# A structured refusal the transport should surface as an error.
KIND_ERROR = "error"
# The user asked to change model, mode or provider in conversation.
# metadata["model_switch"] holds the resolved target; performing the
# switch is the transport's job, because validating it is mode
# separation and mode separation is Engine A's.
KIND_MODEL_SWITCH = "model_switch"

RESULT_KINDS = frozenset({
    KIND_TEXT, KIND_INFERENCE, KIND_SAFETY_WARNING, KIND_CLARIFY, KIND_ERROR,
    KIND_MODEL_SWITCH,
})


@dataclass(frozen=True)
class SessionState:
    """What the transport is currently holding, as a read-only snapshot.

    Passed in rather than reached for, so the orchestrator cannot mutate it
    and cannot depend on a handler instance existing. Everything here is
    per-connection state that outlives a single turn.
    """

    mode: str = "local"
    explicit_model_override: str | None = None
    awaiting_weather_location: bool = False
    last_turn_was_weather: bool = False
    connection_healthy: bool = True
    # The model a "Proceed Anyway" was granted for. One-shot: it is
    # consumed on use, so a second request for the same model goes through
    # the gate normally again.
    override_model_id: str | None = None


@dataclass(frozen=True)
class TurnRequest:
    """One chat turn, as both transports describe it.

    `messages` is the FULL history, deliberately untrimmed. Engine B's turn
    selection exists to reach past a trim -- its whole point is keeping the
    turn that stated the goal after twenty intervening ones -- so handing it
    the trimmed list would silently reduce it to the truncation it replaces.
    The trimmed view is produced inside the orchestrator and goes only to
    the provider.
    """

    messages: list[dict] = field(default_factory=list)
    latest_user_text: str = ""
    conversation_id: str = ""
    multi_turn: bool = True
    requested_model_id: str | None = None
    skip_safety_check: bool = False
    allow_override: bool = False
    max_tokens: int = 2048
    temperature: float = 0.7
    session: SessionState = field(default_factory=SessionState)
    # Ask the orchestrator to record how the model was chosen. Off by
    # default: the trace is for someone debugging a routing decision, and
    # every turn carrying one would be noise on every other turn.
    debug_trace: bool = False


@dataclass(frozen=True)
class TurnResult:
    """What the transport should do about this turn.

    A description of effects, never the effects themselves. `kind` selects
    the branch; the transport applies `session_updates`, emits `telemetry`,
    and then acts on exactly one of text / inference_request / warning.
    """

    kind: str
    text: str | None = None
    inference_request: Any = None
    model_id: str | None = None
    conversation_id: str = ""
    warning: dict | None = None
    policy_info: dict = field(default_factory=dict)
    session_updates: dict = field(default_factory=dict)
    telemetry: list[dict] = field(default_factory=list)

    # Facts about how this turn was produced, for a caller that needs to
    # check rather than assume. Observability only -- nothing routes on
    # it, and an empty dict is always valid.
    #
    #   tool_runs: tuple[str, ...]  -- tools that actually executed
    #   model_switch: dict          -- the resolved switch, for KIND_MODEL_SWITCH
    metadata: dict = field(default_factory=dict)

    # Which synthesis this turn ended up asking for: "full",
    # "simplified_local", or "raw_evidence". See
    # backend/core/evidence_routing.py -- a small local model handed
    # multi-source evidence answers around it rather than from it, so a
    # turn can be downgraded to a simpler prompt, or to no model at all.
    synthesis_mode: str = "full"

    # A lookup was expected for this turn and produced nothing. The turn
    # says so rather than answering anyway -- a question about a current
    # price, answered from a model's weights because the search failed, is
    # the exact failure the evidence pipeline exists to prevent.
    evidence_missing: bool = False

    # How the model was chosen, when TurnRequest.debug_trace asked for it.
    # Empty otherwise. Debugging aid only -- nothing routes on it.
    model_resolution_trace: dict = field(default_factory=dict)

    # The safety decision and the model config it was made against, carried
    # so the transport can run its own warning emission (_emit_warnings)
    # without re-evaluating. Present on every turn that resolved a model,
    # not only refusals, because warnings are emitted on both paths today
    # and dropping that would be a behaviour change.
    safety_decision: Any = None
    model_cfg: dict | None = None

    @property
    def needs_model(self) -> bool:
        """Whether this turn will load a model.

        The safety gate exists to protect a model load, so a turn that
        answers from a tool or the registry does not need to pass it --
        the same reasoning that already exempts self-knowledge.
        """
        return self.kind == KIND_INFERENCE
