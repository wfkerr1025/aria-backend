# backend/core/safety_manager.py

from dataclasses import dataclass
import os

from .resource_monitor import get_resource_snapshot, ResourceSnapshot
from .safety_profiles import select_profile, PerformanceProfile
from .model_metadata import get_model_params, get_quant_bytes
from .compatibility_checker import check_requirements
from .performance_estimator import estimate_speed

from logger import get_logger

logger = get_logger(__name__)


@dataclass
class SafetyDecision:
    safe_to_run: bool
    requires_warning: bool
    # "ok" | "caution" | "block" — see the threshold table below. Additive
    # field: safe_to_run/requires_warning keep their exact prior meaning
    # (safe_to_run is False only for "block"; requires_warning is True for
    # both "caution" and "block"), so nothing that only read those two
    # breaks.
    severity: str
    message: str
    profile: PerformanceProfile
    snapshot: ResourceSnapshot
    projected_cpu_pct: float = 0.0
    projected_ram_pct: float = 0.0
    projected_vram_pct: float = 0.0
    projected_speed_toksec: float = 0.0


# ============================================================
# SAFETY THRESHOLDS — three tiers, checked independently per resource.
#
#   OK       Projected usage stays under the CAUTION_* line. No warning
#            is sent at all (see evaluate_safety() below).
#
#   CAUTION  Projected usage exceeds the CAUTION_* line for at least one
#            resource, but the model still meets its own minimum
#            requirements (backend.core.compatibility_checker). The user
#            sees the safety_warning modal and can Proceed Anyway, switch
#            to a suggested lighter model, or cancel — same flow as
#            before this tiering existed.
#
#   BLOCK    Either (a) the model fails its own minimum requirements
#            outright, or (b) projected usage would cross the BLOCK_*
#            line — i.e. the resource is projected to be essentially
#            exhausted (real thrash/OOM territory), not just
#            "uncomfortably high". BLOCK is still not a hard stop at the
#            code level: backend/websocket/handlers.py never starts
#            inference for anything above OK without an explicit
#            allow_override=True (from "Proceed Anyway"), the one-shot
#            post-override bypass, or session-wide skipSafetyCheck — see
#            _handle_chat_request(). This tier exists so the UI can make
#            that distinction visible instead of presenting a genuinely
#            dangerous projection with the same mild framing as a merely
#            elevated one.
#
# CAUTION_* values are unchanged from this module's original single-tier
# thresholds (no behavior change for the common case). BLOCK_* values are
# new: previously "meets minimum but projected usage is still severe"
# (e.g. 98% RAM) had no ceiling above CAUTION at all.
# ============================================================
CAUTION_RAM_PCT = 80.0
BLOCK_RAM_PCT = 95.0

CAUTION_VRAM_PCT = 75.0
BLOCK_VRAM_PCT = 92.0

CAUTION_CPU_PCT_UNITY = 50.0
CAUTION_CPU_PCT_SOLO = 75.0
BLOCK_CPU_PCT = 95.0  # not split by Unity — at this level everything stutters regardless

# Fixed per-connection overhead for this app's own WebSocket/IPC runtime
# (asyncio + `websockets` send/receive buffers, JSON encode/decode scratch
# space, the heartbeat/batch_request bookkeeping in backend/ipc_router.py
# and backend/ipc_schema.py) — small and roughly constant regardless of
# model size, so it's added once here rather than scaled by anything.
# Deliberately NOT modeled as a separate CPU constant: heartbeat/batch
# traffic is one small JSON message roughly every 10s, whose CPU cost is
# genuinely negligible next to inference — inventing a CPU percentage for
# it would manufacture false precision, not improve accuracy.
IPC_RUNTIME_OVERHEAD_RAM_GB = 0.1


def estimate_ram_usage(snapshot: ResourceSnapshot, profile: PerformanceProfile, model_cfg: dict) -> float:
    logger.debug("Estimating RAM usage")

    params = get_model_params(model_cfg)
    bytes_per_param = get_quant_bytes(model_cfg)

    total_layers = 80
    gpu_layer_ratio = profile.n_gpu_layers / total_layers

    ram_model_bytes = params * bytes_per_param * (1.0 - gpu_layer_ratio)
    ram_model_gb = ram_model_bytes / (1024 ** 3)

    ram_ctx_gb = 0.000015 * profile.max_ctx

    projected_ram_gb = (
        snapshot.ram_used_gb + ram_model_gb + ram_ctx_gb + IPC_RUNTIME_OVERHEAD_RAM_GB
    )
    projected_ram_pct = (projected_ram_gb / snapshot.ram_total_gb) * 100 if snapshot.ram_total_gb else 0.0

    logger.debug(f"Projected RAM → {projected_ram_pct:.2f}%")
    return projected_ram_pct


