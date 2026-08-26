# backend/core/performance_estimator.py

"""
CPU-only tokens/sec estimator.

llama.cpp-style GGUF inference on CPU is memory-bandwidth bound: each
generated token requires streaming the (quantized) model weights
through the CPU roughly once. So estimated throughput is modeled as

    tokens/sec ≈ effective_memory_bandwidth_GBps / model_size_GB

where effective bandwidth scales with active thread count (up to a
system bandwidth ceiling, since more threads past the memory
controller's limit stop helping) and is penalized heavily when AVX2
is unavailable, since llama.cpp's quantized matmul/dequant kernels
rely on it.

CPU detection (physical core count, real AVX2/AVX512 flags) is sourced
from hardware_detector, which fixes two real accuracy bugs this module
used to have on its own: os.cpu_count() returns logical threads, not
physical cores (over-counting on any CPU with SMT/Hyper-Threading), and
AVX2 was previously just assumed present on non-Linux platforms since
/proc/cpuinfo doesn't exist there.
"""

import math

from .resource_monitor import ResourceSnapshot
from .safety_profiles import PerformanceProfile
from . import hardware_snapshot_cache
from . import perf_profiler
from .model_metadata import get_model_params, get_quant_bytes, get_quant_difficulty

from logger import get_logger

logger = get_logger(__name__)


# -----------------------------------------------------------
# TUNING CONSTANTS
# -----------------------------------------------------------

# Achievable memory bandwidth per active CPU thread, in GB/s, for a
# memory-bound quantized matmul workload. Tuned against typical
# llama.cpp CPU benchmarks on consumer desktop hardware.
PER_THREAD_BW_GBPS = 4.0

# Most consumer dual-channel DDR4/DDR5 systems top out here regardless
# of thread count — more threads beyond this just contend for the
# same memory bus.
MAX_SYSTEM_BW_GBPS = 50.0

# Multiplier applied when AVX2 is not available. Scalar/SSE fallback
# kernels in llama.cpp are dramatically slower than AVX2 kernels.
NO_AVX2_PENALTY = 0.4

# Context size affects throughput too: attention over a larger KV
# cache costs more per token as generation proceeds. Below this
# baseline there's no penalty; above it, throughput degrades
# logarithmically (doubling context costs a fixed additional slice
# of speed rather than a linear one — matches observed llama.cpp
# behavior better than a linear falloff).
CONTEXT_BASELINE = 2048
CONTEXT_PENALTY_PER_DOUBLING = 0.05
MAX_CONTEXT_PENALTY = 0.3

MIN_TOKENS_PER_SEC = 0.1
MAX_TOKENS_PER_SEC = 100.0


def _context_penalty(max_ctx: int) -> float:
    if max_ctx <= CONTEXT_BASELINE:
        return 0.0

    doublings = math.log2(max_ctx / CONTEXT_BASELINE)
    penalty = min(MAX_CONTEXT_PENALTY, CONTEXT_PENALTY_PER_DOUBLING * doublings)
    return penalty


# -----------------------------------------------------------
# PUBLIC API
# -----------------------------------------------------------

def estimate_speed(snapshot: ResourceSnapshot, model_cfg: dict, profile: PerformanceProfile) -> float:
    """
    Estimate CPU-only inference throughput in tokens/sec for
    model_cfg under the given adaptive PerformanceProfile.
    """
    model_id = model_cfg.get("id", "unknown")
    with perf_profiler.timed("performance_estimator.estimate_speed"):
        return _estimate_speed_inner(snapshot, model_cfg, profile, model_id)


def _estimate_speed_inner(snapshot: ResourceSnapshot, model_cfg: dict, profile: PerformanceProfile, model_id: str) -> float:
    logger.debug(f"estimate_speed() called for model_id={model_id}")

    params = get_model_params(model_cfg)
    bytes_per_param = get_quant_bytes(model_cfg)

    model_size_bytes = params * bytes_per_param
    model_size_gb = model_size_bytes / (1024 ** 3)

    if model_size_gb <= 0:
        logger.debug("estimate_speed() → model_size_gb <= 0, returning 0.0")
        return 0.0

    # TTL-cached (backend.core.hardware_snapshot_cache) rather than a
    # fresh hardware_detector.detect_cpu() every call — this ran once per
    # model per models_list_request before, another real contributor to
    # the "15-second model card stall".
    cpu = hardware_snapshot_cache.get_cpu()
    logical_threads = cpu["logical_threads"]
    avx2 = cpu["avx2"]

    threads = max(1, min(profile.n_threads, logical_threads))
    logger.debug(f"estimate_speed() → threads={threads}, logical_threads={logical_threads}, avx2={avx2}, model_size_gb={model_size_gb:.2f}")

    effective_bw_gbps = min(threads * PER_THREAD_BW_GBPS, MAX_SYSTEM_BW_GBPS)

    if not avx2:
        effective_bw_gbps *= NO_AVX2_PENALTY
        logger.debug(f"estimate_speed() → AVX2 unavailable, penalized bandwidth={effective_bw_gbps:.2f}GB/s")

    quant_difficulty = get_quant_difficulty(model_cfg)
    tokens_per_sec = effective_bw_gbps / model_size_gb / quant_difficulty
    logger.debug(f"estimate_speed() → quant_difficulty={quant_difficulty}x")

    context_penalty = _context_penalty(profile.max_ctx)
    if context_penalty > 0:
        tokens_per_sec *= (1.0 - context_penalty)
        logger.debug(f"estimate_speed() → context={profile.max_ctx}, penalty={context_penalty:.2f}")

    tokens_per_sec = max(MIN_TOKENS_PER_SEC, min(tokens_per_sec, MAX_TOKENS_PER_SEC))

    logger.debug(f"estimate_speed() → model_id={model_id}, tokens_per_sec={tokens_per_sec:.2f}")

    return round(tokens_per_sec, 2)
