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

Three different questions, three different bars, and they must not share
an answer:

    "may this model be trusted to synthesize evidence?"  -> allowlist,
        conservative by default, because a wrong answer looks right;
    "can this model hold a chat turn at all?"            -> a floor,
        permissive by default, because the alternative is overriding a
        choice the user made on purpose;
    "can this model emit an action block?"               -> allowlist
        again, and this one is measured: phi-3-mini clears the chat floor
        and cannot do it. Asked to create a file it wrote two complete C#
        implementations and not one action, so the user got an essay
        about a file instead of a file.

The third is why this module consults the allowlist after all -- for
TOOL turns only, where it is the established authority and where being
wrong in the permissive direction means the turn silently does nothing.
Each question still decides and repairs with its own rule; what changed
is that there are three questions, not two.

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

# Where a redirected turn should land, by what the turn is for. Keyed by
# backend.chat.model_router's turn kinds and valued with
# backend.config.model_roles' families; both are imported lazily so this
# module stays importable on its own.
_FAMILY_FOR_TURN = {
    "chat": "phi-3-mini-4k-instruct-q4",
    "tools": "mistral-7b",
    "heavy_reasoning": "mistral-nemo-12b",
    # A classification turn that is somehow gated has nowhere sensible to
    # go: the 0.5B is the only model meant for it. Absent from this map
    # on purpose, so it falls through to the ladder.
}

# The exact sentence the user sees when nothing on this machine can run a
# chat turn. Deliberately short and without blame: the cause is which
# models are installed, not anything they typed.
UNABLE_TO_PROCESS = "I'm unable to process that request with the current model."


def switch_warning(model_id: str) -> str:
    return (
        "Active model cannot follow ARIA's chat protocol. "
        f"Switching to: {model_id}."
    )


def _replacement_for(model_id: str, turn_kind: str | None = None) -> str | None:
    """The model this turn should move to, or None if there is none.

    Asked of the same floor that condemned the original, rather than
    delegating to model_capability.recommended_tool_model(): that one
    answers the allowlist question, and a gate that decides with one rule
    and repairs with another can talk itself into recommending a model it
    would then reject.

    `turn_kind` is the routing layer's classification. With it, a chat
    turn moves to the chat model and a tool turn to the tool model --
    rather than every redirected turn landing on the heaviest thing
    installed, which is correct but needlessly slow for "hello". Without
    it the old behaviour is kept exactly: the strongest installed model
    that clears the floor.

    None when nothing installed clears the floor. Naming a model that is
    not there would send the caller to a load that cannot succeed.
    """
    from backend.core.complexity_router import _LADDER, _is_installed

    def usable(candidate: str | None) -> bool:
        return bool(candidate) and candidate != model_id and _is_installed(candidate)             and not too_weak_for_chat(candidate)

    if turn_kind is not None:
        from backend.config.model_roles import installed_model_for

        preferred = installed_model_for(_FAMILY_FOR_TURN.get(turn_kind, ""))
        if usable(preferred):
            return preferred
        # Falls through rather than refusing. The preferred model not
        # being installed is a reason to pick another one, never a reason
        # to tell the user their turn cannot run.

    for candidate in _LADDER:
        if usable(candidate):
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


# Turn kinds that ask the model to emit an action block. Named here
# rather than imported from model_router to keep this module free of a
# dependency on the router that calls it.
_TOOL_TURNS = frozenset({"tools"})


def _lighter_tool_model(too_big: str, model_id: str) -> str | None:
    """A tool-capable model that fits, when the recommended one does not."""
    from backend.core.complexity_router import _LADDER, _fits_in_memory, _is_installed
    from backend.core.model_capability import supports_tool_use

    for candidate in _LADDER:
        if candidate in (too_big, model_id):
            continue
        if (_is_installed(candidate) and supports_tool_use(candidate)
                and _fits_in_memory(candidate)):
            return candidate
    return None


