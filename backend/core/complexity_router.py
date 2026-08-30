# backend/core/complexity_router.py

"""
Task-complexity-based LOCAL model tier selection.

Maps a classified task complexity (backend.core.task_classifier) onto a
specific installed local model, following a fixed ladder from lightest
to heaviest:

    low complexity    -> qwen2.5-0.5b-instruct-q4_k_m  (trivial)
    medium complexity -> phi-3-mini-4k-instruct-q4 or mistral-7b-q4km
    high complexity   -> nemo-12b-q5                   (difficult)

backend.core.task_classifier.classify_task_complexity() only ever
returns "low"/"medium"/"high" — three buckets, not the five named in the
routing spec this module implements ("trivial / slightly complex /
medium / difficult / complex-advanced"). Rather than fork a second,
divergent complexity classifier, "medium" is split into two model tiers
using prompt length as a tiebreaker within that bucket (see
_pick_medium_tier()) — the closest honest mapping onto the existing
3-bucket signal without inventing a parallel classification system that
could disagree with the one everything else already uses. "complex /
advanced" (the cloud tier) is NOT decided here at all — escalating to
cloud is backend.core.auto_selector.AutoSelector's job (via
success_predictor.should_use_local()); this module only ever answers
"given we're going local, which specific installed model fits."

Hardware limits are considered SECOND, after complexity, per this
system's routing rules ("classify by task complexity first, consider
hardware limits second"):
  - The difficult tier (nemo-12b-q5) additionally requires
    MIN_FREE_RAM_GB_FOR_DIFFICULT_TIER free RAM, not just installed —
    named explicitly in the spec ("sufficient RAM (>=48GB free)").
  - Any tier steps down one rung further if the CPU is already
    saturated (reusing backend.core.safety_manager.BLOCK_CPU_PCT so
    "saturated" means the same thing everywhere in this codebase, not a
    second, differently-tuned threshold).
  - A tier's model is only actually used if it's registered
    (model_registry.get_model() is not None) — if a given install is
    missing one of the ladder's models, this steps down (never up)
    until it finds one that exists, all the way to whatever the
    install's own default/fallback/emergency roles are if none of the
    four named models are present at all. This is what makes "if the
    user has only 1-2 local models installed, ARIA must balance between
    them" and "fall back to qwen-0.5B automatically ... or local model
    failure" hold even on an install that doesn't match this ladder.

This module does NOT decide local-vs-cloud (AutoSelector's job) and
does NOT handle a mid-flight downgrade once a model is already loading
under rising RAM pressure (backend.llm.providers.local_provider.py's
job, unchanged, and reactive rather than proactive). Those two systems
and this one are complementary, not overlapping: this picks a starting
tier before any provider is even resolved; local_provider.py can still
step further down after that if live conditions worsen mid-request.
"""

from __future__ import annotations

import os

import time
from collections import deque
from typing import Any, Deque, Dict, Optional

from .task_classifier import classify_task_complexity
from .model_registry import get_model, get_default_model_id, get_fallback_model_id, get_emergency_model_id
from .resource_monitor import get_resource_snapshot
from .safety_manager import BLOCK_CPU_PCT
from . import routing_history

from backend.logger import log as unified_log
from logger import get_logger

logger = get_logger(__name__)

# ============================================================
# THE LADDER — named exactly as the routing spec's task-complexity
# table names them. Registered-ness is checked at selection time, not
# here; a model missing from a given install is simply never chosen.
# ============================================================
TRIVIAL_MODEL_ID = "qwen2.5-0.5b-instruct-q4_k_m"
SIMPLE_MODEL_ID = "phi-3-mini-4k-instruct-q4"
MEDIUM_MODEL_ID = "mistral-7b-q4km"
DIFFICULT_MODEL_ID = "nemo-12b-q5"

# Heaviest-first — "step down" is just "advance through this list" from
# wherever complexity/hardware landed.
_LADDER = [DIFFICULT_MODEL_ID, MEDIUM_MODEL_ID, SIMPLE_MODEL_ID, TRIVIAL_MODEL_ID]

