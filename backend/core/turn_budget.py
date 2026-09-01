"""ARIA Lite - how long one turn may take before it is ended.

A turn that never ends is worse than one that fails. It holds
WebSocketHandler._turn_lock, so every later message queues behind it
for the life of the connection, and the client sits on an open bubble
waiting for a stream_end that is never coming.

WHY THE NUMBERS ARE HERE AND NOT SCATTERED
------------------------------------------
Every deadline below already existed somewhere in the tree as a
literal. Collecting them makes one thing checkable that was not:
whether an OUTER budget is longer than the work it contains. A turn
budget shorter than its own inner limit is not a safety net, it is a
guaranteed failure that kills correct work -- and it is invisible
until somebody's Blender render dies at two minutes.

test_budgets_exceed_the_work_they_contain pins exactly that.

A BUDGET IS A BACKSTOP, NOT A TARGET
------------------------------------
These are worst cases, and the worst case is not the common one.
Measured on the developer's machine: a Ludo image is about 16 seconds
end to end and a 3D model about two minutes, against a budget of 720.
The budget exists for the run that hangs, not the run that is slow.
"""

from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "BUDGETS",
    "SCRIPT_MAX_SECONDS",
    "TURN_MAX_SECONDS",
    "budget_for",
    "inner_limit_for",
    "kinds",
]

ENV_TURN_MAX = "ARIA_TURN_MAX_SECONDS"

# The backstop for a turn whose route nobody recognised. Long enough
# for a slow local model on a cold cache, short enough that a wedged
# turn is over within a coffee.
TURN_MAX_SECONDS = 180.0

# A caller with its own patience -- a script, a REST client, a test.
# Not reachable from chat.
SCRIPT_MAX_SECONDS = 900.0

# What each kind of turn gets, in seconds.
#
# The arithmetic for each is in _WORST_CASE below, and the test
# derives from that rather than trusting these numbers. Changing an
# inner limit without changing its budget therefore fails a test
# rather than shipping.
BUDGETS: Dict[str, float] = {
    "chat": 120.0,
    "tool": 90.0,
    "ludo": 720.0,
    "blender": 960.0,
    "unity_cli": 1860.0,
    # 960, not 360. Caught by the consistency check before this
    # shipped: cli_programs.PROGRAMS["blender"] carries a 900-second
    # timeout of its own, because a typed `blender --background ...`
    # IS Blender. A 360-second budget would have killed a correct
    # render at six minutes.
    "cli_program": 960.0,
}


def _ludo_worst_case() -> float:
    """The longest a Ludo turn can legitimately take.

    NOT simply JOB_TOTAL_SECONDS, which was this module's first
    mistake. A generation is:

        call()      -- up to REQUEST_TIMEOUT_SECONDS synchronously, or
                       a 202 and then poll_job up to JOB_TOTAL_SECONDS
        ...twice    -- generate_model is an image AND a conversion,
                       because Ludo has no text-to-3D endpoint
        download()  -- up to DOWNLOAD_TIMEOUT_SECONDS

    So the worst case is 2 x 180 + 300 = 660, not 120.
    """
    from backend.ludo import ludo_client

    one_call = max(ludo_client.REQUEST_TIMEOUT_SECONDS,
                   ludo_client.JOB_TOTAL_SECONDS)
    return (2 * one_call) + ludo_client.DOWNLOAD_TIMEOUT_SECONDS


def _blender_worst_case() -> float:
    from backend.blender import blender_actions

    return float(blender_actions.DEFAULT_TIMEOUT_SECONDS)


def _unity_worst_case() -> float:
    from backend.unity import unity_cli_engine

    return float(unity_cli_engine.LONG_RUNNING_SECONDS)


def _cli_program_worst_case() -> float:
    from backend.plugins import cli_programs

    # The table's own per-program timeout wins where it has one.
    limits = [float(spec.get("timeout") or cli_programs.DEFAULT_TIMEOUT_SECONDS)
              for spec in cli_programs.PROGRAMS.values()]
    return max(limits or [cli_programs.DEFAULT_TIMEOUT_SECONDS])


# kind -> the work its budget has to be longer than. Computed at call
# time from the real constants, so this cannot drift from them.
_WORST_CASE = {
    "ludo": _ludo_worst_case,
    "blender": _blender_worst_case,
    "unity_cli": _unity_worst_case,
    "cli_program": _cli_program_worst_case,
}