def _tool_replacement_for(model_id: str | None, turn_kind: str | None,
                          mode: str, evidence_turn: bool = False) -> str | None:
    """A tool-capable model for a tool turn, or None to leave it alone.

    THE GAP THIS CLOSES
    -------------------
    There was a floor for CHAT and none for TOOLS. phi-3-mini is 3.8B, so
    it clears CHAT_PARAM_FLOOR and the gate waved it through -- onto a
    turn asking it to create a file. Measured live: it produced two
    complete C# implementations, several hundred tokens each, and not one
    action block. The user asked for a file and got an essay about a
    file, twice.

    The role floor in complexity_router already raises a tool turn to a
    tool model, but only when the router is the one choosing. A model
    that arrives pinned -- an explicit override, "switch to a lighter
    model", a restored session -- never passes through it. That is the
    same shape as every other hole this codebase has closed: a floor
    works wherever the decision is made, a gate only works where it is
    placed, and this gate is on the path every turn takes.

    WHY THE ALLOWLIST DECIDES, NOT A PARAMETER COUNT
    ------------------------------------------------
    _replacement_for warns against deciding with one rule and repairing
    with another. That warning is about ONE axis: the chat floor decides
    chat and repairs chat. Tool use is a different axis with its own
    established authority -- model_capability.TOOL_CAPABLE_MODELS -- and
    it decides and repairs here, so the rule stays single-valued per
    question. It already knew phi-3-mini could not do this and already
    named nemo-12b as the fix; nothing was consulting it.

    Returns None whenever the turn should be left as it is, which is
    every chat turn, every already-capable model, and any case where the
    substitute is missing or would cross the local/cloud boundary. A gate
    that refused here would refuse turns on a machine whose registry
    happens not to list a tool model -- the failure this project already
    met once with model pinning, and removed pinning for.
    """
    if not model_id or str(turn_kind) not in _TOOL_TURNS:
        return None

    # An evidence turn is a tool turn by classification and not by need:
    # "what is the stock price of Microsoft" wants a SEARCH, and the
    # model only has to read what the search found. The evidence ladder
    # has already decided what happens to those -- keep the small local
    # model, simplify the prompt -- and two rules answering one question
    # is how the answer stops being single-valued. Caught by
    # test_evidence_routing, which said so immediately.
    if evidence_turn:
        return None

    try:
        from backend.core.model_capability import (recommended_tool_model,
                                                   supports_tool_use)

        if supports_tool_use(model_id):
            return None

        replacement = recommended_tool_model(model_id)
    except Exception:  # pragma: no cover - a gate fault must not fail a turn
        logger.exception("could not check tool capability for %s", model_id)
        return None

    if not replacement or replacement == model_id:
        return None

    from backend.core.complexity_router import _is_installed

    if not _is_installed(replacement):
        logger.info("tool floor: %s is not installed; leaving %s in place",
                    replacement, model_id)
        return None

    # The same question the role floor asks. Substituting a model the
    # machine cannot hold would trade an answer that is useless for one
    # that does not arrive, and the second is worse.
    from backend.core.complexity_router import _fits_in_memory

    if not _fits_in_memory(replacement):
        lighter = _lighter_tool_model(replacement, model_id)
        if lighter is None:
            logger.info("tool floor: %s does not fit and nothing lighter is "
                        "tool-capable; leaving %s in place", replacement, model_id)
            return None
        replacement = lighter

    if model_violates_mode_separation(replacement, mode):
        logger.warning(
            "tool floor: %s is incompatible with mode %s; leaving %s in place.",
            replacement, mode, model_id,
        )
        return None

    return replacement


def ensure_tool_capable(model_id: str | None, *, mode: str = "local",
                        turn_kind: str | None = None,
                        evidence_turn: bool = False) -> CapabilityGateOutcome:
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

    `turn_kind` says what the turn is for, so a redirected chat turn
    lands on the chat model rather than on the heaviest model installed.
    It is a preference, not an authority: an unroutable turn_kind falls
    back to the ladder rather than refusing.
    """
    if not too_weak_for_chat(model_id):
        replacement = _tool_replacement_for(model_id, turn_kind, mode, evidence_turn)
        if replacement is None:
            return CapabilityGateOutcome(model_id=model_id)

        logger.info(
            "capability gate: %s is not tool-capable and this is a %s turn; "
            "using %s instead.", model_id, turn_kind, replacement,
        )
        return CapabilityGateOutcome(
            model_id=replacement,
            switched_from=model_id,
            warning=switch_warning(replacement),
        )

    replacement = _replacement_for(model_id, turn_kind)

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