# "sufficient RAM (>=48GB free)" — named explicitly in the routing spec
# for the difficult/Nemo-12B tier specifically. This is FREE (total -
# used) RAM, not total RAM, since a machine with 64GB total but 30GB
# already in use does not actually have headroom for a 12B model.
MIN_FREE_RAM_GB_FOR_DIFFICULT_TIER = 48.0

# Within task_classifier's single "medium" bucket, use prompt length as
# a tiebreaker between "slightly complex" (simple tier) and "medium"
# tier proper — classify_task_complexity()'s own "medium" range is
# length < 1200 chars, so the midpoint is a reasonable split point
# without inventing new classifier logic.
_MEDIUM_BUCKET_SPLIT_CHARS = 600

# Named exactly as the routing spec's task-complexity table names each
# rung — used only for the routing-log diagnostics surface below
# (dual-context UI's "reasoning tier" column), not for any selection
# logic (that's all still driven by the ladder above).
_REASONING_TIER_NAMES = {
    TRIVIAL_MODEL_ID: "trivial",
    SIMPLE_MODEL_ID: "simple",
    MEDIUM_MODEL_ID: "medium",
    DIFFICULT_MODEL_ID: "difficult",
}

# Per-call routing-decision history for the webui's collapsible Routing
# Log panel — mirrors backend.core.auto_balancer's own `_history` +
# diagnostics_snapshot() pattern exactly (ring buffer, same maxlen),
# rather than threading new fields through the live per-token streaming
# path (backend.core.streaming_engine/execution_pipeline), which would
# add risk to the hot chat path for a diagnostics-only feature.
_ROUTING_HISTORY: Deque[Dict[str, Any]] = deque(maxlen=200)


def _is_installed(model_id: Optional[str]) -> bool:
    return bool(model_id) and get_model(model_id) is not None


# ============================================================
# EVIDENCE-BEARING TURNS
#
# classify_task_complexity() measures one thing: how long the prompt is.
# That is a fine proxy for how much context a model has to hold, and a bad
# one for how capable it has to be -- and the two come apart exactly on
# short factual questions.
#
# "What is the stock price of Microsoft?" is 37 characters, so it
# classifies "low" and lands on the 0.5B emergency model. It is also a
# question that cannot be answered from any model's weights, only from a
# lookup. The weakest model was being handed the turns with the highest
# hallucination risk.
#
# Worse, that happened precisely when the lookup had FAILED. With evidence
# the prompt is the assembled synthesis document (~1600 chars, "high", the
# 12B model); without it the turn falls back to the raw question (37
# chars, "low", the 0.5B model). So losing the evidence also lost the
# model that might have said "I don't know" -- degrading quietly toward
# fabrication.
#
# So: when the prompt carries evidence, length stops deciding.
#
# Detected from the prompt rather than passed down, because the two call
# sites (backend.core.auto_selector, backend.core.provider_router) sit
# behind interfaces this change is not allowed to widen. The markers are
# section headings owned by prompts this codebase builds, so they are
# stable, and an explicit evidence_present= argument overrides the sniff
# for any caller that knows better.
_EVIDENCE_MARKERS = (
    "tool results:",             # synthesis_prompt's tool section
    "evidence summary:",         # synthesis_prompt's header line
    "synthesis instructions:",   # synthesis_prompt's rule block
    "what the lookup returned:",  # evidence_routing's simplified prompt
)


def prompt_carries_evidence(prompt: str) -> bool:
    """Whether this prompt has retrieved or looked-up material in it.

    Also the answer to "is this a simplified evidence prompt", which is
    ~314 characters and would otherwise sit 14 characters from being
    classified "low" -- a margin no routing decision should rest on.
    """
    lowered = (prompt or "").lower()
    return any(marker in lowered for marker in _EVIDENCE_MARKERS)


