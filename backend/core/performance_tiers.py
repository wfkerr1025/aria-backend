# backend/core/performance_tiers.py

"""
Modern performance-tier scoring system.

Produces a coarse Excellent / Great / Good / Usable / Poor /
Not Recommended verdict for how well a given system will run a given
model, using one of three scoring modes depending on how the model's
weights relate to available VRAM:

  - split_load: the model's weights fit entirely within the GPU's VRAM
    — dominated by GPU throughput (VRAM x memory bandwidth x CUDA
    cores), plus a smaller additive CPU contribution.
  - offload: the model doesn't fit in VRAM, so most of it has to stream
    from RAM/disk — dominated by storage speed x available RAM x CPU
    throughput, divided by model size (bigger model = more streaming
    per token = lower score).
  - cpu_only: no GPU at all — dominated by CPU AVX throughput x RAM
    bandwidth, divided by model size.

This is a coarse, order-of-magnitude classifier for UI badges, not a
precise benchmark predictor — there is no dependency-free way to run an
actual FLOPS/bandwidth microbenchmark from pure Python (see
hardware_detector.py's docstring). The formulas above come directly
from this feature's spec; the bucket thresholds below are tuned against
three representative real systems (a high-end desktop, a mid-range
desktop, and a GPU-less low-end desktop) across the 12B/30B/70B/405B
(and 7B) size classes — see
backend/tests/hardware_and_performance_tests.py for the exact worked
examples this was calibrated against.

Inputs are the same plain dicts hardware_detector.py's detect_cpu() /
detect_ram() / detect_gpu() / detect_storage() produce, so this module
never probes hardware itself — it's a pure function of already-known
hardware facts, making it directly unit-testable with literal, named
hardware specs.
"""

from __future__ import annotations

from .model_metadata import get_model_params, get_quant_bytes, get_quant_difficulty

TIER_ORDER = ["Excellent", "Great", "Good", "Usable", "Poor", "Not Recommended"]

# Each ladder is checked highest-threshold-first; the first threshold the
# score meets or exceeds wins. Falling below every threshold is
# "Not Recommended". These three ladders are NOT comparable to each
# other — each mode's formula produces numbers on its own scale (VRAM x
# bandwidth x CUDA cores is many orders of magnitude larger than
# cores x GFLOPS x RAM-bandwidth / model_size_gb), so each has its own
# independently-calibrated threshold set.
_CPU_ONLY_THRESHOLDS = [
    (20000.0, "Excellent"),
    (14000.0, "Great"),
    (9000.0, "Good"),
    (7500.0, "Usable"),
    (3000.0, "Poor"),
]

_SPLIT_LOAD_THRESHOLDS = [
    (50_000_000.0, "Excellent"),
    (10_000_000.0, "Great"),
    (3_000_000.0, "Good"),
    (800_000.0, "Usable"),
    (200_000.0, "Poor"),
]

_OFFLOAD_THRESHOLDS = [
    (25000.0, "Excellent"),
    (8000.0, "Great"),
    (1300.0, "Good"),
    (900.0, "Usable"),
    (300.0, "Poor"),
]

_THRESHOLDS_BY_MODE = {
    "cpu_only": _CPU_ONLY_THRESHOLDS,
    "split_load": _SPLIT_LOAD_THRESHOLDS,
    "offload": _OFFLOAD_THRESHOLDS,
}


def _bucket(score: float, thresholds: list) -> str:
    for threshold, label in thresholds:
        if score >= threshold:
            return label
    return "Not Recommended"


def select_mode(gpu: dict | None, model_size_gb: float) -> str:
    """
    split_load if the model's weights fit within VRAM outright (ignoring
    KV-cache/activation headroom — a deliberate simplification: this is
    a coarse tiering signal, not an exact load-planning calculation);
    offload if a GPU exists but the model doesn't fit; cpu_only if there
    is no GPU at all.
    """
    if gpu is None:
        return "cpu_only"
    if model_size_gb <= gpu["vram_total_gb"]:
        return "split_load"
    return "offload"


def compute_raw_score(mode: str, cpu: dict, ram: dict, gpu: dict | None, storage: dict,
                       model_size_gb: float, quant_difficulty: float) -> float:
    if mode == "cpu_only":
        return (cpu["physical_cores"] * cpu["estimated_gflops"] * ram["bandwidth_gbps"]) / (model_size_gb * quant_difficulty)

    if mode == "split_load":
        cpu_score = cpu["physical_cores"] * cpu["estimated_gflops"]
        gpu_score = gpu["vram_total_gb"] * (gpu["memory_bandwidth_gbps"] or 0.0) * (gpu["cuda_cores"] or 0)
        return (gpu_score + cpu_score) / quant_difficulty

    if mode == "offload":
        return (storage["read_speed_gbps"] * ram["available_gb"] * cpu["estimated_gflops"]) / (model_size_gb * quant_difficulty)

    raise ValueError(f"unknown performance mode: {mode}")


def estimate_performance_tier(cpu: dict, ram: dict, gpu: dict | None, storage: dict, model_cfg: dict) -> dict:
    """
    Full pipeline: model size + quant difficulty → mode selection → raw
    score → tier. Returns {"mode", "score", "tier", "model_size_gb"}.
    """
    params = get_model_params(model_cfg)
    bytes_per_param = get_quant_bytes(model_cfg)
    quant_difficulty = get_quant_difficulty(model_cfg)

    model_size_gb = (params * bytes_per_param) / (1024 ** 3)
    if model_size_gb <= 0:
        return {"mode": "unknown", "score": 0.0, "tier": "Not Recommended", "model_size_gb": 0.0}

    mode = select_mode(gpu, model_size_gb)
    raw_score = compute_raw_score(mode, cpu, ram, gpu, storage, model_size_gb, quant_difficulty)
    tier = _bucket(raw_score, _THRESHOLDS_BY_MODE[mode])

    return {
        "mode": mode,
        "score": round(raw_score, 2),
        "tier": tier,
        "model_size_gb": round(model_size_gb, 2),
    }