def inner_limit_for(kind: str) -> Optional[float]:
    """The longest the work inside this kind of turn can take, or None.

    None means nothing inside it enforces its own limit -- an ordinary
    chat turn, where the budget IS the only limit.
    """
    worst = _WORST_CASE.get(str(kind or ""))
    if worst is None:
        return None
    try:
        return float(worst())
    except Exception:  # pragma: no cover - a missing constant is a test failure
        logger.exception("could not read the inner limit for %r", kind)
        return None


def kinds() -> Tuple[str, ...]:
    return tuple(sorted(BUDGETS))


def budget_for(kind: str = "", requested: Optional[float] = None) -> float:
    """How long this turn may take.

    Per-turn, then per-kind, then the backstop -- the same resolution
    order as every path in this codebase, so there is one convention
    rather than four.

    A requested budget is honoured up to SCRIPT_MAX_SECONDS. Above
    that it is capped rather than refused: a caller asking for an hour
    wants "as long as possible", and the cap is what that means here.
    """
    if requested is not None:
        try:
            asked = float(requested)
        except (TypeError, ValueError):
            asked = 0.0
        if asked > 0:
            return min(asked, SCRIPT_MAX_SECONDS)

    from_env = str(os.environ.get(ENV_TURN_MAX) or "").strip()
    if from_env:
        try:
            return max(1.0, float(from_env))
        except ValueError:
            logger.warning("%s is not a number: %r", ENV_TURN_MAX, from_env)

    return float(BUDGETS.get(str(kind or ""), TURN_MAX_SECONDS))


# ======================================================
# Which kind of turn this is
# ======================================================

def classify(text: str) -> str:
    """The kind of turn a message will become, before it runs.

    THE CHICKEN AND EGG, AND HOW IT IS RESOLVED
    -------------------------------------------
    The deadline has to be armed when the turn claims the lock, which
    is before the orchestrator has decided anything. So the budget
    cannot wait for the route -- it has to predict it.

    It predicts it by asking the SAME public predicates the
    short-circuits use to claim the turn: names_blender, names_ludo,
    parse_invocation. Routing and budgeting therefore cannot disagree,
    for the same reason names_blender is public rather than a private
    detail of its mapper -- a budget that decided differently from the
    router would give a Blender render the chat budget and kill it at
    two minutes.

    Every predicate here is free: no model, no I/O, no subprocess.
    """
    said = str(text or "").strip()
    if not said:
        return "chat"

    try:
        from backend.unity import unity_cli_engine
        if unity_cli_engine.parse_invocation(said):
            return "unity_cli"
    except Exception:  # pragma: no cover - a predicate fault is not a route
        logger.debug("unity predicate failed while classifying", exc_info=True)

    try:
        from backend.plugins import cli_programs
        if cli_programs.parse_invocation(said):
            return "cli_program"
    except Exception:  # pragma: no cover
        logger.debug("cli predicate failed while classifying", exc_info=True)

    # NAMING a tool is not the same as USING one, and the difference is
    # sixteen minutes.
    #
    # Measured: "Create a 3D Model in Blender of an Anime Swordsman"
    # names Blender, so this returned "blender" and granted the turn a
    # 960-second budget. The Blender layer then declined it in
    # microseconds -- there is no swordsman recipe -- and the turn was
    # answered by a model instead, holding a budget sized for a render
    # that was never going to happen.
    #
    # So the question is not "does this name Blender" but "would
    # Blender actually be asked to do something". map_text answers
    # exactly that, and costs nothing.
    try:
        from backend.blender import blender_nl_mapping
        if (blender_nl_mapping.names_blender(said)
                and not blender_nl_mapping.names_another_tool(said)
                and blender_nl_mapping.map_text(said) is not None):
            return "blender"
    except Exception:  # pragma: no cover
        logger.debug("blender predicate failed while classifying", exc_info=True)

    try:
        from backend.ludo import ludo_nl_mapping
        if (ludo_nl_mapping.names_ludo(said)
                and not ludo_nl_mapping.names_another_tool(said)
                and (ludo_nl_mapping.map_text(said) is not None
                     or ludo_nl_mapping.collect_request(said))):
            return "ludo"
    except Exception:  # pragma: no cover
        logger.debug("ludo predicate failed while classifying", exc_info=True)

    return "chat"