def evidence_floor_available() -> bool:
    """Whether this install has any model trusted to read evidence.

    The ladder's floor for an evidence-bearing turn is MEDIUM_MODEL_ID,
    and the tiers below it are the ones that cannot be trusted to answer
    from supplied material rather than around it. Normally at least one
    of the two at or above the floor is installed and the floor simply
    holds.

    When neither is, select_local_model_for_prompt falls below the floor
    -- deliberately, because a weaker answer beats no answer -- and says
    so in the log. This is the same question asked before the turn runs,
    so the caller can decide to ask that model for less rather than
    handing it evidence it will answer around.

    A pure registry lookup: no prompt, no hardware probe, no model load,
    and nothing that could put the turn in front of the safety gate.
    """
    floor_index = _LADDER.index(MEDIUM_MODEL_ID)
    return any(_is_installed(model_id) for model_id in _LADDER[:floor_index + 1])


def _pick_medium_tier(prompt: str) -> str:
    return SIMPLE_MODEL_ID if len(prompt.strip()) < _MEDIUM_BUCKET_SPLIT_CHARS else MEDIUM_MODEL_ID


def routing_diagnostics_snapshot() -> Dict[str, Any]:
    """GET /v1/diagnostics/routing + the matching IPC round-trip read this."""
    return {"recent_decisions": list(_ROUTING_HISTORY)[-50:]}


def _apply_chat_floor(chosen: Optional[str], allow_below: bool) -> Optional[str]:
    """Never hand a user-facing turn to a model that cannot hold one.

    The last line of defence, and the one that was missing.

    Everything above this decides by task complexity, and
    classify_task_complexity rates "think carefully about the
    architecture of this module" as low -- so the ladder handed
    essentially every conversational prompt to the 0.5B. On a live
    Automatic-mode session that is exactly what happened: the 0.5B
    answered "Hello Aria" and leaked its own system prompt into the
    reply.

    chat_capability_gate had been added upstream for this, and never saw
    the turn: in Automatic mode the orchestrator resolves model_id to
    None and the model is chosen HERE, afterwards. A gate placed before
    the decision cannot gate a decision made after it.

    So the floor is applied at the point of choice, where every caller
    reaches it -- auto_selector and provider_router both -- rather than
    at one of the paths into it.

    `allow_below` is for the one legitimate use of a sub-floor model:
    classification, whose output nobody reads. It is off by default, so
    a caller has to say so.
    """
    if allow_below or chosen is None:
        return chosen

    try:
        from backend.core.chat_capability_gate import too_weak_for_chat

        if not too_weak_for_chat(chosen):
            return chosen

        # The NEAREST model that clears the floor, not the strongest.
        # _LADDER is heaviest-first, so stepping toward index 0 from where
        # the complexity decision landed is one rung up. Taking the first
        # entry instead sent "Hello Aria" to the 12B -- correct, and four
        # times slower than the answer it needs.
        try:
            start = _LADDER.index(chosen)
        except ValueError:
            start = len(_LADDER)

        for candidate in reversed(_LADDER[:start]):
            if _is_installed(candidate) and not too_weak_for_chat(candidate):
                logger.info(
                    "select_local_model_for_prompt() -> %s is below the chat floor; "
                    "stepping up to %s", chosen, candidate,
                )
                return candidate

        # Nothing installed clears it. The original stands: a weak answer
        # beats no answer, and the caller is told nothing has changed.
        logger.warning(
            "select_local_model_for_prompt() -> %s is below the chat floor and no "
            "installed model clears it", chosen,
        )
    except Exception:  # pragma: no cover - the floor must not break routing
        logger.exception("could not apply the chat floor; leaving %s in place", chosen)

    return chosen


# Weights, plus as much again for context, KV cache and the headroom a
# loaded model actually wants. A rule of thumb, stated as one rather
# than dressed up as a measurement -- but grounded in the file on disk
# instead of a constant nobody re-checked.
_MEMORY_HEADROOM = 2.0