def estimate_vram_usage(snapshot: ResourceSnapshot, profile: PerformanceProfile, model_cfg: dict) -> float:
    logger.debug("Estimating VRAM usage")

    if snapshot.vram_total_gb <= 0:
        logger.debug("No VRAM detected → returning 0%")
        return 0.0

    params = get_model_params(model_cfg)
    bytes_per_param = get_quant_bytes(model_cfg)

    total_layers = 80
    gpu_layer_ratio = profile.n_gpu_layers / total_layers

    vram_model_bytes = params * bytes_per_param * gpu_layer_ratio
    vram_model_gb = vram_model_bytes / (1024 ** 3)

    vram_overhead_gb = 0.8

    projected_vram_gb = snapshot.vram_used_gb + vram_model_gb + vram_overhead_gb
    projected_vram_pct = (projected_vram_gb / snapshot.vram_total_gb) * 100

    logger.debug(f"Projected VRAM → {projected_vram_pct:.2f}%")
    return projected_vram_pct


def estimate_cpu_usage(snapshot: ResourceSnapshot, profile: PerformanceProfile, model_cfg: dict) -> float:
    logger.debug("Estimating CPU usage")

    # gpu_layer_ratio matches estimate_ram_usage/estimate_vram_usage's own
    # calculation exactly (same total_layers=80 convention) — a model with
    # most of its layers offloaded to GPU does proportionally less CPU
    # work per token, so it must not project the same CPU load as a fully
    # CPU-resident model at the same thread count. Previously this ignored
    # model_cfg entirely and scaled with thread count alone, which
    # over-projected (and over-warned) for exactly the GPU-heavy case —
    # notably smaller models, which safety_profiles._adaptive_gpu_layers
    # tends to grant more relative GPU headroom to.
    total_layers = 80
    gpu_layer_ratio = profile.n_gpu_layers / total_layers if total_layers else 0.0
    cpu_layer_ratio = 1.0 - gpu_layer_ratio

    # Per-thread contribution tuned against real llama.cpp CPU-only
    # behavior (see performance_estimator.py's PER_THREAD_BW_GBPS for the
    # analogous throughput-side tuning).
    additional_cpu = profile.n_threads * 4 * cpu_layer_ratio
    projected_cpu_pct = min(100.0, snapshot.cpu_usage + additional_cpu)

    logger.debug(f"Projected CPU → {projected_cpu_pct:.2f}% (gpu_layer_ratio={gpu_layer_ratio:.2f})")
    return projected_cpu_pct


def _speed_message_lines(profile: PerformanceProfile, projected_speed_toksec: float, rec_speed_toksec) -> str:
    """
    The estimate from performance_estimator.estimate_speed() is, by that
    module's own design (see its docstring), a CPU-only bandwidth-bound
    model — it does not know how much faster this specific run will be
    when profile.n_gpu_layers > 0. Presenting it as an unqualified single
    number in that case is exactly the "misleading estimate" this
    function used to produce. Rather than fabricate a blended CPU+GPU
    number from a GPU bandwidth constant this codebase has no real data
    for, this labels the number for what it actually is (a CPU-only
    floor) and says so plainly when GPU offload will make the real
    number higher — honest under-promising instead of ungrounded
    precision.
    """
    line = f"- Estimated speed (CPU-only): {projected_speed_toksec:.1f} tok/sec"
    if profile.n_gpu_layers > 0:
        line += f" — actual speed will be higher ({profile.n_gpu_layers}/80 layers offloaded to GPU)"
    if rec_speed_toksec is not None:
        line += f"\n- This model's recommended speed for good responsiveness: {rec_speed_toksec} tok/sec"
    return line


