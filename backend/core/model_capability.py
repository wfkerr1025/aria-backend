"""ARIA Lite - whether the model in front of us can be asked to act.

An action is a fenced JSON block naming a tool, and a model that cannot
hold that shape produces prose where an action was wanted. The turn then
looks like ARIA talking about a change instead of proposing one -- which
is what "ARIA talks but never acts" is, seen from the inside.

So this answers one question: can the active model be asked for a
structured action. And it answers it from what is INSTALLED. The request
that prompted this named Qwen2.5 7B/14B, LLaMA-3 Instruct, Phi-3 Medium
and Mistral-7B v0.3; none of the four is on this machine, so a list of
them would report every local model as incapable and send every turn to
a default.

The basis is the list that already exists. evidence_routing decides which
models may be asked to read supplied evidence and answer from it rather
than around it, and that is the same discipline an action needs: follow
the structure you were given instead of writing about it. Rather than
keep a second roster of "the good models" -- which would drift from the
first, and drift silently -- this reads that one. A test pins that they
stay in step.

What this module does NOT do is switch models.

    Pinning a model was implemented, measured and removed once already,
    and turn_orchestrator still carries the note: a concrete model_id is
    a model the SAFETY GATE evaluates, and with model_id None the gate is
    skipped entirely. Pinning put an evidence turn in front of the gate
    for the first time and, on a loaded machine, came back
    safety_warning/caution -- refusing a turn that would otherwise have
    run. Trading "might pick a weak model" for "might refuse outright" is
    not a trade worth making, and it has now been proposed three times.

    The existing machinery already gets this right without pinning:
    complexity_router floors an evidence-bearing prompt at the 7B and
    steps down no further, and AutoSelector prefers a trusted local model
    for evidence turns. Both models above that floor are tool-capable, so
    an action turn already lands on one.

    What was missing is the part this adds: saying so when it does not.
"""

from __future__ import annotations

from backend.core.evidence_routing import EVIDENCE_MODEL_ALLOWLIST

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "TOOL_CAPABLE_MODELS",
    "active_model_id",
    "capability_warning",
    "recommended_tool_model",
    "supports_tool_use",
]

# Matched by prefix, like the list it comes from: registry ids carry a
# quantisation suffix ("mistral-7b" is registered as "mistral-7b-q4km"),
# and a re-quantised rebuild of the same model is the same model here.
TOOL_CAPABLE_MODELS = EVIDENCE_MODEL_ALLOWLIST


def supports_tool_use(model_id: str | None) -> bool:
    """Whether this model can be asked for a structured action.

    None means Cloud or Automatic routing, where ProviderRouter picks and
    a frontier model answers. Those are capable, and answering False
    would warn a user about the strongest configuration they have.
    """
    if not model_id:
        return True

    lowered = str(model_id).lower()
    return any(lowered.startswith(allowed) for allowed in TOOL_CAPABLE_MODELS)


def recommended_tool_model(model_id: str | None) -> str | None:
    """The model an action turn should be on, or None if this one will do.

    A recommendation, never an action. The caller decides what to do with
    it, and on the evidence path the answer is usually "nothing": the
    complexity ladder has already floored the turn at a capable model by
    the time anything here is asked.
    """
    if supports_tool_use(model_id):
        return None

    from backend.core.complexity_router import (
        MEDIUM_MODEL_ID, _is_installed, _LADDER,
    )

    floor = _LADDER.index(MEDIUM_MODEL_ID)
    for candidate in _LADDER[:floor + 1]:
        if _is_installed(candidate):
            return candidate

    # Nothing capable is installed. None rather than a name, because
    # naming a model that is not there would send the caller to a load
    # that cannot succeed.
    logger.warning("no tool-capable model is installed; an action turn cannot be routed to one")
    return None


def capability_warning(model_id: str | None) -> str | None:
    """What to tell the user, or None when there is nothing to say.

    Said out loud rather than handled quietly. "ARIA must never silently
    fall back to chat-only mode" is the requirement, and this is the part
    of it that can be met honestly: a model that cannot be asked for an
    action will answer in prose, and the user should know that is why.
    """
    if supports_tool_use(model_id):
        return None

    recommended = recommended_tool_model(model_id)
    if recommended:
        return (
            f"{model_id} cannot reliably produce structured actions, so this turn "
            f"will answer in prose rather than propose an edit. {recommended} can. "
            f"Ask again with it selected, or let an evidence turn route there on "
            f"its own."
        )
    return (
        f"{model_id} cannot reliably produce structured actions, and no model that "
        f"can is installed. Edits will be described rather than proposed."
    )


def active_model_id() -> str | None:
    """The model a turn would use right now, as the UI would report it.

    None in Cloud and Automatic mode is meaningful and is passed through:
    it is this codebase's way of saying "ProviderRouter chooses", and
    replacing it with a guess would put a name in the UI that the next
    turn need not honour.
    """
    try:
        from backend.core.mode_manager import ModeManager

        manager = ModeManager()
        override = manager.get_explicit_model_override()
        if override:
            return override
        if manager.get_mode() == "local":
            from backend.core.model_registry import get_default_model_id

            return get_default_model_id()
    except Exception:  # pragma: no cover - a UI field is not worth a failure
        logger.exception("could not determine the active model")
    return None