def _fits_in_memory(model_id: str) -> bool:
    """Whether this model can be loaded with the RAM free right now.

    Unknown size means yes. A router that refused every model it could
    not measure would refuse everything on a machine whose registry has
    no paths, which is worse than the thing it guards against.
    """
    try:
        from backend.core import model_registry

        entry = model_registry.get_model(model_id) or {}
        path = entry.get("path")
        if not path:
            return True

        size_gb = os.path.getsize(path) / (1024 ** 3)
        if size_gb <= 0:
            return True

        snapshot = get_resource_snapshot()
        free_gb = max(0.0, snapshot.ram_total_gb - snapshot.ram_used_gb)
        needed = size_gb * _MEMORY_HEADROOM

        if free_gb < needed:
            logger.info(
                "_fits_in_memory() -> %s needs about %.1fGB and %.1fGB is free",
                model_id, needed, free_gb,
            )
            return False
        return True
    except Exception:  # pragma: no cover - a fit check must not break routing
        logger.exception("could not size %s; assuming it fits", model_id)
        return True


def _apply_role_floor(chosen: Optional[str], prompt: str, tool_use: bool,
                      allow_below: bool = False, history=()) -> Optional[str]:
    """Floor the ladder at the tier this turn's WORK needs.

    The same shape as the evidence floor above it, for the same reason
    and at the same layer. In Automatic mode the orchestrator resolves
    model_id to None and this function chooses -- so a role table that
    says "tool turns use mistral-7b" has nothing to act on unless the
    rule also exists here.

    The prompt is classified with backend.chat.model_router's own
    vocabulary rather than a second table. Two lists that answer "is this
    a tool turn" would eventually disagree, and the disagreement would
    surface as a model chosen for work it never did.

    A floor, not a pin: a prompt already above the tool tier stays there.
    """
    # allow_below means "give me the raw ladder answer", and it has to
    # lift BOTH floors or it lifts neither usefully: a classification
    # turn floored up to the tool model is as wrong as one floored up to
    # the chat model.
    if chosen is None or allow_below:
        return chosen

    try:
        from backend.chat.model_router import TURN_CHAT, TURN_HEAVY, classify_text

        kind = TURN_HEAVY if tool_use is None else classify_text(prompt, history)

        if kind == TURN_CHAT and not tool_use:
            return chosen

        # Deep work goes higher than tool work. classify_task_complexity
        # rates "think carefully about the architecture of this module"
        # as low, so without this the same floor that rescues a tool turn
        # would leave a reasoning turn on the 7B.
        from backend.config.model_roles import family_for_turn, installed_model_for

        # The same table the router uses when it can name a model, so
        # Automatic mode and Local mode floor at the same tier.
        target = installed_model_for(family_for_turn(kind))
        if target is None or not _is_installed(target):
            target = MEDIUM_MODEL_ID
        if not _is_installed(target):
            return chosen

        # A floor raises the tier the WORK needs. It does not get to
        # ignore what the machine can hold. Without this, a floor applied
        # after the ladder's resource step-down quietly undid it, and a
        # heavy-looking prompt put a 12B model on a machine with twelve
        # gigabytes free.
        #
        # Found by model_routing_tests.py, which asserts exactly that and
        # which nothing collected until pytest.ini did. It is also the
        # failure mode this project has already met once: pinning a model
        # without consulting resources refused turns on a loaded machine,
        # and was removed for it.
        #
        # WHY NOT MIN_FREE_RAM_GB_FOR_DIFFICULT_TIER
        # ------------------------------------------
        # That constant is the LADDER's rule, and it is a spec number
        # rather than a measurement: 48GB free to run a model that is
        # 8.1GB on disk. Applying it here would have taken the 12B away
        # from tool work on this very machine -- 63.7GB total, 43.7GB
        # free, five times the model's size -- where it has been creating
        # files correctly all session. A floor that refuses a model the
        # machine is demonstrably running is not caution, it is a bug
        # with a safety-sounding name.
        #
        # So the floor asks the question it actually cares about: does
        # this model fit right now. Weights plus as much again for
        # context, KV cache and headroom.
        if not _fits_in_memory(target):
            logger.info(
                "select_local_model_for_prompt() -> %s prompt wants %s, which does "
                "not fit in free memory; flooring at %s instead",
                kind, target, MEDIUM_MODEL_ID,
            )
            target = MEDIUM_MODEL_ID

        if _LADDER.index(chosen) > _LADDER.index(target):
            logger.info(
                "select_local_model_for_prompt() -> %s prompt; flooring %s at %s",
                kind, chosen, target,
            )
            return target
    except Exception:  # pragma: no cover - a floor must not break routing
        logger.exception("could not apply the tool floor; leaving %s in place", chosen)

    return chosen


