"""ARIA Lite - the next model to try when this one will not work.

There is no FallbackManager in this codebase. The brief said to
integrate with one; nothing by that name exists, and nothing else does
the job either -- a provider failure reaches
streaming_engine's `except Exception`, becomes a stream_error packet,
and the turn is over. This module is that missing piece, built to the
shape of what is already here.

WHAT IT MAY NOT DO
------------------
Two hard constraints, both pre-existing and both load-bearing:

  1. It must NOT bypass chat_capability_gate. That gate exists because
     a 0.5B answers an ordinary question by inventing a system prompt
     and replying to one nobody asked. CHAT_PARAM_FLOOR is 3e9, and a
     fallback chain that walked below it would be the gate's failure
     mode reintroduced through a side door -- the very thing the
     original brief said not to build.

  2. It must NOT switch a model without the safety gate seeing the
     substitution. This module RETURNS a model id; it never loads
     one. The caller re-enters the same path a first attempt takes,
     so mode separation and _evaluate_safety run over whatever this
     chose, exactly as they run over an original choice.

WHY THE CHAINS ARE BY MODE AND NOT ONE LIST
-------------------------------------------
"Smaller" is the right direction for a chat turn that hit a loaded
model, and the WRONG direction for a turn that needs tool calls: a
model below the tool-capable line cannot do the work at all, so
falling back to it turns a failure into a wrong answer. The chains
therefore differ by what the turn needs, not by size alone.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "CHAINS",
    "MODE_CHAT",
    "MODE_HEAVY",
    "MODE_SAFE",
    "chain_for",
    "next_model",
    "remaining_after",
]

MODE_CHAT = "chat"      # an ordinary question
MODE_HEAVY = "heavy"    # tools, evidence, file work -- capability matters
MODE_SAFE = "safe"      # resources are tight; smaller is the point

# The chains, in the order they are tried.
#
# Model ids are the ones actually installed on this machine
# (model_registry): nemo-12b-q5, mistral-7b-q4km,
# phi-3-mini-4k-instruct-q4, qwen2.5-0.5b-instruct-q4_k_m.
#
# qwen2.5-0.5b appears in NO chain. It is below CHAT_PARAM_FLOOR, and
# _is_allowed() below re-checks that at call time so a future edit to
# these lists cannot quietly reintroduce it.
CHAINS: Dict[str, Tuple[str, ...]] = {
    MODE_CHAT: (
        "nemo-12b-q5",
        "mistral-7b-q4km",
        "phi-3-mini-4k-instruct-q4",
    ),
    # Capability first and no small tail: a model that cannot hold a
    # tool call does not "degrade" a tool turn, it answers the wrong
    # question confidently.
    MODE_HEAVY: (
        "nemo-12b-q5",
        "mistral-7b-q4km",
    ),
    # Already conserving. One step, and it is the smallest model that
    # is still above the chat floor.
    MODE_SAFE: (
        "mistral-7b-q4km",
        "phi-3-mini-4k-instruct-q4",
    ),
}


def _is_allowed(model_id: str) -> bool:
    """Whether this model may back a chat turn at all.

    Asks chat_capability_gate rather than reimplementing its floor, so
    a change to CHAT_PARAM_FLOOR moves this too. A model the gate
    would immediately substitute is not a fallback -- it is a detour
    that ends where it started.
    """
    try:
        from backend.core import chat_capability_gate

        return not chat_capability_gate.too_weak_for_chat(model_id)
    except Exception:  # pragma: no cover - a gate fault must not open the gate
        logger.exception("could not ask the capability gate about %r", model_id)
        return False


def chain_for(mode: str) -> Tuple[str, ...]:
    """The chain for this mode, with anything the gate forbids removed."""
    raw = CHAINS.get(str(mode or ""), CHAINS[MODE_CHAT])
    return tuple(model for model in raw if _is_allowed(model))


def remaining_after(model_id: Optional[str], mode: str = MODE_CHAT,
                    *, tried: Sequence[str] = ()) -> List[str]:
    """Everything still worth trying after `model_id` failed.

    A model already tried is never offered again -- including the one
    the turn started on, which may not be in the chain at all (an
    explicit override, a cloud provider). That is why `tried` is
    separate from the chain position: the starting model is a fact
    about this turn, not about the chain.
    """
    already = {str(model_id or "")} | {str(name) for name in tried}
    return [model for model in chain_for(mode) if model not in already]


def next_model(model_id: Optional[str], mode: str = MODE_CHAT,
               *, tried: Sequence[str] = ()) -> Optional[str]:
    """The next model to try, or None when the chain is exhausted.

    None is a real answer and the caller must report it as one:
    "fallback_exhausted" is a different sentence from "it failed", and
    a user who has silently had three models tried on their behalf
    should be told that is what happened.
    """
    remaining = remaining_after(model_id, mode, tried=tried)
    if not remaining:
        logger.debug("fallback chain for %s exhausted after %s",
                     mode, list(tried) or model_id)
        return None
    return remaining[0]


def mode_for_turn(*, needs_tools: bool = False, conserving: bool = False) -> str:
    """Which chain this turn should walk.

    Deliberately small: the caller knows these two facts already, and
    a cleverer classifier here would be a second opinion competing
    with the router's.
    """
    if conserving:
        return MODE_SAFE
    return MODE_HEAVY if needs_tools else MODE_CHAT