def evaluate_safety(model_cfg: dict) -> SafetyDecision:
    logger.debug(f"evaluate_safety() called for model_id={model_cfg.get('id')}")

    snapshot = get_resource_snapshot()
    profile = select_profile(snapshot, model_cfg)

    compat = check_requirements(snapshot, model_cfg)
    if not compat["meets_minimum"]:
        logger.debug(
            f"evaluate_safety() → blocked (fails minimum requirements), model_id={model_cfg.get('id')}, "
            f"missing_minimum={compat['missing_minimum']}"
        )
        return SafetyDecision(
            safe_to_run=False,
            requires_warning=True,
            severity="block",
            message=(
                f"🛑 Model ID: {model_cfg.get('id')}\n"
                "Your system does NOT meet the minimum requirements for this model — "
                f"missing: {', '.join(compat['missing_minimum']) or 'unspecified requirement'}.\n\n"
                "Running it is likely to fail to load, or to be unusably slow if it does. "
                "You can proceed anyway, or switch to a lighter model."
            ),
            profile=profile,
            snapshot=snapshot,
            projected_cpu_pct=0,
            projected_ram_pct=0,
            projected_vram_pct=0,
            projected_speed_toksec=0,
        )

    projected_ram_pct = estimate_ram_usage(snapshot, profile, model_cfg)
    projected_vram_pct = estimate_vram_usage(snapshot, profile, model_cfg)
    projected_cpu_pct = estimate_cpu_usage(snapshot, profile, model_cfg)
    projected_speed_toksec = estimate_speed(snapshot, model_cfg, profile)

    rec_speed_toksec = model_cfg.get("requirements", {}).get("recSpeedTokSec")

    cpu_caution_target = CAUTION_CPU_PCT_UNITY if snapshot.unity_running else CAUTION_CPU_PCT_SOLO
    logger.debug(f"CPU caution target → {cpu_caution_target}% (UnityRunning={snapshot.unity_running})")

    is_block = (
        projected_ram_pct >= BLOCK_RAM_PCT or
        projected_vram_pct >= BLOCK_VRAM_PCT or
        projected_cpu_pct >= BLOCK_CPU_PCT
    )
    is_caution = not is_block and (
        projected_ram_pct > CAUTION_RAM_PCT or
        projected_vram_pct > CAUTION_VRAM_PCT or
        projected_cpu_pct > cpu_caution_target
    )

    if is_block:
        severity = "block"
    elif is_caution:
        severity = "caution"
    else:
        severity = "ok"

    requires_warning = severity != "ok"
    safe_to_run = severity != "block"

    speed_lines = _speed_message_lines(profile, projected_speed_toksec, rec_speed_toksec)
    has_gpu = snapshot.vram_total_gb > 0

    def _current_usage_lines() -> str:
        lines = [
            f"- CPU: {snapshot.cpu_usage:.0f}%",
            f"- RAM: {snapshot.ram_used_pct:.0f}% of {snapshot.ram_total_gb:.1f} GB",
        ]
        # Omitted entirely (not "0% of 0.0 GB") when there's no GPU at
        # all — showing a percentage of a zero-byte budget is meaningless
        # and reads as a bug, not as "no GPU present".
        if has_gpu:
            lines.append(f"- VRAM: {snapshot.vram_used_pct:.0f}% of {snapshot.vram_total_gb:.1f} GB")
        return "\n".join(lines)

    def _projected_usage_lines(limit_label: str, cpu_limit: float, ram_limit: float, vram_limit: float) -> str:
        lines = [
            f"- CPU: {projected_cpu_pct:.0f}% ({limit_label}: {cpu_limit:.0f}%)",
            f"- RAM: {projected_ram_pct:.0f}% ({limit_label}: {ram_limit:.0f}%)",
        ]
        if has_gpu:
            lines.append(f"- VRAM: {projected_vram_pct:.0f}% ({limit_label}: {vram_limit:.0f}%)")
        return "\n".join(lines)

    if severity == "block":
        logger.debug("Safety hard block triggered (severe projected usage)")
        msg = (
            f"🛑 Model ID: {model_cfg.get('id')}\n"
            "Running this model would push a resource to its limit — this is likely to "
            "cause severe slowdowns, an out-of-memory failure, or system instability, "
            "not just reduced performance.\n\n"
            f"Current usage:\n{_current_usage_lines()}\n\n"
            f"Projected usage with this model:\n"
            f"{_projected_usage_lines('hard limit', BLOCK_CPU_PCT, BLOCK_RAM_PCT, BLOCK_VRAM_PCT)}\n"
            f"{speed_lines}\n\n"
            "You can proceed anyway, close other programs first, or switch to a lighter model."
        )
    elif severity == "caution":
        logger.debug("Safety caution triggered")
        msg = (
            f"⚠ Model ID: {model_cfg.get('id')}\n"
            "Running this model may impact system performance.\n\n"
            f"Current usage:\n{_current_usage_lines()}\n\n"
            f"Projected usage with this model:\n"
            f"{_projected_usage_lines('comfortable max', cpu_caution_target, CAUTION_RAM_PCT, CAUTION_VRAM_PCT)}\n"
            f"{speed_lines}\n\n"
            "You can proceed, close other programs, or switch to a lighter model."
        )
    else:
        logger.debug("Model is safe to run")
        vram_line = f"\n- VRAM: {projected_vram_pct:.0f}%" if has_gpu else ""
        msg = (
            "Resources are within safe limits for this model.\n\n"
            f"Projected usage:\n"
            f"- CPU: {projected_cpu_pct:.0f}%\n"
            f"- RAM: {projected_ram_pct:.0f}%"
            f"{vram_line}\n"
            f"{speed_lines}"
        )

    decision = SafetyDecision(
        safe_to_run=safe_to_run,
        requires_warning=requires_warning,
        severity=severity,
        message=msg,
        profile=profile,
        snapshot=snapshot,
        projected_cpu_pct=projected_cpu_pct,
        projected_ram_pct=projected_ram_pct,
        projected_vram_pct=projected_vram_pct,
        projected_speed_toksec=projected_speed_toksec,
    )

    logger.debug(
        f"SafetyDecision → severity={severity}, warning={requires_warning}, "
        f"CPU={projected_cpu_pct:.2f}%, RAM={projected_ram_pct:.2f}%, VRAM={projected_vram_pct:.2f}%, "
        f"Speed={projected_speed_toksec:.2f} tok/sec"
    )

    return decision
