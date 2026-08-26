# backend/core/auto_balancer.py

"""
Tiered, reversible CPU auto-balancer for local CPU-only inference.

Scope, enforced by is_eligible(): local provider only, and
n_gpu_layers == 0 for the resolved run. That's it — there is no size
bucket. This module used to gate on an additional "params in the 12B
bucket (>8B and <=16B)" check; that gate is gone. The universal rule is
"local + CPU-only" for exactly the reason the original 12B-only
scoping existed in the first place: every tier this module applies
(chunk size, threads, context, quant) is a property of a CPU-bound
llama.cpp run, not of any particular parameter count — a 0.5B model
saturating a low-core-count machine benefits from the exact same
throttle ladder a 12B model does, and there was never a real technical
reason (only an unexamined assumption) to withhold it.

Eligibility is evaluated on the RESOLVED runtime configuration, not on
a model's name or declared size class. A future 70B/120B/140B model
that ends up running with real GPU layers offloaded (n_gpu_layers > 0)
is NOT eligible — thread/context/chunk tuning targets CPU-bound
inference specifically (see the HONEST SCOPE section below), and a
GPU-resident model doesn't bottleneck the same way. The SAME model
falling back to a CPU-only run (no GPU present, or GPU capacity
exhausted) IS eligible, exactly like any other local model. This is
what reconciles "auto-balance applies to all local models, including
future GPU-class ones" with "the rule is local + CPU-only" — the two
are not in tension once eligibility is understood as per-run state,
never a per-model allowlist.

Every other model, every cloud provider, and every GPU-offloaded local
run is untouched — is_eligible() returning False is a no-op everywhere
this module is consulted.

HONEST SCOPE — what "throttle"/"restore" actually change, and when:

  - Chunk size (tier 1): the ONE genuinely live, mid-generation change.
    backend.core.streaming_engine_v2's consumer loop calls tick() once
    per iteration and reads get_chunk_size() to size its next flush —
    an already-streaming response visibly gets chunkier/smoother within
    the same turn.

  - Threads / context / quant (tiers 2-4): llama-cpp-python 0.3.34 (the
    version this project depends on) fixes n_threads and n_ctx at
    Llama() construction time — there is no runtime setter, and no
    per-call override on create_completion()/__call__() either
    (confirmed by inspecting the installed binding before writing this).
    Changing either therefore REQUIRES reconstructing the Llama object,
    which only backend.core.model_loader.ModelLoader.load_model() does —
    and it only ever runs between generations, never mid-stream. So
    these three tiers take effect starting with this model's NEXT
    load_model() call, not the one currently in flight. This is not a
    shortcut; forcibly reloading mid-generation is exactly the
    interruption this task's safety rules forbid. See
    model_loader.py's own integration comment for where this is applied.

  - Quantization specifically requires a DIFFERENT GGUF file on disk
    (llama.cpp cannot re-quantize a loaded model in place). If no
    lower-quant sibling file exists for this model — the common case,
    since most installs have exactly one quant per model — the quant
    tier is entered in bookkeeping/diagnostics ("requested but
    unavailable") without ever claiming a swap that didn't happen. See
    _find_lower_quant_sibling().
"""

from __future__ import annotations

import glob
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional
from collections import deque

from . import perf_profiler

from backend.logger import log as unified_log
from logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Thresholds (spec-literal)
# ---------------------------------------------------------------------------
WARN_CPU = 70.0
HIGH_CPU = 80.0
CRITICAL_CPU = 85.0
RESTORE_CPU = 60.0

THREADS_ENTER_AFTER_SECONDS = 0.5
CONTEXT_ENTER_AFTER_SECONDS = 1.5
QUANT_ENTER_AFTER_SECONDS = 3.0

QUANT_RESTORE_AFTER_SECONDS = 3.0
CONTEXT_RESTORE_AFTER_SECONDS = 2.0
THREADS_RESTORE_AFTER_SECONDS = 1.0
CHUNK_RESTORE_AFTER_SECONDS = 0.5

TIER_NONE = 0
TIER_CHUNK = 1
TIER_THREADS = 2
TIER_CONTEXT = 3
TIER_QUANT = 4
TIER_NAMES = {0: "none", 1: "chunk", 2: "threads", 3: "context", 4: "quant"}

