"""ARIA Lite - when to try again, and when trying again is a lie.

A retry is only ever correct when the SAME request, sent again, could
plausibly succeed. That is a narrower set than "it failed", and the
difference between the two is where retry logic usually goes wrong:
retrying a 401 spends time to be refused three times, and retrying a
paid generation spends money to be charged three times.

THE THREE TIERS DECIDE, AGAIN
-----------------------------
Cancellation has three tiers in this codebase, and retry has the same
three -- because both questions are really "what did the work already
cost, and can it be undone?"

    killable      cli_runner kills the subprocess, so a retry starts
                  genuinely fresh. Retrying is cheap and honest.

    abandonable   the thread runs on with its output discarded. A
                  retry therefore runs CONCURRENTLY with the attempt
                  it replaced -- two threads, one result. Fine for a
                  search; the reason the counts here are small.

    already spent the request was charged when it was made. A retry
                  is a SECOND CHARGE for the same asset. This is why
                  RESUBMIT_NEVER exists and why the Ludo family sets
                  it: polling a job again is free, submitting it again
                  is not.

BUDGETS OUTRANK POLICIES
------------------------
Every wait here is checked against the turn's remaining budget before
it is taken. A backoff that would outlive the turn is not slept
through -- it is abandoned, because the alternative is a retry that
is guaranteed to be interrupted by the deadline, which spends the
budget to achieve nothing.

STOPPING OUTRANKS EVERYTHING
----------------------------
Every wait is also interruptible by the same threading.Event the stop
button and the deadline already set. A retry that carried on after
_end_turn would be work nobody is waiting for, streamed into a bubble
that is already closed.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Sequence, Tuple

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "PROVIDER_POLICY",
    "POLICIES",
    "RESUBMIT_NEVER",
    "RetryPolicy",
    "classify",
    "policy_for",
    "sleep_before_retry",
]

# What a failure IS, which is not the same as what it says.
TRANSIENT = "transient"        # try again; the same request may work
PERMANENT = "permanent"        # try again and be refused identically
UNKNOWN = "unknown"            # not recognised -- treated as permanent

# Whether the work may be SUBMITTED again, as opposed to waited on
# again. Polling a Ludo job is free; submitting it is a second charge.
RESUBMIT_OK = "resubmit_ok"
RESUBMIT_NEVER = "resubmit_never"


@dataclass(frozen=True)
class RetryPolicy:
    """How many times, how long between, and what counts.

    Frozen because a policy is a rule, not a scratchpad -- and one
    edited mid-turn would make two attempts of the same turn obey
    different limits.
    """

    max_attempts: int = 1                 # 1 == no retry at all
    backoff_base: float = 0.5             # seconds before the 2nd attempt
    backoff_factor: float = 2.0           # multiplied per further attempt
    backoff_cap: float = 8.0              # no single wait longer than this
    retryable: Tuple[str, ...] = (TRANSIENT,)
    resubmit: str = RESUBMIT_OK
    label: str = ""

    def wait_before(self, attempt: int) -> float:
        """Seconds to wait before `attempt` (1-based; attempt 1 is 0)."""
        if attempt <= 1:
            return 0.0
        raw = self.backoff_base * (self.backoff_factor ** (attempt - 2))
        return float(min(raw, self.backoff_cap))

    def total_worst_case(self) -> float:
        """Every wait this policy could ever impose, added up.

        Read by turn_budget's consistency check: a policy whose waits
        alone outlast the turn budget is a policy that guarantees a
        timeout.
        """
        return sum(self.wait_before(n) for n in range(1, self.max_attempts + 1))

    def allows(self, kind: str) -> bool:
        return kind in self.retryable


# ======================================================
# What each family gets
# ======================================================

# The counts are small on purpose. An abandonable retry runs
# CONCURRENTLY with the attempt it replaced -- the first thread is
# still going -- so three attempts of a slow search is three live
# threads, not one. Two is a genuine second chance; five is a denial
# of service aimed at yourself.
POLICIES: Dict[str, RetryPolicy] = {
    # Network-bound and cheap. The one family where a retry is almost
    # always the right answer.
    "search": RetryPolicy(max_attempts=3, backoff_base=0.5, backoff_factor=2.0,
                          label="search"),

    # MONEY. A generation is charged when the request is made, so a
    # resubmission is a second charge for the same asset -- and Ludo's
    # own request_id de-duplication only helps if the id is REUSED,
    # which a naive retry does not do.
    #
    # max_attempts=1 means the SUBMIT is never repeated. Polling an
    # already-submitted job is a different operation with its own
    # policy below, and that one is free.
    "ludo": RetryPolicy(max_attempts=1, resubmit=RESUBMIT_NEVER, label="ludo"),

    # Waiting on a job that has already been paid for. Free, so it can
    # be patient -- and it is the reason a Ludo timeout hands back a
    # job id rather than losing it.
    "ludo_poll": RetryPolicy(max_attempts=4, backoff_base=1.0,
                             backoff_factor=2.0, backoff_cap=15.0,
                             resubmit=RESUBMIT_NEVER, label="ludo_poll"),

    # Killable, so a retry starts genuinely fresh -- but a Blender
    # render is minutes of CPU and a Unity build longer. One retry for
    # a transient failure (an Editor still starting, a lock not yet
    # released); anything else is surfaced rather than repeated.
    "cli": RetryPolicy(max_attempts=2, backoff_base=2.0, backoff_factor=1.0,
                       backoff_cap=2.0, label="cli"),

    # Sandbox threads. Same abandonable caveat as search.
    "registry": RetryPolicy(max_attempts=2, backoff_base=0.5, label="registry"),
}

DEFAULT_POLICY = RetryPolicy(max_attempts=1, label="default")

# A model call. Separate from the tool families because its failure
# modes are different: a provider that is down does not become up in
# half a second, and the useful move is usually the NEXT MODEL rather
# than the same one again. So: one retry for a genuine blip, then the
# fallback chain takes over.
PROVIDER_POLICY = RetryPolicy(max_attempts=2, backoff_base=0.75,
                              backoff_factor=2.0, label="provider")


def policy_for(family: str) -> RetryPolicy:
    return POLICIES.get(str(family or ""), DEFAULT_POLICY)


# ======================================================
# Reading a failure
# ======================================================

# Phrases that mean "the same request might work next time". Matched
# against the message because that is what the providers actually
# give us -- backend.llm.providers raises plain exceptions and returns
# plain strings, with no error taxonomy to interrogate.
_TRANSIENT_PATTERNS = (
    r"\btimed?\s?out\b", r"\btimeout\b",
    r"\bconnection (?:reset|refused|aborted|error)\b",
    r"\btemporarily unavailable\b", r"\bservice unavailable\b",
    r"\btry again\b", r"\brate.?limit", r"\btoo many requests\b",
    r"\bremote end closed\b", r"\bbroken pipe\b",
    r"\bread timed out\b", r"\bnetwork is unreachable\b",
    r"\b5\d\d\b",                       # 500, 502, 503, 504
    r"\bEOF occurred\b", r"\bssl\b.*\bviolation\b",
    r"\bmodel is (?:still )?loading\b",
    r"\bserver (?:is )?busy\b", r"\bovercapacity\b",
)

# Phrases that mean "it will be refused identically". Checked FIRST:
# "401 unauthorized" beside a "try again" in the same body must not be
# read as transient.
_PERMANENT_PATTERNS = (
    r"\b40[1-9]\b", r"\b41\d\b",        # 4xx, except 408/429 below
    r"\bunauthorized\b", r"\bforbidden\b", r"\binvalid api key\b",
    r"\bauthentication\b", r"\bpermission denied\b",
    r"\bnot found\b", r"\bno such (?:model|file|tool)\b",
    # PLURALS. "insufficient credits" -- the exact string Ludo returns
    # when the money runs out -- fell through to UNKNOWN because
    # `credit\b` does not match "credits". It is the single most
    # important thing on this list not to retry.
    r"\binsufficient (?:credits?|funds|quotas?)\b",
    r"\b(?:out of|no) credits?\b", r"\bquota exceeded\b",
    r"\bpayment required\b", r"\bbilling\b",
    r"\bcontext length\b", r"\btoo many tokens\b",
    r"\binvalid (?:request|argument|parameter)\b",
    r"\bunsupported\b", r"\bmalformed\b",
)

# The two 4xx codes that ARE worth another go, listed so the blanket
# 4xx rule above does not swallow them.
_TRANSIENT_4XX = (r"\b408\b", r"\b429\b")


def classify(error: object) -> str:
    """Whether the same request, sent again, could plausibly work.

    Errors here are strings and exceptions, not codes: none of the 15
    providers in backend.llm.providers raises a typed error, so this
    reads what they actually produce. Unrecognised means PERMANENT --
    the safe direction, because a retry that cannot help still costs
    the turn's budget and, for a spending family, its money.
    """
    if error is None:
        return UNKNOWN

    text = str(error).strip()
    if not text:
        return UNKNOWN

    lowered = text.lower()

    for pattern in _TRANSIENT_4XX:
        if re.search(pattern, lowered):
            return TRANSIENT

    for pattern in _PERMANENT_PATTERNS:
        if re.search(pattern, lowered):
            return PERMANENT

    for pattern in _TRANSIENT_PATTERNS:
        if re.search(pattern, lowered):
            return TRANSIENT

    return UNKNOWN


def should_retry(error: object, policy: RetryPolicy, attempt: int) -> bool:
    """Whether to make attempt+1, on the failure just seen."""
    if attempt >= policy.max_attempts:
        return False
    return policy.allows(classify(error))


def sleep_before_retry(policy: RetryPolicy, attempt: int, *,
                       remaining_budget: Optional[float] = None,
                       stopped: Optional[Callable[[], bool]] = None,
                       poll: float = 0.1) -> bool:
    """Wait before the next attempt. False if the retry is off.

    Three things can call it off, and all three are more important
    than the retry:

      * the turn was ended -- stop or timeout set the same
        threading.Event the worker thread already reads, so a retry
        cannot outlive the bubble it would stream into;
      * the wait would not fit in the remaining budget -- sleeping
        into a guaranteed timeout spends the budget to achieve
        nothing; and
      * the wait finished, but the turn ended during it.

    Slept in small steps rather than one time.sleep, so a stop lands
    within `poll` seconds instead of at the end of an eight-second
    backoff.
    """
    wait = policy.wait_before(attempt)

    if stopped is not None and stopped():
        return False

    if remaining_budget is not None and wait >= max(0.0, remaining_budget):
        logger.debug("%s retry abandoned: %.1fs wait does not fit in %.1fs left",
                     policy.label or "retry", wait, remaining_budget)
        return False

    deadline = time.monotonic() + wait
    while True:
        if stopped is not None and stopped():
            return False
        left = deadline - time.monotonic()
        if left <= 0:
            return True
        time.sleep(min(poll, left))
