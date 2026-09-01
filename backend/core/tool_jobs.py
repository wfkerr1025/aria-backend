"""ARIA Lite - running several tools at once, safely.

Every tool already goes through one door: tool_registry.execute_tool().
That door is synchronous and singular. This widens it rather than
building a second one beside it, because a second execution path means
permission checks, sandbox limits and ToolSchema.timeout_seconds all
exist twice, and the copy nobody updated is the one somebody is using.

So a job here is execute_tool, entered concurrently under a semaphore.
Nothing about validation, permissions or sandboxing changes.

WHY THE POOLS ARE PER-FAMILY AND NOT ONE
----------------------------------------
A single pool would be wrong: the families fail in different ways
under fan-out, and the right width for one is the wrong width for
another.

    search    bounded by network latency         -- wide, 8
    ludo      bounded by MONEY and rate limits   -- narrow, 2, gated
    cli       bounded by CPU and RAM             -- 1; one Blender is
                                                    already a whole core
    registry  bounded by run_in_sandbox threads  -- 4

The Ludo line is not a performance question. "Generate 3 models at
once" is SIX generations -- Ludo has no text-to-3D, so each model is
an image plus a conversion -- and all six are charged before any of
them can be judged bad. Every other family degrades gracefully under
fan-out; this one converts a mistake into an invoice. Hence
MAX_SPEND_JOBS and a confirmation that carries the count.

WHAT CANCELLING A JOB ACTUALLY DOES
-----------------------------------
Three different things, and saying "cancelled" without saying which
would be the half-truth this codebase keeps paying for:

    cli       REALLY stops it. cli_runner already owns a timer and
              kill_tree, so the subprocess dies.
    search,   stops the WAIT. run_in_sandbox hands the work to a
    registry  thread and its own docstring says it deliberately does
              not call future.cancel(), because a thread cannot be
              safely killed. The work finishes; its value is dropped.
    ludo      saves nothing at all. The credit was spent when the
              request was made.

The one clean case is a job cancelled BEFORE it starts -- nothing has
been spent, and for the Ludo family that is the whole difference.
CANCELLED_BEFORE_START exists to say so.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from backend.core import retry_policy
from backend.core.tool_registry import ToolResult, execute_tool, get_tool_schema
from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "CANCELLED_BEFORE_START",
    "FAMILY_LIMITS",
    "MAX_SPEND_JOBS",
    "SPENDING_FAMILIES",
    "ToolJob",
    "ToolJobSet",
    "ToolJobSpec",
    "route_tools_in_parallel",
]

# How many of each family may run at once. See the module docstring.
FAMILY_LIMITS: Dict[str, int] = {
    "search": 8,
    "ludo": 2,
    "cli": 1,
    "registry": 4,
}

DEFAULT_FAMILY = "registry"

# Families where running one more costs real money rather than real
# time. Fanning out past MAX_SPEND_JOBS in one of these needs the
# caller to say so explicitly.
SPENDING_FAMILIES = frozenset({"ludo"})
MAX_SPEND_JOBS = 1

# Job states.
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
CANCELLED_BEFORE_START = "cancelled_before_start"
TIMEOUT = "timeout"

TERMINAL_STATES = frozenset({DONE, FAILED, CANCELLED, CANCELLED_BEFORE_START,
                             TIMEOUT})

# What a cancelled job of each family should be said to have done.
CANCEL_TRUTH = {
    "cli": "The process was killed; nothing more will run.",
    "ludo": ("The generation was already paid for and finishes on its own. "
             "Cancelling does not refund it."),
}
CANCEL_TRUTH_DEFAULT = ("The wait was abandoned. The work finishes on its own "
                        "and its result is discarded.")


@dataclass(frozen=True)
class ToolJobSpec:
    """What to run. Frozen, so a queued job cannot be edited under the
    scheduler that is about to run it."""

    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    family: str = ""                       # "" -> ask the tool's schema
    timeout_seconds: Optional[float] = None
    label: str = ""                        # what a person sees

    def resolved_family(self) -> str:
        """The declared family, the schema's, or the default.

        Asking the schema means the router keeps no second table of
        which tool is which -- one place to be wrong instead of two.
        """
        if self.family:
            return self.family
        schema = get_tool_schema(self.tool)
        return getattr(schema, "family", DEFAULT_FAMILY) if schema else DEFAULT_FAMILY

    def resolved_timeout(self) -> Optional[float]:
        if self.timeout_seconds is not None:
            return float(self.timeout_seconds)
        schema = get_tool_schema(self.tool)
        return float(schema.timeout_seconds) if schema else None


@dataclass
class ToolJob:
    """A spec plus its life. Owned by exactly one connection."""

    job_id: str
    spec: ToolJobSpec
    state: str = QUEUED
    result: Optional[ToolResult] = None     # the EXISTING registry type
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    task: Optional[asyncio.Task] = None
    attempts: int = 0
    retry_verdict: Optional[str] = None    # transient | permanent | unknown

    @property
    def family(self) -> str:
        return self.spec.resolved_family()

    @property
    def done(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.monotonic()) - self.started_at

    def as_dict(self) -> dict:
        return {
            "jobId": self.job_id,
            "tool": self.spec.tool,
            "label": self.spec.label or self.spec.tool,
            "family": self.family,
            "state": self.state,
            "elapsed": round(self.elapsed, 2),
            "ok": None if self.result is None else bool(self.result.ok),
            "error": None if self.result is None else self.result.error,
            "attempts": self.attempts,
        }


def _semaphores() -> Dict[str, asyncio.Semaphore]:
    """One semaphore per family, per event loop.

    Bound to the running loop rather than created at import: an
    asyncio.Semaphore made on one loop and awaited on another raises,
    and the test suite runs a fresh loop per case.
    """
    loop = asyncio.get_running_loop()
    existing = getattr(loop, "_aria_tool_semaphores", None)
    if existing is None:
        existing = {family: asyncio.Semaphore(width)
                    for family, width in FAMILY_LIMITS.items()}
        setattr(loop, "_aria_tool_semaphores", existing)
    return existing


def _semaphore_for(family: str) -> asyncio.Semaphore:
    pool = _semaphores()
    if family not in pool:
        pool[family] = asyncio.Semaphore(FAMILY_LIMITS.get(family, 4))
    return pool[family]


class ToolJobSet:
    """Every job belonging to one connection.

    Per-connection, not global: cancellation, teardown and reporting
    all follow the socket, exactly as _turns_in_flight already does.
    A global set would let one client's disconnect kill another's work.
    """

    def __init__(self) -> None:
        self._jobs: Dict[str, ToolJob] = {}
        # Set by bind_turn. Retries read both: the flag so a stopped
        # turn stops retrying, the deadline so a backoff that cannot
        # fit is abandoned rather than slept through.
        self._stop_flag = None
        self._deadline: Optional[float] = None

    # -- reading -------------------------------------------------

    def get(self, job_id: str) -> Optional[ToolJob]:
        return self._jobs.get(str(job_id))

    def snapshot(self) -> List[dict]:
        return [job.as_dict() for job in self._jobs.values()]

    def running(self) -> List[ToolJob]:
        return [job for job in self._jobs.values() if not job.done]

    def __len__(self) -> int:
        return len(self._jobs)

    # -- running -------------------------------------------------

    async def submit(self, spec: ToolJobSpec, *,
                     on_progress: Optional[Callable[[ToolJob], None]] = None
                     ) -> ToolJob:
        """Start one job and return it immediately.

        Fire-and-forget: the caller may await job.task, or never.
        """
        job = ToolJob(job_id=uuid.uuid4().hex[:12], spec=spec)
        self._jobs[job.job_id] = job
        job.task = asyncio.create_task(self._run(job, on_progress))
        return job

    async def run_all(self, specs: Sequence[ToolJobSpec], *,
                      on_progress: Optional[Callable[[ToolJob], None]] = None,
                      deadline_seconds: Optional[float] = None
                      ) -> List[ToolJob]:
        """Run every spec concurrently and return them all, terminal.

        Never raises. One bad tool must not lose the results of the
        others, so a failure is a ToolJob in state FAILED rather than
        an exception that unwinds the whole fan-out.
        """
        jobs = [await self.submit(spec, on_progress=on_progress)
                for spec in specs]
        if not jobs:
            return []

        pending = [job.task for job in jobs if job.task is not None]
        try:
            if deadline_seconds:
                await asyncio.wait(pending, timeout=float(deadline_seconds))
            else:
                await asyncio.wait(pending)
        except asyncio.CancelledError:
            for job in jobs:
                await self.cancel(job.job_id)
            raise

        # Anything still going when the set's own deadline passed.
        for job in jobs:
            if not job.done:
                await self.cancel(job.job_id, state=TIMEOUT)
        return jobs

    async def _run(self, job: ToolJob,
                   on_progress: Optional[Callable[[ToolJob], None]]) -> None:
        family = job.family
        semaphore = _semaphore_for(family)

        try:
            async with semaphore:
                # Cancelled while queued: nothing has been spent, and
                # for a spending family that is the whole difference.
                if job.state == CANCELLED_BEFORE_START:
                    return

                job.state = RUNNING
                job.started_at = time.monotonic()
                _report(on_progress, job)

                job.result = await self._attempt(job, on_progress)
                job.state = DONE if job.result.ok else FAILED

        except asyncio.CancelledError:
            # Only claim a plain cancellation if nothing has already
            # said WHY. cancel() sets TIMEOUT before cancelling the
            # task, and overwriting it here reported a deadline as a
            # user cancellation -- the same wrong-reason problem the
            # turn terminator's _turn_ending guard exists for.
            if job.state not in TERMINAL_STATES:
                job.state = CANCELLED
            raise
        except Exception as error:          # pragma: no cover - defensive
            logger.exception("tool job %s (%s) fell over", job.job_id,
                             job.spec.tool)
            job.state = FAILED
            job.result = ToolResult(ok=False, error=str(error),
                                    error_code="JOB_FAILED")
        finally:
            job.finished_at = time.monotonic()
            if job.state == RUNNING:        # pragma: no cover - defensive
                job.state = FAILED
            _report(on_progress, job)

    async def _attempt(self, job: "ToolJob",
                       on_progress: Optional[Callable[["ToolJob"], None]]):
        """Run one job, retrying where retrying is honest.

        THE RULE IS NOT "IT FAILED", IT IS "COULD THE SAME REQUEST WORK"
        A 401 retried three times is refused three times. A paid
        generation retried three times is charged three times. So the
        policy comes from the family and the decision from
        retry_policy.classify, and a family that must never be
        resubmitted gets max_attempts=1 rather than a special case
        here.

        Still one door: every attempt is execute_tool, so validation,
        permissions and the sandbox's limits apply to attempt three
        exactly as they applied to attempt one.
        """
        policy = retry_policy.policy_for(job.family)
        last = None

        for attempt in range(1, policy.max_attempts + 1):
            if job.state in TERMINAL_STATES:
                # Cancelled or timed out while waiting to retry.
                break

            job.attempts = attempt
            last = await asyncio.to_thread(
                execute_tool, job.spec.tool, dict(job.spec.args))

            if last.ok:
                return last

            if not retry_policy.should_retry(last.error, policy, attempt):
                job.retry_verdict = retry_policy.classify(last.error)
                return last

            job.retry_verdict = retry_policy.TRANSIENT
            logger.info("tool %s attempt %d/%d failed transiently: %s",
                        job.spec.tool, attempt, policy.max_attempts, last.error)
            _report(on_progress, job)

            # The wait is interruptible and budget-aware. A backoff
            # that outlives the turn is abandoned rather than slept
            # through, because sleeping into a guaranteed timeout
            # spends the budget to achieve nothing.
            went = await asyncio.to_thread(
                retry_policy.sleep_before_retry, policy, attempt + 1,
                remaining_budget=self._remaining_budget(),
                stopped=self._stopped)
            if not went:
                break

        return last

    def _remaining_budget(self) -> Optional[float]:
        """Seconds left in the turn these jobs belong to, if bounded."""
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - time.monotonic())

    def _stopped(self) -> bool:
        """Whether the turn that owns these jobs has ended.

        The SAME threading.Event the stop button and the deadline set.
        A retry reading its own flag would be a second opinion about
        whether the turn is over.
        """
        stop = self._stop_flag
        return bool(stop is not None and stop.is_set())

    def bind_turn(self, *, stop_flag=None, deadline: Optional[float] = None) -> None:
        """Tie these jobs to the turn that owns them.

        Called by the handler when a turn claims the lock. Without it
        a retry has no idea the turn was stopped, and would go on
        backing off into a bubble that is already closed.
        """
        self._stop_flag = stop_flag
        self._deadline = deadline

    # -- stopping ------------------------------------------------

    async def cancel(self, job_id: str, *, state: str = CANCELLED) -> bool:
        job = self._jobs.get(str(job_id))
        if job is None or job.done:
            return False

        # Never started: the clean case, and the only one where
        # nothing was spent.
        if job.state == QUEUED:
            job.state = CANCELLED_BEFORE_START
            job.finished_at = time.monotonic()
            if job.task is not None:
                job.task.cancel()
            return True

        job.state = state
        if job.task is not None:
            job.task.cancel()
        return True

    async def cancel_all(self) -> None:
        """Stop everything this connection owns.

        Called on teardown and by context_reset -- resetting a
        conversation while three of its jobs are still writing results
        into it is the race this whole design exists to prevent.
        """
        pending = [job for job in self._jobs.values() if not job.done]
        for job in pending:
            await self.cancel(job.job_id)

        tasks = [job.task for job in pending if job.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def forget_finished(self) -> int:
        """Drop terminal jobs. A connection that ran a thousand tools
        should not hold a thousand records for the life of the socket."""
        done = [job_id for job_id, job in self._jobs.items() if job.done]
        for job_id in done:
            self._jobs.pop(job_id, None)
        return len(done)


def _report(on_progress: Optional[Callable[[ToolJob], None]],
            job: ToolJob) -> None:
    """Progress reporting is decoration; a job must not be lost to it."""
    if on_progress is None:
        return
    try:
        on_progress(job)
    except Exception:  # pragma: no cover - decoration must not break a job
        logger.debug("a tool-job progress callback raised; continuing")


def spend_check(specs: Sequence[ToolJobSpec], *,
                confirmed: bool = False) -> Optional[str]:
    """Why this fan-out should not run, if it should not.

    Returns None when it is fine. The only thing it guards is money:
    every other family degrades gracefully under fan-out, and this one
    charges for each job before any of them can be judged bad.
    """
    spending = [spec for spec in specs
                if spec.resolved_family() in SPENDING_FAMILIES]
    if len(spending) <= MAX_SPEND_JOBS or confirmed:
        return None

    return (f"That is {len(spending)} generations at once, and each one is "
            f"charged before any of them can be looked at. Say so explicitly "
            f"and I will run them.")


async def route_tools_in_parallel(
        specs: Sequence[ToolJobSpec], *,
        job_set: ToolJobSet,
        on_progress: Optional[Callable[[ToolJob], None]] = None,
        deadline_seconds: Optional[float] = None,
        confirmed_spend: bool = False) -> List[ToolJob]:
    """Run specs concurrently, bounded per family.

    Never raises for anything a tool does. A refused fan-out comes
    back as a single FAILED job carrying the reason, so a caller has
    one shape to read rather than two.
    """
    specs = list(specs or [])
    if not specs:
        return []

    refusal = spend_check(specs, confirmed=confirmed_spend)
    if refusal:
        job = ToolJob(job_id=uuid.uuid4().hex[:12], spec=specs[0],
                      state=FAILED,
                      result=ToolResult(ok=False, error=refusal,
                                        error_code="SPEND_NOT_CONFIRMED"))
        job.finished_at = time.monotonic()
        return [job]

    return await job_set.run_all(specs, on_progress=on_progress,
                                 deadline_seconds=deadline_seconds)


def cancel_truth(family: str) -> str:
    """What cancelling a job of this family actually achieved."""
    return CANCEL_TRUTH.get(str(family), CANCEL_TRUTH_DEFAULT)