# One reduction step per tier — see module docstring for why this is
# modeled as a single discrete step rather than the multi-value examples
# in the spec (64->32->16 etc.) being additional independent sub-steps:
# a single, deterministic step per tier keeps the state machine
# idempotent and testable, and still traverses the same example values
# when a session escalates through consecutive tiers (CHUNK_SIZES[1]==32
# is tier 1's target; a session that also reaches tier "again" after a
# restore-then-rethrottle cycle simply re-enters at the same step).
CHUNK_SIZE_FULL = 64
CHUNK_SIZE_REDUCED = 32
THREAD_FRACTION = 0.75
CONTEXT_FRACTION = 0.5
MIN_THREADS = 2

_TICK_MIN_INTERVAL_SECONDS = 0.1  # tick() self-throttles faster callers to roughly the requested 100-250ms cadence

_QUANT_ORDER = ["Q2_K", "Q3_K_M", "Q4_K_M", "Q5_K_M", "Q6_K"]  # ascending difficulty, from model_metadata.QUANT_DIFFICULTY_MULTIPLIER


@dataclass
class BalanceAction:
    session_id: str
    model_id: str
    direction: str  # "throttle" | "restore"
    tier: int
    field: str  # "chunk_size" | "threads" | "context" | "quant"
    old_value: Any
    new_value: Any
    cpu_at_change: float
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id, "model_id": self.model_id,
            "direction": self.direction, "tier": self.tier, "tier_name": TIER_NAMES[self.tier],
            "field": self.field, "old_value": self.old_value, "new_value": self.new_value,
            "cpu_at_change": round(self.cpu_at_change, 1), "timestamp": self.timestamp,
        }


