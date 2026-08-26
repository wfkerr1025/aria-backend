# backend/core/warning_manager.py

"""
Session-scoped + risk-scoped warning system.

Two independent warning levels, matched to how often they should
actually reach the user:

  NORMAL    Routine, expected-state warnings ("Model may be heavy for
            your PC.", "Auto-balance is active.", "Performance may be
            reduced.") -- each shown at most ONCE per connection. The
            point is to inform the user the first time a state is
            entered, not to repeat something already acknowledged every
            single turn it remains true.

  CRITICAL  Genuinely actionable resource conditions (sustained CPU
            >95%, RAM >90%, thermal throttling, the auto-balancer maxed
            out and still unsafe, or even the smallest installed model
            failing safety_manager.evaluate_safety()) -- shown EVERY
            time they're detected, deliberately overriding the
            once-per-session rule, because these represent a live risk
            the user needs to see again if it recurs.

`session_state` is a small plain dict the CALLER owns and passes back in
on every call -- see backend/websocket/handlers.py's per-connection
handler object, which already holds similarly-scoped state (e.g.
`_override_model_id`). This module deliberately holds no session
registry of its own, unlike auto_balancer.py's `_active_sessions` (that
one is keyed by model_id and outlives a single connection on purpose;
warning dedup is the opposite -- strictly per-connection, gone the
moment the connection is, via new_session_state()).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import auto_balancer
from .model_registry import get_emergency_model_id
from .safety_manager import SafetyDecision
from .resource_monitor import ResourceSnapshot

from backend.logger import log as unified_log
from logger import get_logger

logger = get_logger(__name__)

LEVEL_NORMAL = "normal"
LEVEL_CRITICAL = "critical"

# Stable ids for the three once-per-session warnings named in the spec.
WARNING_HEAVY_MODEL = "heavy_model"
WARNING_AUTO_BALANCE_ACTIVE = "auto_balance_active"
WARNING_PERFORMANCE_REDUCED = "performance_reduced"

# Critical warning ids -- always emitted when triggered, never deduped.
WARNING_CPU_SUSTAINED = "cpu_sustained_critical"
WARNING_RAM_CRITICAL = "ram_critical"
WARNING_THERMAL_THROTTLING = "thermal_throttling"
WARNING_BALANCER_MAXED = "balancer_cannot_scale_down_enough"
WARNING_SMALLEST_MODEL_UNSAFE = "smallest_model_unsafe"

# Named explicitly in the warning spec ("CPU > 95% sustained", "RAM >
# 90%") -- deliberately distinct from (and stricter than)
# safety_manager.BLOCK_CPU_PCT/BLOCK_RAM_PCT, which gate whether
# inference is allowed to start at all. These gate a live, already-
# running-system warning instead, so a slightly different threshold is
# not a bug -- the two systems answer different questions.
CRITICAL_CPU_SUSTAINED_PCT = 95.0
CRITICAL_RAM_PCT = 90.0


@dataclass
class WarningEvent:
    id: str
    level: str  # "normal" | "critical"
    message: str
    model_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "level": self.level, "message": self.message, "model_id": self.model_id}


def new_session_state() -> Dict[str, Any]:
    """A fresh per-connection dedup record. Callers store this once per
    connection (alongside their other per-connection state) and pass it
    into every evaluate_warnings() call for that connection's lifetime."""
    return {"shown_normal_ids": set()}


def _emit_normal(session_state: Dict[str, Any], warning_id: str, message: str, model_id: Optional[str], out: List[WarningEvent]) -> None:
    shown = session_state.setdefault("shown_normal_ids", set())
    if warning_id in shown:
        return
    shown.add(warning_id)
    out.append(WarningEvent(warning_id, LEVEL_NORMAL, message, model_id))


def _emit_critical(warning_id: str, message: str, model_id: Optional[str], out: List[WarningEvent]) -> None:
    out.append(WarningEvent(warning_id, LEVEL_CRITICAL, message, model_id))


def evaluate_warnings(
    session_state: Dict[str, Any],
    model_cfg: Optional[dict],
    safety_decision: Optional[SafetyDecision] = None,
    balancer_snapshot: Optional[Dict[str, Any]] = None,
    resource_snapshot: Optional[ResourceSnapshot] = None,
    thermal: Optional[Dict[str, Any]] = None,
    cpu_sustained_critical: bool = False,
) -> List[WarningEvent]:
    """
    Evaluate every warning condition for the current turn and return the
    events that should actually be sent this time -- normal warnings
    already shown on this connection are silently skipped; critical
    warnings are always included regardless of history.

    Every input besides `session_state` is optional so a call site that
    doesn't have a piece of data yet (e.g. no balancer session started,
    or no live resource snapshot taken) can still get whatever the other
    inputs support evaluated, rather than requiring every caller to
    assemble the full picture up front.

    `cpu_sustained_critical` is a pre-computed "has CPU stayed above the
    critical line for the required duration" flag, not a raw percentage
    -- the "sustained" qualifier the spec attaches to the CPU condition
    (but not to the RAM one) needs duration-tracking state that lives in
    backend.core.runtime_health_monitor.RuntimeHealthMonitor, not in this
    stateless evaluator. RAM has no such qualifier in the spec, so its
    check stays a plain instantaneous threshold on `resource_snapshot`.
    """
    model_id = model_cfg.get("id") if model_cfg else None
    out: List[WarningEvent] = []

    # --- Normal, once-per-connection ---------------------------------
    if safety_decision is not None and safety_decision.severity in ("caution", "block"):
        _emit_normal(session_state, WARNING_HEAVY_MODEL, "Model may be heavy for your PC.", model_id, out)
        _emit_normal(session_state, WARNING_PERFORMANCE_REDUCED, "Performance may be reduced.", model_id, out)

    if balancer_snapshot is not None and balancer_snapshot.get("tier", 0) > 0:
        _emit_normal(session_state, WARNING_AUTO_BALANCE_ACTIVE, "Auto-balance is active.", model_id, out)

    # --- Critical, every time ----------------------------------------
    if cpu_sustained_critical:
        _emit_critical(WARNING_CPU_SUSTAINED, "CPU usage is critically high and sustained.", model_id, out)

    if resource_snapshot is not None and resource_snapshot.ram_used_pct >= CRITICAL_RAM_PCT:
        _emit_critical(WARNING_RAM_CRITICAL, "RAM usage is critically high.", model_id, out)

    if thermal is not None and thermal.get("throttling_detected"):
        _emit_critical(WARNING_THERMAL_THROTTLING, "Thermal throttling detected -- your system is overheating.", model_id, out)

    if balancer_snapshot is not None:
        still_unsafe = safety_decision is not None and safety_decision.severity == "block"
        if balancer_snapshot.get("tier", 0) >= auto_balancer.TIER_QUANT and still_unsafe:
            _emit_critical(WARNING_BALANCER_MAXED, "Auto-balance cannot scale down enough to run this model safely.", model_id, out)

    if safety_decision is not None and safety_decision.severity == "block":
        emergency_id = get_emergency_model_id()
        if emergency_id and model_id == emergency_id:
            _emit_critical(
                WARNING_SMALLEST_MODEL_UNSAFE,
                "Even the smallest available model is unsafe to run on this system right now.",
                model_id, out,
            )

    if out:
        unified_log("warning_manager", "INFO", "warnings evaluated", {
            "model_id": model_id, "emitted": [w.id for w in out],
        })
    return out
