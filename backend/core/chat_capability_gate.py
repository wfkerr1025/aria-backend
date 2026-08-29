"""ARIA Lite - refusing to hand a chat turn to a model that cannot hold one.

qwen2.5-0.5b answers a chat turn by inventing a system prompt, repeating
itself, or replying to a question nobody asked. That is not a prompt that
needs tuning; it is a model too small to follow the protocol, and every
turn routed to it is wasted. So before a turn reaches a model, this asks
one question -- can this model be asked to follow ARIA's chat protocol --
and moves the turn to one that can.

WHY THIS IS NOT THE MODEL PINNING THAT WAS REFUSED
--------------------------------------------------
Pinning a model was implemented, measured and removed once already, and
model_capability's docstring still carries the reason: a concrete
model_id is a model the SAFETY GATE evaluates, and with model_id None the
gate is skipped entirely. Pinning put turns in front of the gate for the
first time and, on a loaded machine, came back safety_warning/caution --
refusing turns that would otherwise have run.

That measurement stands, and it is why this gate is written the way it
is. Two things keep it on the right side of it:

  1. It only ever moves a turn OFF a model that cannot do the job. The
     refused patches pinned a model on turns the complexity ladder had
     already floored at a capable one, so they bought nothing and cost a
     new refusal path. Here the alternative to switching is not "a
     slightly weaker answer", it is nonsense. A safety warning the user
     can read and click through is strictly better than confident
     garbage.

  2. It runs BEFORE the safety gate and never around it. The substituted
     model_id goes through _evaluate_safety exactly as the original would
     have, and a refusal surfaces as the ordinary safety_warning with its
     ordinary "Proceed Anyway". There is no switch_model() ahead of the
     gate here -- that was named as forbidden and it is not done.

Mode separation is likewise checked, not assumed: the substitute is
validated with model_violates_mode_separation before it is accepted, so
this cannot smuggle a local id into a cloud turn.

WHY A PARAMETER FLOOR AND NOT THE TOOL-CAPABLE LIST
--------------------------------------------------
The obvious implementation is model_capability.supports_tool_use(), and
it is wrong here. That is an ALLOWLIST -- the models trusted to read
supplied evidence and answer from it -- and anything not on it reads as
incapable. Used as a chat gate it would redirect every model ARIA has
not been told about, including one the user deliberately installed and
explicitly asked for. Measured against the real registry, it moved
turns off `phi-3-mini`, which holds a conversation perfectly well, and
off every id in the test corpus.

Two different questions, two different bars, and they must not share an
answer:

    "may this model be trusted to synthesize evidence?"  -> allowlist,
        conservative by default, because a wrong answer looks right;
    "can this model hold a chat turn at all?"            -> a floor,
        permissive by default, because the alternative is overriding a
        choice the user made on purpose.

So this reads `params` off the registry entry and refuses only what is
demonstrably below the floor. A model with no registry entry, or an
entry with no parameter count, passes through untouched -- not knowing
something about a model is not evidence against it.

WHAT model_id=None MEANS HERE
-----------------------------
None is Cloud or Automatic routing, where ProviderRouter picks and a
frontier model answers. Those turns pass through untouched -- this gate
exists for an explicit pin onto a small local model, which is the only
way a turn reaches one of these in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.core.model_registry import model_violates_mode_separation

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "CHAT_PARAM_FLOOR",
    "CapabilityGateOutcome",
    "UNABLE_TO_PROCESS",
    "ensure_tool_capable",
    "switch_warning",
    "too_weak_for_chat",
]

# Below this many parameters, a model does not hold ARIA's chat protocol.
#
# Set between the two models that decided it. qwen2.5-0.5b (500M) is the
# reported failure: asked an ordinary question it invents a system prompt
# and answers one nobody asked. phi-3-mini (3.8B) is on the same machine,
# is NOT on the evidence allowlist, and converses fine -- it is not
# trusted to synthesize multi-source evidence, which is a different and
# stricter question than whether it can talk.
#
# A round 3B sits between them with room either side. It is a threshold,
# so it will eventually be wrong about some model; being wrong in the
# permissive direction leaves the user with the model they chose, which
# is the right way round to be wrong.
CHAT_PARAM_FLOOR = 3_000_000_000

# The exact sentence the user sees when nothing on this machine can run a
# chat turn. Deliberately short and without blame: the cause is which
# models are installed, not anything they typed.
UNABLE_TO_PROCESS = "I'm unable to process that request with the current model."


def switch_warning(model_id: str) -> str:
    return (
        "Active model cannot follow ARIA's chat protocol. "
        f"Switching to: {model_id}."
    )


def _replacement_for(model_id: str) -> str | None:
    """The strongest installed model that clears the floor, or None.

    Asked of the same floor that condemned the original, rather than
    delegating to model_capability.recommended_tool_model(): that one
    answers the allowlist question, and a gate that decides with one rule
    and repairs with another can talk itself into recommending a model it
    would then reject.

    None when nothing installed clears the floor. Naming a model that is
    not there would send the caller to a load that cannot succeed.
    """
    from backend.core.complexity_router import _LADDER, _is_installed

    for candidate in _LADDER:
        if candidate != model_id and _is_installed(candidate) and not too_weak_for_chat(candidate):
            return candidate
    return None


def too_weak_for_chat(model_id: str | None) -> bool:
    """Whether this model is demonstrably below the floor.

    Everything unknown answers False. None (ProviderRouter chooses), a
    model with no registry entry, an entry with no parameter count, or a
    count that will not parse -- none of those is evidence that the model
    is too small, and this gate overrides a user's explicit choice, so it
    acts only on evidence.
    """
    if not model_id:
        return False

    try:
        from backend.core.model_registry import get_model

        entry = get_model(model_id) or {}
        params = entry.get("params")
        if params is None:
            return False
        return int(params) < CHAT_PARAM_FLOOR
    except Exception:  # pragma: no cover - a lookup fault must not refuse a turn
        logger.exception("could not read params for %s; letting the turn through", model_id)
        return False


@dataclass(frozen=True)
class CapabilityGateOutcome:
    """What the gate decided, as a value the caller acts on.

    Described rather than performed, the same way TurnResult describes a
    turn's effects: this module changes no state, loads nothing, and
    sends nothing. That is what lets it be tested without a mode manager,
    a registry or a socket.
    """

    model_id: str | None
    # The model the turn arrived on, when it was moved off one. None when
    # nothing was switched -- so `switched_from is not None` is the whole
    # test for "did this gate act".
    switched_from: str | None = None
    # Populated only alongside switched_from. The caller shows it; this
    # module does not know what a UI is.
    warning: str | None = None
    # True when the turn cannot run at all. model_id is then meaningless
    # and the caller must answer with UNABLE_TO_PROCESS instead of
    # generating.
    refused: bool = False

    @property
    def switched(self) -> bool:
        return self.switched_from is not None


def ensure_tool_capable(model_id: str | None, *, mode: str = "local") -> CapabilityGateOutcome:
    """Move a turn onto a model that can follow the chat protocol.

    Returns the model the turn should use. Three outcomes:

      - capable already (including None, meaning ProviderRouter chooses):
        returned unchanged, no warning, nothing to say;
      - not capable, something better installed: the better model, with
        the sentence to show;
      - not capable and nothing better installed: refused, and the caller
        answers with UNABLE_TO_PROCESS rather than generating.

    `mode` is used only to reject a substitute that would cross the
    local/cloud boundary. It is not used to choose one.
    """
    if not too_weak_for_chat(model_id):
        return CapabilityGateOutcome(model_id=model_id)

    replacement = _replacement_for(model_id)

    if replacement is None:
        # Nothing installed can do this. Said out loud rather than
        # answered badly: a turn that runs on a model which cannot follow
        # the protocol produces text that looks like an answer and is
        # not, which is worse than no answer at all.
        logger.warning(
            "chat capability gate: %s cannot follow the chat protocol and no "
            "installed model can; refusing the turn.", model_id,
        )
        return CapabilityGateOutcome(model_id=model_id, refused=True)

    if model_violates_mode_separation(replacement, mode):
        # Should not happen -- the ladder holds local models and this gate
        # only fires on a concrete local id -- but absolute mode
        # separation is checked, never assumed. Refusing is the safe
        # direction: the alternative is putting a local model in front of
        # a cloud turn.
        logger.warning(
            "chat capability gate: replacement %s is incompatible with mode %s; "
            "refusing rather than crossing the boundary.", replacement, mode,
        )
        return CapabilityGateOutcome(model_id=model_id, refused=True)

    logger.info(
        "chat capability gate: %s cannot follow the chat protocol; using %s instead.",
        model_id, replacement,
    )
    return CapabilityGateOutcome(
        model_id=replacement,
        switched_from=model_id,
        warning=switch_warning(replacement),
    )