class AutoBalancer:
    """One instance per active inference session (see execution_pipeline.py)."""

    def __init__(self, session_id: str, model_id: str, model_cfg: dict, baseline_threads: int, baseline_context: int, prompt_token_estimate: int = 0):
        self.session_id = session_id
        self.model_id = model_id
        self.model_cfg = model_cfg
        self.baseline_threads = baseline_threads
        self.baseline_context = baseline_context
        self.baseline_quant = (model_cfg.get("quant") or "").upper()
        self.prompt_token_estimate = prompt_token_estimate

        self.current_tier = TIER_NONE
        self.chunk_size = CHUNK_SIZE_FULL
        self.current_threads = baseline_threads
        self.current_context = baseline_context
        self.current_quant = self.baseline_quant
        self.quant_override_path: Optional[str] = None

        self._above_high_since: Optional[float] = None
        self._above_critical_since: Optional[float] = None
        self._below_restore_since: Optional[float] = None
        self._last_tick_ts: float = 0.0
        self._last_cpu: float = 0.0

        self._lock = threading.Lock()
        self.created_at = time.time()
        self.last_change_at: Optional[float] = None
        self.history: List[BalanceAction] = []

    # -----------------------------------------------------------
    # Public tick — self-throttling, safe to call as often as the
    # caller likes (streaming's consumer loop calls it every ~50ms;
    # tick() itself only actually evaluates state at _TICK_MIN_INTERVAL_SECONDS).
    # -----------------------------------------------------------
    def tick(self, cpu_pct: float) -> Optional[BalanceAction]:
        now = time.time()
        with self._lock:
            if (now - self._last_tick_ts) < _TICK_MIN_INTERVAL_SECONDS:
                return None
            self._last_tick_ts = now
            self._last_cpu = cpu_pct
            return self._evaluate(cpu_pct, now)

    def _evaluate(self, cpu_pct: float, now: float) -> Optional[BalanceAction]:
        # Duration tracking for entering a higher tier.
        self._above_high_since = self._above_high_since or now if cpu_pct >= HIGH_CPU else None
        self._above_critical_since = self._above_critical_since or now if cpu_pct >= CRITICAL_CPU else None
        self._below_restore_since = self._below_restore_since or now if cpu_pct < RESTORE_CPU else None

        action = self._maybe_throttle(cpu_pct, now)
        if action is not None:
            return action
        return self._maybe_restore(cpu_pct, now)

    # -----------------------------------------------------------
    # Throttle path — advances at most one tier per tick(), in order.
    # -----------------------------------------------------------
    def _maybe_throttle(self, cpu_pct: float, now: float) -> Optional[BalanceAction]:
        if cpu_pct >= WARN_CPU and self.current_tier < TIER_CHUNK:
            return self._apply_chunk(cpu_pct, now)

        if (
            cpu_pct >= HIGH_CPU and self._above_high_since is not None
            and (now - self._above_high_since) > THREADS_ENTER_AFTER_SECONDS
            and self.current_tier < TIER_THREADS
        ):
            return self._apply_threads(cpu_pct, now)

        if (
            cpu_pct >= HIGH_CPU and self._above_high_since is not None
            and (now - self._above_high_since) > CONTEXT_ENTER_AFTER_SECONDS
            and self.current_tier < TIER_CONTEXT
        ):
            if not self._context_fits(self.baseline_context * CONTEXT_FRACTION):
                logger.debug(f"auto_balancer[{self.session_id}]: context reduction skipped — prompt does not fit in reduced context")
                return None
            return self._apply_context(cpu_pct, now)

        if (
            cpu_pct >= CRITICAL_CPU and self._above_critical_since is not None
            and (now - self._above_critical_since) > QUANT_ENTER_AFTER_SECONDS
            and self.current_tier < TIER_QUANT
        ):
            return self._apply_quant(cpu_pct, now)

        return None

    def _context_fits(self, reduced_context: float) -> bool:
        if self.prompt_token_estimate <= 0:
            return True
        # Leave headroom for the response itself, not just the prompt.
        return self.prompt_token_estimate <= (reduced_context * 0.8)

    def _apply_chunk(self, cpu_pct: float, now: float) -> BalanceAction:
        old = self.chunk_size
        self.chunk_size = CHUNK_SIZE_REDUCED
        self.current_tier = TIER_CHUNK
        return self._record("throttle", TIER_CHUNK, "chunk_size", old, self.chunk_size, cpu_pct, now)

    def _apply_threads(self, cpu_pct: float, now: float) -> BalanceAction:
        old = self.current_threads
        self.current_threads = max(MIN_THREADS, round(self.baseline_threads * THREAD_FRACTION))
        self.current_tier = TIER_THREADS
        return self._record("throttle", TIER_THREADS, "threads", old, self.current_threads, cpu_pct, now)

    def _apply_context(self, cpu_pct: float, now: float) -> BalanceAction:
        old = self.current_context
        self.current_context = max(512, round(self.baseline_context * CONTEXT_FRACTION))
        self.current_tier = TIER_CONTEXT
        return self._record("throttle", TIER_CONTEXT, "context", old, self.current_context, cpu_pct, now)

    def _apply_quant(self, cpu_pct: float, now: float) -> BalanceAction:
        old = self.current_quant
        sibling = _find_lower_quant_sibling(self.model_cfg)
        self.current_tier = TIER_QUANT
        if sibling is None:
            logger.info(f"auto_balancer[{self.session_id}]: quant downgrade requested but no lower-quant file found on disk for {self.model_id} — recorded, not applied")
            return self._record("throttle", TIER_QUANT, "quant", old, old, cpu_pct, now, applied=False)
        self.current_quant = sibling["quant"]
        self.quant_override_path = sibling["path"]
        return self._record("throttle", TIER_QUANT, "quant", old, self.current_quant, cpu_pct, now)

    # -----------------------------------------------------------
    # Restore path — steps down exactly one tier per tick(), reverse order.
    # -----------------------------------------------------------
    def _maybe_restore(self, cpu_pct: float, now: float) -> Optional[BalanceAction]:
        """
        Restores exactly the CURRENT (highest active) tier, gated on
        THAT tier's own restore duration — not "whichever of the four
        duration thresholds happens to be satisfied first". The four
        durations are deliberately different lengths (3s/2s/1s/0.5s),
        and CHUNK_RESTORE_AFTER_SECONDS is the shortest — a naive
        "check all four, return the first match" (an earlier, buggy
        version of this method) would satisfy chunk's short threshold
        before quant's long one even when the balancer is still at tier
        4, restoring chunk directly and skipping straight from tier 4 to
        tier 0 in one step. Keying the check to self.current_tier alone
        is what makes "never restore more than one tier at a time" and
        "reverse of throttle order" both actually hold.
        """
        if self.current_tier == TIER_NONE or self._below_restore_since is None:
            return None
        elapsed_low = now - self._below_restore_since

        # Reads the module-level duration constants directly (not a
        # dict built once at import time) so a test — or any future
        # runtime tuning — that reassigns e.g. auto_balancer.QUANT_RESTORE_AFTER_SECONDS
        # takes effect immediately, not just for balancers constructed afterward.
        if self.current_tier == TIER_QUANT:
            required_duration, restore_fn = QUANT_RESTORE_AFTER_SECONDS, AutoBalancer._restore_quant
        elif self.current_tier == TIER_CONTEXT:
            required_duration, restore_fn = CONTEXT_RESTORE_AFTER_SECONDS, AutoBalancer._restore_context
        elif self.current_tier == TIER_THREADS:
            required_duration, restore_fn = THREADS_RESTORE_AFTER_SECONDS, AutoBalancer._restore_threads
        else:
            required_duration, restore_fn = CHUNK_RESTORE_AFTER_SECONDS, AutoBalancer._restore_chunk

        if elapsed_low > required_duration:
            return restore_fn(self, cpu_pct, now)
        return None

    def _restore_quant(self, cpu_pct: float, now: float) -> BalanceAction:
        old = self.current_quant
        self.current_quant = self.baseline_quant
        self.quant_override_path = None
        self.current_tier = TIER_CONTEXT
        return self._record("restore", TIER_QUANT, "quant", old, self.current_quant, cpu_pct, now)

    def _restore_context(self, cpu_pct: float, now: float) -> BalanceAction:
        old = self.current_context
        self.current_context = self.baseline_context
        self.current_tier = TIER_THREADS
        return self._record("restore", TIER_CONTEXT, "context", old, self.current_context, cpu_pct, now)

    def _restore_threads(self, cpu_pct: float, now: float) -> BalanceAction:
        old = self.current_threads
        self.current_threads = self.baseline_threads
        self.current_tier = TIER_CHUNK
        return self._record("restore", TIER_THREADS, "threads", old, self.current_threads, cpu_pct, now)

    def _restore_chunk(self, cpu_pct: float, now: float) -> BalanceAction:
        old = self.chunk_size
        self.chunk_size = CHUNK_SIZE_FULL
        self.current_tier = TIER_NONE
        return self._record("restore", TIER_CHUNK, "chunk_size", old, self.chunk_size, cpu_pct, now)

    # -----------------------------------------------------------
    # _record() below is intentionally still part of the class; the
    # tier->(duration, restore_fn) map _maybe_restore() consults is built
    # right after the class body instead (see _RESTORE_STEPS), since it
    # needs the fully-defined unbound methods as plain callables.
    # -----------------------------------------------------------
    def _record(self, direction: str, tier: int, field_name: str, old_value: Any, new_value: Any, cpu_pct: float, now: float, applied: bool = True) -> BalanceAction:
        action = BalanceAction(
            session_id=self.session_id, model_id=self.model_id, direction=direction, tier=tier,
            field=field_name, old_value=old_value, new_value=new_value, cpu_at_change=cpu_pct, timestamp=now,
        )
        self.history.append(action)
        self.last_change_at = now
        _record_global(action)

        with perf_profiler.timed(f"auto_balancer.{direction}.{field_name}"):
            pass

        log_level = "WARNING" if direction == "throttle" else "INFO"
        unified_log("auto_balancer", log_level, f"{direction} tier={TIER_NAMES[tier]} field={field_name}", {
            "session_id": self.session_id, "model_id": self.model_id,
            "old_value": old_value, "new_value": new_value, "cpu_pct": round(cpu_pct, 1), "applied": applied,
        })
        logger.info(f"auto_balancer[{self.session_id}]: {direction} {field_name} {old_value} -> {new_value} (cpu={cpu_pct:.1f}%)")
        return action

    def get_chunk_size(self) -> int:
        return self.chunk_size

    def snapshot(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "model_id": self.model_id,
            "tier": self.current_tier,
            "tier_name": TIER_NAMES[self.current_tier],
            "cpu_pct": round(self._last_cpu, 1),
            "adjustments": {
                "chunk_size": self.chunk_size,
                "threads": self.current_threads,
                "context": self.current_context,
                "quant": self.current_quant,
            },
            "baseline": {
                "chunk_size": CHUNK_SIZE_FULL,
                "threads": self.baseline_threads,
                "context": self.baseline_context,
                "quant": self.baseline_quant,
            },
            "created_at": self.created_at,
            "last_change_at": self.last_change_at,
        }


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------
def is_eligible(model_cfg: Optional[dict], n_gpu_layers: int = 0) -> bool:
    """
    Universal "local + CPU-only" rule — see the module docstring's
    HONEST SCOPE / eligibility discussion for why there is deliberately
    no parameter-count gate here anymore.
    """
    if not model_cfg:
        return False
    if model_cfg.get("provider") != "local":
        return False
    if n_gpu_layers and n_gpu_layers > 0:
        return False
    return True


