"""ARIA Lite - what Ludo.ai costs, before it costs it.

WHY THIS EXISTS
---------------
Every other tool ARIA drives spends time. Ludo spends money, and it
spends it at REQUEST time: the credit is gone the moment the POST
lands, so a cancel, a timeout, a crash, a retry and a user changing
their mind all cost the same as a success. There is no refund path and
no balance endpoint to check first.

That was survivable while a person typed one request at a time and
looked at the result. The pipeline work ahead -- "create a forest scene
with three characters" fanning out into concept art, models, cleanup
and import -- turns one careless sentence into a dozen paid calls with
nobody watching. This module is what makes that safe to build.

FOUR THINGS, IN THE ORDER THEY SAVE MONEY
-----------------------------------------
1. REUSE. The same request, asked twice, is the archetypal waste. Every
   billable call is keyed by a hash of what it asks for, and a repeat
   returns the earlier asset instead of buying it again. Never silently
   -- the answer says it was reused, because a user who wanted a second
   variation must be able to tell that they did not get one.

2. IDEMPOTENCY. Ludo already declines to charge twice for the same
   request_id. ludo_client used to send a fresh uuid4 on every call,
   which meant that mechanism could never fire and every retry was a
   new purchase. The key from (1) IS the request_id, so a retry of the
   same logical request is free on Ludo's side even when the reuse
   cache has been cleared or disabled.

3. CEILINGS. A per-run cap and a per-day cap. A runaway loop, a
   pipeline that fans out wider than intended, or a model that decides
   to be helpful twelve times stops at a number instead of at the end
   of the account.

4. A LEDGER. Append-only, on disk, so "what did that cost" survives a
   restart and a plan can be priced before it runs. Reuse and estimates
   both read it; without it neither is possible.

WHAT A CREDIT IS NOT MEASURED IN
--------------------------------
The spec gives every POST an `x-credit-action` but no price, and there
is no endpoint that reports a balance. So the unit here is a BILLABLE
CALL, not a currency: one POST to a generating endpoint is one call. A
3D model is two, because it is an image and then a conversion. If real
per-endpoint prices ever become known, WEIGHTS is where they go and
everything else keeps working.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "BudgetExceeded",
    "DAILY_CALLS",
    "LEDGER_PATH",
    "REUSE_MAX_AGE_SECONDS",
    "RUN_CALLS",
    "UNLIMITED",
    "WEIGHTS",
    "check",
    "estimate",
    "history",
    "is_billable",
    "ledger_path",
    "lookup",
    "note",
    "record",
    "request_key",
    "reuse_enabled",
    "spent",
    "start_tally",
    "tally",
]


class BudgetExceeded(RuntimeError):
    """A ceiling was reached. Nothing was sent and nothing was spent."""


# ======================================================
# What the answer in front of the user is allowed to claim
# ======================================================

# One action can make several calls -- a 3D model is an image and then
# a conversion -- and any of them can be a reuse hit. Kept per thread
# because the tool router runs Ludo jobs two at a time, and a tally
# shared between them would report one action's spending on another's
# answer.
_tally = threading.local()


def start_tally() -> None:
    """Begin counting for one action. Called by the actions layer."""
    _tally.spent = 0
    _tally.reused = 0


def note(outcome: str, calls: int) -> None:
    if outcome == "reused":
        _tally.reused = getattr(_tally, "reused", 0) + 1
    elif outcome in ("spent", "maybe"):
        _tally.spent = getattr(_tally, "spent", 0) + int(calls or 0)


def tally() -> Dict[str, int]:
    """What the action just now cost, in billable calls.

    `reused` is the number of calls it did NOT have to make. A user
    told "0 calls, reused an earlier result" can ask for a fresh one;
    a user told nothing assumes they got a new asset and finds out
    later that they did not.
    """
    return {"calls": getattr(_tally, "spent", 0),
            "reused": getattr(_tally, "reused", 0)}


# ======================================================
# What costs, and how much
# ======================================================

# Endpoints that take money. Everything absent from here is free:
# polling a job, validating a key. Listed explicitly rather than
# derived, so a new endpoint added to ludo_client is not billable by
# accident -- and an endpoint that IS billable and missing here shows
# up as a call this module failed to count, which the tests check.
BILLABLE = frozenset({
    "image", "image_edit", "image_style", "remove_background",
    "model_3d", "model_3d_rig", "model_3d_animate", "model_3d_animate_preset",
    "sprite_animate", "sprite_keyframes", "sprite_rotate", "sprite_pose",
    "video", "music", "sound_effect", "speech", "speech_preset", "voice",
})

FREE = frozenset({"job", "validate"})

# One POST is one call. If per-endpoint prices are ever published, they
# go here and estimate()/check() start speaking in them instead.
WEIGHTS: Dict[str, int] = {}
DEFAULT_WEIGHT = 1

# Ceilings. Deliberately low enough to be felt: a cap nobody ever
# reaches is a cap that was never tested, and the failure this guards
# against is a loop, not a busy afternoon.
RUN_CALLS = 12
DAILY_CALLS = 60

# An asset URL from Ludo does not live forever, so a reuse hit older
# than this is ignored rather than handed over as a dead link.
REUSE_MAX_AGE_SECONDS = 12 * 3600

# The ledger is machine state, not a log: reuse and estimates read it.
_REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = _REPO_ROOT / "aria_config" / "ludo_spend.jsonl"
LEDGER_MAX_LINES = 5000

ENV_LEDGER = "ARIA_LUDO_SPEND_LEDGER"
ENV_RUN_CALLS = "ARIA_LUDO_RUN_CALLS"
ENV_DAILY_CALLS = "ARIA_LUDO_DAILY_CALLS"
ENV_REUSE = "ARIA_LUDO_REUSE"

_write_lock = threading.Lock()


def ledger_path() -> Path:
    configured = os.environ.get(ENV_LEDGER)
    return Path(configured) if configured else LEDGER_PATH


# What a ceiling of "no ceiling" is spelled as. NOT zero: somebody
# typing ARIA_LUDO_DAILY_CALLS=0 means "do not spend anything", and on
# a guard whose entire job is money, reading that as "spend without
# limit" is the worst possible way to be wrong. Zero is an off switch.
UNLIMITED = -1


def _limit(name: str, fallback: int) -> int:
    """A ceiling from the environment, or the one compiled in.

    A value that is not a number is the compiled-in one rather than an
    error: a typo in a shell variable must not be the reason a turn
    fails, and it must certainly not be the reason a ceiling silently
    disappears.

    0 blocks every paid call. Negative means no ceiling.
    """
    raw = os.environ.get(name)
    if raw is None:
        return fallback
    try:
        value = int(str(raw).strip())
    except ValueError:
        logger.warning("%s is not a number (%r); using %d", name, raw, fallback)
        return fallback
    return UNLIMITED if value < 0 else value


def run_cap() -> int:
    return _limit(ENV_RUN_CALLS, RUN_CALLS)


def daily_cap() -> int:
    return _limit(ENV_DAILY_CALLS, DAILY_CALLS)


def reuse_enabled() -> bool:
    """Reuse is on unless it is switched off.

    On by default because that is the setting that saves money, and the
    surprise it can cause -- the same prompt returning the same asset
    -- is made visible in the answer rather than hidden.
    """
    return str(os.environ.get(ENV_REUSE, "1")).strip().lower() not in (
        "0", "false", "no", "off")


def is_billable(endpoint: str) -> bool:
    return str(endpoint) in BILLABLE


def weight(endpoint: str) -> int:
    if not is_billable(endpoint):
        return 0
    return int(WEIGHTS.get(str(endpoint), DEFAULT_WEIGHT))


def estimate(endpoints: Iterable[str]) -> Dict[str, Any]:
    """What a plan will cost, before any of it runs.

    The point of a number here is consent: a user who is told "this is
    seven calls" can say no, which is the only form of refund Ludo
    offers.
    """
    counted = [str(name) for name in (endpoints or [])]
    billable = [name for name in counted if is_billable(name)]
    return {
        "calls": sum(weight(name) for name in billable),
        "billable": billable,
        "free": [name for name in counted if not is_billable(name)],
    }


# ======================================================
# The key: what makes two requests the same request
# ======================================================

# Fields that say nothing about WHAT is being asked for. Left in the
# key they would make every request unique, which is the same as having
# no key at all.
_NOT_PART_OF_THE_ASK = frozenset({"request_id", "webhook_url", "callback_url"})


def request_key(endpoint: str, payload: Optional[Dict[str, Any]] = None) -> str:
    """A stable name for one logical request.

    Two calls with this key are the same purchase: same endpoint, same
    parameters, in any order. It is both the reuse-cache key and the
    request_id sent to Ludo, so a repeat is caught here if the ledger
    has it and by Ludo if it does not.
    """
    body = {name: value for name, value in (payload or {}).items()
            if name not in _NOT_PART_OF_THE_ASK and value is not None}
    try:
        stable = json.dumps(body, sort_keys=True, separators=(",", ":"),
                            default=str)
    except (TypeError, ValueError):  # pragma: no cover - default=str covers it
        stable = repr(sorted(body.items()))
    digest = hashlib.sha256(f"{endpoint}\x00{stable}".encode("utf-8"))
    return digest.hexdigest()


# ======================================================
# The ledger
# ======================================================

def record(endpoint: str, *, key: str = "", outcome: str = "spent",
           run_id: str = "", calls: int = 0, url: str = "",
           result: Any = None, note: str = "") -> None:
    """Append one line. Never raises.

    A ledger that could fail a turn would be worse than no ledger: the
    money is already gone by the time this is called, and losing the
    RECORD of it on top helps nobody.
    """
    entry = {
        "ts": round(time.time(), 3),
        "endpoint": str(endpoint),
        "outcome": str(outcome),
        "calls": int(calls),
        "key": str(key),
        "run": str(run_id or ""),
    }
    if url:
        entry["url"] = str(url)
    if note:
        entry["note"] = str(note)
    if result is not None and outcome == "spent":
        entry["result"] = result

    target = ledger_path()
    try:
        with _write_lock:
            target.parent.mkdir(parents=True, exist_ok=True)

            # A write killed part-way leaves a line with no newline on
            # the end; appending onto it would glue two records
            # together and lose both.
            needs_break = False
            try:
                if target.exists() and target.stat().st_size:
                    with open(target, "rb") as check_tail:
                        check_tail.seek(-1, os.SEEK_END)
                        needs_break = check_tail.read(1) != b"\n"
            except OSError:
                needs_break = False

            with open(target, "a", encoding="utf-8") as handle:
                if needs_break:
                    handle.write("\n")
                handle.write(json.dumps(entry, separators=(",", ":"),
                                        default=str) + "\n")
        _trim(target)
    except OSError:
        logger.exception("could not write the Ludo spend ledger at %s", target)


def _trim(target: Path) -> None:
    """Keep the ledger from becoming the problem it reports on."""
    try:
        with open(target, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        if len(lines) <= LEDGER_MAX_LINES:
            return
        with _write_lock:
            with open(target, "w", encoding="utf-8") as handle:
                handle.writelines(lines[-LEDGER_MAX_LINES:])
    except OSError:  # pragma: no cover - trimming is best effort
        pass


def history(limit: int = 0, *, run_id: str = "") -> List[dict]:
    """The ledger, newest last. A torn or unreadable line is skipped.

    One interrupted write must not make the whole history unreadable --
    the reuse cache and every estimate read through here.
    """
    target = ledger_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []

    entries = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            if run_id and str(entry.get("run") or "") != str(run_id):
                continue
            entries.append(entry)

    return entries[-limit:] if limit else entries


def spent(*, since: Optional[float] = None, run_id: str = "") -> int:
    """Billable calls actually made. Reused and refused ones cost
    nothing and are not counted."""
    total = 0
    for entry in history(run_id=run_id):
        if entry.get("outcome") not in ("spent", "maybe"):
            continue
        if since is not None and float(entry.get("ts") or 0) < since:
            continue
        total += int(entry.get("calls") or 0)
    return total


def spent_today() -> int:
    return spent(since=time.time() - 86400)


# ======================================================
# Reuse
# ======================================================

def lookup(key: str) -> Optional[dict]:
    """The result of an identical earlier request, if there is one.

    Only a `spent` entry that kept its result, and only while its asset
    URL can still be expected to resolve. A stale hit is worse than a
    miss: it hands back a dead link and reports that no credit was
    needed for it.
    """
    if not key or not reuse_enabled():
        return None

    cutoff = time.time() - REUSE_MAX_AGE_SECONDS
    for entry in reversed(history()):
        if entry.get("key") != key or entry.get("outcome") != "spent":
            continue
        if float(entry.get("ts") or 0) < cutoff:
            return None
        result = entry.get("result")
        if result is None:
            return None
        return {"result": result, "url": entry.get("url") or "",
                "ts": entry.get("ts"), "endpoint": entry.get("endpoint")}
    return None


# ======================================================
# The gate
# ======================================================

def _calls(count: int) -> str:
    """"1 paid Ludo call", not "1 paid Ludo calls". This string is read
    at the moment something the user wanted did not happen, which is
    the worst moment to look careless."""
    return f"{count} paid Ludo call" + ("" if count == 1 else "s")


def check(endpoint: str, *, run_id: str = "", calls: int = 0) -> None:
    """Refuse before the POST, or return quietly.

    Raises BudgetExceeded, which the client turns into LudoUnavailable
    -- the one error in this stack that means "did not try", so the
    answer a user sees says nothing was spent and means it.
    """
    if not is_billable(endpoint):
        return

    wanted = int(calls or weight(endpoint))

    if run_id:
        cap = run_cap()
        already = spent(run_id=run_id)
        if cap != UNLIMITED and already + wanted > cap:
            raise BudgetExceeded(
                f"That would be {_calls(already + wanted)} in one run and "
                f"the limit is {cap}. Nothing was sent. Raise it with "
                f"{ENV_RUN_CALLS} if you meant to.")

    cap = daily_cap()
    today = spent_today()
    if cap != UNLIMITED and today + wanted > cap:
        raise BudgetExceeded(
            f"That would be {_calls(today + wanted)} today and the limit "
            f"is {cap}. Nothing was sent. Raise it with "
            f"{ENV_DAILY_CALLS} if you meant to.")