def select_local_model_for_prompt(
    prompt: str,
    context_length: Optional[int] = None,
    tool_use: bool = False,
    evidence_present: Optional[bool] = None,
    allow_below_chat_floor: bool = False,
    history=(),
) -> str:
    """
    Pick the best installed local model for `prompt`'s task complexity.
    Always returns a usable model_id — never None — falling back all
    the way to whatever this specific install actually treats as its
    default/fallback/emergency model if none of the four ladder models
    are registered at all.

    `evidence_present` overrides the length heuristic entirely: a turn
    carrying looked-up material is never trivial, however short the
    question was. Left as None it is sniffed from the prompt (see
    prompt_carries_evidence), which is what lets the two existing call
    sites benefit without changing their signatures. `tool_use=True`
    counts as evidence too.

    `context_length`/`tool_use` are optional, additive inputs recorded
    into the routing log alongside the decision — every existing call
    site (backend.core.auto_selector, backend.core.provider_router) that
    only passes `prompt` keeps working unchanged. `context_length`
    defaults to len(prompt) when omitted (this module has no visibility
    into a caller's real multi-turn context window); `tool_use` defaults
    to False for the same reason — this function decides purely from
    the prompt text, it doesn't know what the rest of the turn will do.
    """
    prompt = prompt or ""
    if context_length is None:
        context_length = len(prompt)
    complexity = classify_task_complexity(prompt)

    if evidence_present is None:
        evidence_present = prompt_carries_evidence(prompt) or bool(tool_use)

    if evidence_present:
        # Reading evidence and answering from it -- rather than around it
        # -- is the capability at stake, and it is not what prompt length
        # measures. Aim high and let the hardware gates below step down;
        # they cannot go below the medium tier for this turn.
        complexity = "high"
        ideal_model_id = DIFFICULT_MODEL_ID
        logger.info(
            "select_local_model_for_prompt() -> evidence present; ignoring the "
            "length heuristic and flooring at %s", MEDIUM_MODEL_ID,
        )
    elif complexity == "low":
        ideal_model_id = TRIVIAL_MODEL_ID
    elif complexity == "high":
        ideal_model_id = DIFFICULT_MODEL_ID
    else:
        ideal_model_id = _pick_medium_tier(prompt)

    logger.debug(f"select_local_model_for_prompt() -> complexity={complexity}, ideal={ideal_model_id}")

    snapshot = get_resource_snapshot()
    free_ram_gb = max(0.0, snapshot.ram_total_gb - snapshot.ram_used_gb)

    # Hardware limits, considered SECOND (after complexity) per the
    # routing rules — a genuinely difficult task still doesn't get the
    # 12B model if the machine can't actually spare the RAM right now.
    if ideal_model_id == DIFFICULT_MODEL_ID and free_ram_gb < MIN_FREE_RAM_GB_FOR_DIFFICULT_TIER:
        logger.info(
            f"select_local_model_for_prompt() -> free RAM ({free_ram_gb:.1f}GB) below "
            f"{MIN_FREE_RAM_GB_FOR_DIFFICULT_TIER}GB threshold, stepping down from {DIFFICULT_MODEL_ID}"
        )
        ideal_model_id = MEDIUM_MODEL_ID

    # CPU saturation (same threshold safety_manager.py already uses to
    # mean "genuinely saturated") steps down one further rung regardless
    # of which tier complexity picked — "fall back ... under ... CPU
    # saturation" applies at every tier, not just the heaviest.
    cpu_saturated = snapshot.cpu_usage >= BLOCK_CPU_PCT
    start_index = _LADDER.index(ideal_model_id) if ideal_model_id in _LADDER else len(_LADDER) - 1
    if cpu_saturated and start_index < len(_LADDER) - 1:
        logger.info(
            f"select_local_model_for_prompt() -> CPU saturated ({snapshot.cpu_usage:.0f}%), "
            f"stepping down from {_LADDER[start_index]}"
        )
        start_index += 1

    # The floor. An evidence-bearing turn may step down from the 12B to
    # the 7B when the machine is busy, and no further -- the tiers below
    # are the ones that cannot be trusted to read evidence at all, so
    # "the machine is loaded" is not a reason to reach them.
    floor_index = len(_LADDER) - 1
    if evidence_present:
        floor_index = _LADDER.index(MEDIUM_MODEL_ID)
        if start_index > floor_index:
            logger.info(
                "select_local_model_for_prompt() -> hardware would have stepped to %s, "
                "held at %s for an evidence-bearing turn",
                _LADDER[start_index], _LADDER[floor_index],
            )
            start_index = floor_index

    chosen = None
    for candidate in _LADDER[start_index:floor_index + 1]:
        if _is_installed(candidate):
            chosen = candidate
            break

    if chosen is None and evidence_present:
        # Nothing at or above the floor is installed. A weaker model is
        # still better than no answer, but this is worth saying out loud:
        # the turn is about to be handled by a model this router does not
        # trust with evidence.
        for candidate in _LADDER[floor_index + 1:]:
            if _is_installed(candidate):
                chosen = candidate
                logger.warning(
                    "select_local_model_for_prompt() -> no model at or above %s is "
                    "installed; falling below the evidence floor to %r",
                    MEDIUM_MODEL_ID, candidate,
                )
                break

    if chosen is None:
        # None of the four ladder models are registered on this install
        # at all — fall back to whatever this install's own registry
        # roles are, same precedence the rest of the app already uses.
        for fallback_getter in (get_default_model_id, get_fallback_model_id, get_emergency_model_id):
            candidate = fallback_getter()
            if _is_installed(candidate):
                chosen = candidate
                logger.warning(
                    f"select_local_model_for_prompt() -> none of the standard ladder models are "
                    f"installed; falling back to registry role model {candidate!r}"
                )
                break

    chosen = _apply_chat_floor(chosen, allow_below_chat_floor)
    chosen = _apply_role_floor(chosen, prompt, tool_use,
                              allow_below_chat_floor, history)

    if chosen is None:
        # Truly nothing usable is registered — let the caller's own
        # "no local model available" handling take over.
        logger.error("select_local_model_for_prompt() -> no usable local model found at all")
        _ROUTING_HISTORY.append({
            "timestamp": time.time(), "complexity": complexity, "context_length": context_length,
            "tool_use": tool_use, "selected_model": None, "reasoning_tier": _REASONING_TIER_NAMES.get(ideal_model_id),
        })
        return TRIVIAL_MODEL_ID

    if chosen == DIFFICULT_MODEL_ID:
        routing_history.record_local_escalation()

    unified_log("complexity_router", "INFO", "Local model tier selected", {
        "complexity": complexity, "ideal_model_id": ideal_model_id, "chosen_model_id": chosen,
        "free_ram_gb": round(free_ram_gb, 1), "cpu_saturated": cpu_saturated,
        "evidence_present": evidence_present,
    })
    _ROUTING_HISTORY.append({
        "timestamp": time.time(),
        "complexity": complexity,
        "context_length": context_length,
        "tool_use": tool_use,
        "selected_model": chosen,
        "reasoning_tier": _REASONING_TIER_NAMES.get(ideal_model_id, complexity),
    })
    return chosen