# ---------------------------------------------------------------------------
# Quant sibling detection — a genuinely different GGUF file, not a
# pretend swap. See module docstring.
# ---------------------------------------------------------------------------
def _find_lower_quant_sibling(model_cfg: dict) -> Optional[Dict[str, str]]:
    path = model_cfg.get("path")
    current_quant = (model_cfg.get("quant") or "").upper()
    if not path or not current_quant or current_quant not in _QUANT_ORDER:
        return None

    current_idx = _QUANT_ORDER.index(current_quant)
    if current_idx == 0:
        return None  # already the lowest quant this project models

    directory = os.path.dirname(path)
    filename = os.path.basename(path)
    if current_quant not in filename:
        return None

    for candidate_quant in reversed(_QUANT_ORDER[:current_idx]):
        candidate_filename = filename.replace(current_quant, candidate_quant)
        candidate_path = os.path.join(directory, candidate_filename)
        if os.path.exists(candidate_path):
            return {"quant": candidate_quant, "path": candidate_path}
    return None


# ---------------------------------------------------------------------------
# Session registry + global diagnostics history
# ---------------------------------------------------------------------------
_sessions_lock = threading.Lock()
_active_sessions: Dict[str, AutoBalancer] = {}  # model_id -> most recent balancer for that model
_history: Deque[BalanceAction] = deque(maxlen=200)
_session_counter = 0


