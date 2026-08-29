"""ARIA Lite - what each installed model is FOR.

Four models are installed on this machine, and until now the only thing
the system knew about them was a parameter count and an evidence
allowlist. That is enough to answer "is this model too small to talk",
which is what chat_capability_gate does, and not enough to answer "which
of these should take this turn", which is what routing actually needs.

This is that table. It is deliberately a statement about THIS machine:

    qwen2.5-0.5b     router      - classification only. Never user-facing.
    phi-3-mini       supervisor  - the default voice, and the only model
                                   trusted to check another's output.
    mistral-7b       chat_tools  - the default when a turn will use tools.
    mistral-nemo-12b heavy       - deep reasoning, long context, complex code.

WHY THE KEYS ARE NOT REGISTRY IDS
---------------------------------
They are families, matched by prefix, because a registry id carries a
quantisation suffix that has nothing to do with what the model is for:
`mistral-7b` is registered as `mistral-7b-q4km`, and a re-quantised
rebuild is the same model as far as this table is concerned. That is the
same convention EVIDENCE_MODEL_ALLOWLIST already uses.

One of the four does not follow it, and it is the reason ALIASES exists:
the 12B is registered as `nemo-12b-q5`, so "mistral-nemo-12b" is not a
prefix of its id and a plain prefix match finds nothing. Silently. The
whole heavy-reasoning route would have been dead code that every test
using the family name still passed. A test pins that every key here
resolves to a model that is actually installed, so this cannot rot back.

WHAT AN UNKNOWN MODEL GETS
--------------------------
can_chat True, can_tools False, can_supervise False.

Permissive about talking, conservative about acting. A model nobody has
described can be given a conversation -- refusing would mean a newly
installed model cannot be used at all, and the user installed it on
purpose. It is not handed the ability to emit tool packets that get
executed, because nothing here has any evidence it can hold that shape.

This module holds no policy about WHEN to use a role -- that is
backend/chat/model_router.py. It only says what each model is.
"""

from __future__ import annotations

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "DEFAULT_ROLE",
    "MODEL_ROLES",
    "ROLE_CHAT_TOOLS",
    "ROLE_HEAVY_REASONING",
    "ROLE_ROUTER",
    "ROLE_SUPERVISOR",
    "can_chat",
    "can_supervise",
    "can_tools",
    "describe_role",
    "installed_model_for",
    "role_of",
]

ROLE_ROUTER = "router"
ROLE_SUPERVISOR = "supervisor"
ROLE_CHAT_TOOLS = "chat_tools"
ROLE_HEAVY_REASONING = "heavy_reasoning"

MODEL_ROLES = {
    "qwen2.5-0.5b": {
        "role": ROLE_ROUTER,
        # False, and it is the point of the whole table. This model is
        # genuinely useful -- it classifies an intent in a fraction of a
        # second -- and genuinely unable to hold a conversation. Both
        # facts have to be written down or it gets used for the wrong one.
        "can_chat": False,
        "can_tools": False,
        "can_supervise": False,
    },
    "phi-3-mini-4k-instruct-q4": {
        "role": ROLE_SUPERVISOR,
        "can_chat": True,
        # Not trusted to emit tool packets: it is not on the evidence
        # allowlist, and a malformed action packet is executed, not read.
        "can_tools": False,
        "can_supervise": True,
    },
    "mistral-7b": {
        "role": ROLE_CHAT_TOOLS,
        "can_chat": True,
        "can_tools": True,
        "can_supervise": False,
    },
    "mistral-nemo-12b": {
        "role": ROLE_HEAVY_REASONING,
        "can_chat": True,
        "can_tools": True,
        # A heavier model would supervise better, and it is still False:
        # supervision runs in addition to the turn it is checking, so
        # putting the 12B there doubles the slowest thing on the machine.
        "can_supervise": False,
    },
}

# Families whose registry id does not begin with the family name.
# `nemo-12b-q5` is the installed 12B; "mistral-nemo-12b" is what it is
# called everywhere else, including in the specification this table
# implements. Without this the heavy-reasoning route resolves to nothing.
ALIASES = {
    "mistral-nemo-12b": ("nemo-12b", "mistral-nemo-12b", "mistral-nemo"),
}

DEFAULT_ROLE = {
    "role": ROLE_CHAT_TOOLS,
    "can_chat": True,
    "can_tools": False,
    "can_supervise": False,
}


def _prefixes(family: str) -> tuple[str, ...]:
    return ALIASES.get(family, (family,))


def role_of(model_id: str | None) -> dict:
    """What this model is for. Never raises, always returns a role.

    None -- ProviderRouter choosing, which is Cloud or Automatic -- gets
    the full-capability answer rather than the cautious default. A
    frontier model is behind it, and describing that as "cannot use
    tools" would be the strongest configuration on the machine reporting
    itself as the weakest.
    """
    if not model_id:
        return {
            "role": ROLE_HEAVY_REASONING,
            "can_chat": True,
            "can_tools": True,
            "can_supervise": False,
        }

    lowered = str(model_id).lower()
    for family, described in MODEL_ROLES.items():
        if any(lowered.startswith(prefix) for prefix in _prefixes(family)):
            return dict(described)

    return dict(DEFAULT_ROLE)


def can_chat(model_id: str | None) -> bool:
    return bool(role_of(model_id)["can_chat"])


def can_tools(model_id: str | None) -> bool:
    return bool(role_of(model_id)["can_tools"])


def can_supervise(model_id: str | None) -> bool:
    return bool(role_of(model_id)["can_supervise"])


def describe_role(model_id: str | None) -> str:
    return str(role_of(model_id)["role"])


def installed_model_for(family: str) -> str | None:
    """The registry id of an installed model in this family, or None.

    The bridge between the family names above and the ids the rest of the
    system uses. Returns None rather than the family name when nothing
    matches, because handing back a name that is not in the registry
    sends the caller to a load that cannot succeed -- and it would do it
    while looking exactly like success.
    """
    try:
        from backend.core.model_registry import get_model_ids

        prefixes = _prefixes(family)
        for model_id in get_model_ids():
            if any(str(model_id).lower().startswith(prefix) for prefix in prefixes):
                return model_id
    except Exception:  # pragma: no cover - a registry fault is not fatal here
        logger.exception("could not resolve an installed model for family %r", family)
    return None