def _record_global(action: BalanceAction) -> None:
    _history.append(action)


def start_session(session_id: str, model_id: str, model_cfg: dict, baseline_threads: int, baseline_context: int, prompt_token_estimate: int = 0) -> AutoBalancer:
    balancer = AutoBalancer(session_id, model_id, model_cfg, baseline_threads, baseline_context, prompt_token_estimate)
    with _sessions_lock:
        _active_sessions[model_id] = balancer
    return balancer


def end_session(model_id: str) -> None:
    # Deliberately does NOT clear the balancer's final state — a request
    # just after this one for the same model_id should see whatever
    # thread/context/quant overrides are still in effect (they persist
    # until CPU drops and the restore path un-does them, which can span
    # multiple requests), and /v1/diagnostics/autobalance stays useful
    # between requests instead of going blank the instant one finishes.
    pass


def get_active_balancer(model_id: str) -> Optional[AutoBalancer]:
    with _sessions_lock:
        return _active_sessions.get(model_id)


def get_effective_overrides(model_id: str) -> Optional[Dict[str, Any]]:
    """
    What model_loader.py should use for THIS model's next load, or None
    if no active balancer / no adjustment currently in effect.
    """
    balancer = get_active_balancer(model_id)
    if balancer is None or balancer.current_tier < TIER_THREADS:
        return None
    overrides: Dict[str, Any] = {}
    if balancer.current_tier >= TIER_THREADS:
        overrides["n_threads"] = balancer.current_threads
    if balancer.current_tier >= TIER_CONTEXT:
        overrides["max_ctx"] = balancer.current_context
    if balancer.current_tier >= TIER_QUANT and balancer.quant_override_path:
        overrides["model_path"] = balancer.quant_override_path
    return overrides or None


def next_session_id() -> str:
    global _session_counter
    with _sessions_lock:
        _session_counter += 1
        return f"balance-{_session_counter}-{int(time.time() * 1000)}"


def diagnostics_snapshot() -> Dict[str, Any]:
    with _sessions_lock:
        sessions = {model_id: b.snapshot() for model_id, b in _active_sessions.items()}
    return {
        "active_sessions": sessions,
        "recent_actions": [a.to_dict() for a in list(_history)[-50:]],
        "thresholds": {
            "warn_cpu": WARN_CPU, "high_cpu": HIGH_CPU, "critical_cpu": CRITICAL_CPU, "restore_cpu": RESTORE_CPU,
        },
    }
