# backend/tests/hardware_and_performance_tests.py
#
# Regression tests for the hardware-detection and model-performance-
# estimator overhaul:
#   - backend/core/hardware_detector.py: CPU generation detection, AVX2
#     GFLOPS tiering, RAM/VRAM/SSD tiering, RAM pressure.
#   - backend/core/model_metadata.py: quantization difficulty
#     multipliers, wider param-count inference (30B/70B/405B).
#   - backend/core/model_size_requirements.py: canonical per-size-tier
#     requirement thresholds (7B/12B/30B/70B/405B).
#   - backend/core/compatibility_checker.py: physical-core-based
#     checking (not logical threads), AVX2-tier and storage-type gating,
#     exceeds_recommended detection, size-table fallback for models
#     with no hand-authored requirements block.
#   - backend/core/performance_tiers.py: the Excellent/Great/Good/
#     Usable/Poor/Not Recommended scoring system, exercised against
#     three named real-world systems (Section 6 of the spec this was
#     built from).
#
# Self-contained plain-assert tests, matching backend/tests/
# safety_projection_tests.py — not pytest. Run directly:
#
#   python backend/tests/hardware_and_performance_tests.py

from __future__ import annotations

import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ============================================================
# Named test-system hardware fixtures (Section 6)
#
# CUDA core counts, compute capabilities, and GPU memory bandwidths are
# each GPU's real published spec. CPU physical/logical core counts and
# base clocks are each CPU's real published spec. RAM bandwidth
# (channel speed x channel count), available-RAM-at-idle, and NVMe
# sequential read speed are representative estimates for a system of
# that class (real values vary by exact kit/drive) — these are the
# same kind of "tuned against typical real-world configurations"
# constants backend/core/performance_estimator.py already documents
# for its own bandwidth model.
# ============================================================

def _high_end_system():
    """i9-14900KF, 64GB DDR5 RAM, RTX 4070 Ti SUPER — Section 6 system 1."""
    from backend.core.hardware_detector import classify_cpu, classify_ram, classify_gpu, classify_storage

    cpu = classify_cpu(
        physical_cores=24, logical_threads=32, base_clock_ghz=3.2,
        avx2=True, avx512=False, brand="Intel(R) Core(TM) i9-14900KF",
    )
    ram = classify_ram(total_gb=64, available_gb=56)
    ram["bandwidth_gbps"] = 96.0  # DDR5-6000 dual-channel
    gpu = classify_gpu(
        name="NVIDIA GeForce RTX 4070 Ti SUPER", vram_total_gb=16, vram_used_gb=0,
        cuda_cores=8448, compute_capability="8.9", memory_bandwidth_gbps=672.3,
    )
    storage = classify_storage(storage_type="NVMe", read_speed_gbps=6.0, write_speed_gbps=5.5)
    return cpu, ram, gpu, storage


def _mid_range_system():
    """Ryzen 5 5600X, 32GB DDR4 RAM, RTX 3060 — Section 6 system 2."""
    from backend.core.hardware_detector import classify_cpu, classify_ram, classify_gpu, classify_storage

    cpu = classify_cpu(
        physical_cores=6, logical_threads=12, base_clock_ghz=3.7,
        avx2=True, avx512=False, brand="AMD Ryzen 5 5600X 6-Core Processor",
    )
    ram = classify_ram(total_gb=32, available_gb=24)
    ram["bandwidth_gbps"] = 51.2  # DDR4-3200 dual-channel
    gpu = classify_gpu(
        name="NVIDIA GeForce RTX 3060", vram_total_gb=12, vram_used_gb=0,
        cuda_cores=3584, compute_capability="8.6", memory_bandwidth_gbps=360.0,
    )
    storage = classify_storage(storage_type="NVMe", read_speed_gbps=4.8, write_speed_gbps=4.0)
    return cpu, ram, gpu, storage


def _low_end_system():
    """i5-8400, 16GB DDR4 RAM, no GPU — Section 6 system 3."""
    from backend.core.hardware_detector import classify_cpu, classify_ram, classify_storage

    cpu = classify_cpu(
        physical_cores=6, logical_threads=6, base_clock_ghz=2.8,
        avx2=True, avx512=False, brand="Intel(R) Core(TM) i5-8400 CPU",
    )
    ram = classify_ram(total_gb=16, available_gb=8)
    ram["bandwidth_gbps"] = 42.7  # DDR4-2666 dual-channel
    gpu = None
    storage = classify_storage(storage_type="SATA SSD", read_speed_gbps=0.5, write_speed_gbps=0.45)
    return cpu, ram, gpu, storage


def _model_cfg(model_id: str, params: int, quant: str = "Q4_K_M") -> dict:
    return {
        "id": model_id,
        "params": params,
        "quant": quant,
        # get_quant_bytes()/get_quant_difficulty() exact-match against the
        # bare "Q4_K" family via filename substring, mirroring the
        # pattern already established in safety_projection_tests.py.
        "defaultFilename": f"{model_id}.{quant}.gguf",
    }


# ============================================================
# PART 1 — CPU generation / AVX2 tiering (hardware_detector.py)
# ============================================================
def test_intel_generation_detection():
    from backend.core.hardware_detector import detect_cpu_generation

    assert detect_cpu_generation("Intel(R) Core(TM) i9-14900KF") == "Intel 14th Gen"
    assert detect_cpu_generation("Intel(R) Core(TM) i7-13700K") == "Intel 13th Gen"
    assert detect_cpu_generation("Intel(R) Core(TM) i5-12400") == "Intel 12th Gen"
    # Out of the supported 12th-14th gen range → None, not a wrong guess.
    assert detect_cpu_generation("Intel(R) Core(TM) i5-8400 CPU") is None


def test_amd_generation_detection():
    from backend.core.hardware_detector import detect_cpu_generation

    assert detect_cpu_generation("AMD Ryzen 5 5600X 6-Core Processor") == "AMD Ryzen 5000 Series"
    assert detect_cpu_generation("AMD Ryzen 9 7950X3D 16-Core Processor") == "AMD Ryzen 7000 Series"
    assert detect_cpu_generation("AMD Ryzen 7 6800H") == "AMD Ryzen 6000 Series"
    assert detect_cpu_generation("AMD Ryzen 7 3700X") is None


def test_avx2_gflops_tiering_buckets():
    from backend.core.hardware_detector import avx2_tier

    assert avx2_tier(149.9) == "Low"
    assert avx2_tier(150.0) == "Medium"
    assert avx2_tier(300.0) == "Medium"
    assert avx2_tier(300.1) == "High"


def test_high_end_cpu_classifies_as_high_avx2_tier():
    cpu, _, _, _ = _high_end_system()
    assert cpu["avx2_tier"] == "High", cpu
    assert cpu["generation"] == "Intel 14th Gen"


def test_low_end_cpu_classifies_as_medium_avx2_tier():
    # i5-8400: 6 physical cores x 2.8GHz x 16 FLOPs/cycle (AVX2) =
    # 268.8 GFLOPS, which the spec's own bucket (Medium: 150-300)
    # actually places in Medium, not Low — a 6-core AVX2 desktop CPU is
    # "low-end" for running a 12B+ LLM, but its raw AVX2 vector
    # throughput is still solidly mid-pack, which is exactly why AVX2
    # throughput and "is this a good LLM machine" are tracked as
    # separate signals in this feature rather than conflated into one.
    cpu, _, _, _ = _low_end_system()
    assert cpu["avx2_tier"] == "Medium", cpu


def test_a_genuinely_weak_cpu_classifies_as_low_avx2_tier():
    from backend.core.hardware_detector import classify_cpu

    # A dual-core budget/older part — low core count and clock speed
    # even with AVX2 present.
    cpu = classify_cpu(physical_cores=2, logical_threads=4, base_clock_ghz=2.0, avx2=True, avx512=False)
    assert cpu["avx2_tier"] == "Low", cpu


def test_cpu_with_no_avx_support_gets_none_tier():
    from backend.core.hardware_detector import classify_cpu

    cpu = classify_cpu(physical_cores=4, logical_threads=4, base_clock_ghz=2.0, avx2=False, avx512=False)
    assert cpu["avx2_tier"] == "None"


# ============================================================
# PART 2 — RAM / VRAM / SSD tiering (hardware_detector.py)
# ============================================================
def test_ram_tiering_buckets():
    from backend.core.hardware_detector import ram_tier

    assert ram_tier(15.9) == "Low"
    assert ram_tier(16.0) == "Medium"
    assert ram_tier(31.9) == "Medium"
    assert ram_tier(32.0) == "High"
    assert ram_tier(64.0) == "High"
    assert ram_tier(64.1) == "Ultra"


def test_ram_pressure_flag():
    from backend.core.hardware_detector import classify_ram

    low_pressure = classify_ram(total_gb=64, available_gb=45)  # 19GB used
    high_pressure = classify_ram(total_gb=64, available_gb=40)  # 24GB used

    assert low_pressure["pressure"] is False
    assert high_pressure["pressure"] is True


def test_vram_tiering_buckets():
    from backend.core.hardware_detector import vram_tier

    assert vram_tier(5.9) == "Low"
    assert vram_tier(6.0) == "Medium"
    assert vram_tier(11.9) == "Medium"
    assert vram_tier(12.0) == "High"
    assert vram_tier(16.0) == "High"
    assert vram_tier(16.1) == "Ultra"


def test_ssd_tiering_buckets():
    from backend.core.hardware_detector import ssd_tier

    assert ssd_tier(0.9) == "Low"
    assert ssd_tier(1.0) == "Medium"
    assert ssd_tier(2.9) == "Medium"
    assert ssd_tier(3.0) == "High"
    assert ssd_tier(5.0) == "High"
    assert ssd_tier(5.1) == "Ultra"


# ============================================================
# PART 3 — quantization difficulty multipliers (model_metadata.py)
# ============================================================
def test_quant_difficulty_multipliers():
    from backend.core.model_metadata import get_quant_difficulty

    assert get_quant_difficulty(_model_cfg("m", 7_000_000_000, "Q2_K")) == 0.6
    assert get_quant_difficulty(_model_cfg("m", 7_000_000_000, "Q3_K_M")) == 0.8
    assert get_quant_difficulty(_model_cfg("m", 7_000_000_000, "Q4_K_M")) == 1.0
    assert get_quant_difficulty(_model_cfg("m", 7_000_000_000, "Q5_K_M")) == 1.3
    assert get_quant_difficulty(_model_cfg("m", 7_000_000_000, "Q6_K")) == 1.6


def test_heavier_quant_reduces_estimated_speed():
    from backend.core.performance_estimator import estimate_speed
    from backend.core.resource_monitor import ResourceSnapshot
    from backend.core.safety_profiles import PerformanceProfile

    snapshot = ResourceSnapshot(cpu_usage=10.0, ram_used_gb=8.0, ram_total_gb=32.0,
                                 vram_used_gb=0.0, vram_total_gb=0.0, unity_running=False)
    profile = PerformanceProfile(name="test", n_threads=8, n_gpu_layers=0, max_ctx=4096)

    fast_cfg = _model_cfg("q4-model", 7_000_000_000, "Q4_K_M")
    slow_cfg = _model_cfg("q6-model", 7_000_000_000, "Q6_K")

    fast_speed = estimate_speed(snapshot, fast_cfg, profile)
    slow_speed = estimate_speed(snapshot, slow_cfg, profile)

    assert slow_speed < fast_speed, f"Q6_K ({slow_speed}) should be slower than Q4_K_M ({fast_speed})"


# ============================================================
# PART 4 — size-tiered requirement thresholds (model_size_requirements.py)
# ============================================================
def test_12b_requirement_thresholds():
    from backend.core.model_size_requirements import get_requirements_for_params

    req = get_requirements_for_params(12_000_000_000)
    assert req["minRamGB"] == 12 and req["recRamGB"] == 24
    assert req["minCpuCores"] == 4 and req["recCpuCores"] == 8
    assert "AVX2" in req["minCpuFeatures"]
    assert req["recAvx2Tier"] == "High"


def test_30b_requirement_thresholds():
    from backend.core.model_size_requirements import get_requirements_for_params

    req = get_requirements_for_params(30_000_000_000)
    assert req["minRamGB"] == 24 and req["recRamGB"] == 32
    assert req["minCpuCores"] == 8 and req["recCpuCores"] == 12
    assert req["recAvx2Tier"] == "High"


def test_70b_requirement_thresholds():
    from backend.core.model_size_requirements import get_requirements_for_params

    req = get_requirements_for_params(70_000_000_000)
    assert req["minRamGB"] == 48 and req["recRamGB"] == 64
    assert req["minCpuCores"] == 12 and req["recCpuCores"] == 16
    assert req["recVramGB"] == 12


def test_405b_requirement_thresholds():
    from backend.core.model_size_requirements import get_requirements_for_params

    req = get_requirements_for_params(405_000_000_000)
    assert req["minRamGB"] == 64
    assert req["recRamGB"] == 96
    assert req["recVramGB"] == 16
    assert req["minStorageType"] == "NVMe"
    assert req["recStorageType"] == "NVMe Ultra"


# ============================================================
# PART 5 — compatibility_checker.py: physical cores, exceeds_recommended,
# and size-table fallback for models without a hand-authored block.
# ============================================================
def test_compat_uses_physical_cores_not_logical_threads():
    from backend.core.compatibility_checker import check_requirements
    from backend.core.resource_monitor import ResourceSnapshot

    # 6 physical / 12 logical — a requirement of 8 cores must fail even
    # though logical thread count (12) would satisfy it. This is only
    # verifiable end-to-end through detect_cpu(), so this test asserts
    # against the real machine's physical core count directly.
    import psutil
    real_physical = psutil.cpu_count(logical=False) or 1

    snapshot = ResourceSnapshot(cpu_usage=0, ram_used_gb=0, ram_total_gb=64,
                                 vram_used_gb=0, vram_total_gb=0, unity_running=False)
    model_cfg = {
        "id": "core-check-model",
        "requirements": {
            "minRamGB": 1, "recRamGB": 1,
            "minCpuCores": real_physical + 1,  # always one more than what's physically present
            "recCpuCores": real_physical + 1,
            "minCpuFeatures": [], "recCpuFeatures": [],
            "minVramGB": 0, "recVramGB": 0,
        },
    }

    compat = check_requirements(snapshot, model_cfg)
    assert compat["checks"]["cpu_cores"]["value"] == real_physical
    assert compat["checks"]["cpu_cores"]["pass_min"] is False


def test_compat_falls_back_to_size_table_when_no_requirements_block():
    from backend.core.compatibility_checker import check_requirements
    from backend.core.resource_monitor import ResourceSnapshot

    snapshot = ResourceSnapshot(cpu_usage=0, ram_used_gb=0, ram_total_gb=64,
                                 vram_used_gb=0, vram_total_gb=0, unity_running=False)
    model_cfg = {"id": "no-requirements-model", "params": 70_000_000_000}

    compat = check_requirements(snapshot, model_cfg)
    assert compat["checks"]["ram"]["min"] == 48
    assert compat["checks"]["ram"]["rec"] == 64


def test_exceeds_recommended_true_only_when_strictly_above_every_rec_value():
    from backend.core.compatibility_checker import check_requirements
    from backend.core.resource_monitor import ResourceSnapshot
    import psutil

    real_physical = psutil.cpu_count(logical=False) or 1

    exactly_rec_snapshot = ResourceSnapshot(cpu_usage=0, ram_used_gb=0, ram_total_gb=32,
                                             vram_used_gb=0, vram_total_gb=0, unity_running=False)
    generous_snapshot = ResourceSnapshot(cpu_usage=0, ram_used_gb=0, ram_total_gb=256,
                                          vram_used_gb=0, vram_total_gb=0, unity_running=False)
    model_cfg = {
        "id": "exceeds-check-model",
        "requirements": {
            "minRamGB": 8, "recRamGB": 32,
            "minCpuCores": 1, "recCpuCores": 1,  # trivially satisfied/exceeded by any real machine
            "minCpuFeatures": [], "recCpuFeatures": [],
            "minVramGB": 0, "recVramGB": 0,
        },
    }

    exactly_rec = check_requirements(exactly_rec_snapshot, model_cfg)
    generous = check_requirements(generous_snapshot, model_cfg)

    assert exactly_rec["meets_recommended"] is True
    assert exactly_rec["exceeds_recommended"] is False, "exactly at the rec RAM value must not count as exceeding it"
    assert generous["exceeds_recommended"] is True


# ============================================================
# PART 6 — Section 6: performance tiers on named real-world systems
# ============================================================
def test_high_end_system_performance_tiers():
    from backend.core.performance_tiers import estimate_performance_tier

    cpu, ram, gpu, storage = _high_end_system()

    result_12b = estimate_performance_tier(cpu, ram, gpu, storage, _model_cfg("t-12b", 12_000_000_000))
    result_30b = estimate_performance_tier(cpu, ram, gpu, storage, _model_cfg("t-30b", 30_000_000_000))
    result_70b = estimate_performance_tier(cpu, ram, gpu, storage, _model_cfg("t-70b", 70_000_000_000))
    result_405b = estimate_performance_tier(cpu, ram, gpu, storage, _model_cfg("t-405b", 405_000_000_000))

    assert result_12b["tier"] == "Excellent", result_12b
    assert result_30b["tier"] == "Great", result_30b
    assert result_70b["tier"] == "Good", result_70b
    assert result_405b["tier"] == "Usable", result_405b

    assert result_12b["mode"] == "split_load"
    assert result_30b["mode"] == "offload"
    assert result_70b["mode"] == "offload"
    assert result_405b["mode"] == "offload"


def test_mid_range_system_performance_tiers():
    from backend.core.performance_tiers import estimate_performance_tier

    cpu, ram, gpu, storage = _mid_range_system()

    result_12b = estimate_performance_tier(cpu, ram, gpu, storage, _model_cfg("t-12b", 12_000_000_000))
    result_30b = estimate_performance_tier(cpu, ram, gpu, storage, _model_cfg("t-30b", 30_000_000_000))
    result_70b = estimate_performance_tier(cpu, ram, gpu, storage, _model_cfg("t-70b", 70_000_000_000))

    assert result_12b["tier"] == "Great", result_12b
    assert result_30b["tier"] == "Good", result_30b
    assert result_70b["tier"] == "Poor", result_70b

    assert result_12b["mode"] == "split_load"
    assert result_30b["mode"] == "offload"
    assert result_70b["mode"] == "offload"


def test_low_end_system_performance_tiers():
    from backend.core.performance_tiers import estimate_performance_tier

    cpu, ram, gpu, storage = _low_end_system()

    result_7b = estimate_performance_tier(cpu, ram, gpu, storage, _model_cfg("t-7b", 7_000_000_000))
    result_12b = estimate_performance_tier(cpu, ram, gpu, storage, _model_cfg("t-12b", 12_000_000_000))

    assert result_7b["tier"] == "Good", result_7b
    assert result_12b["tier"] == "Poor", result_12b

    assert result_7b["mode"] == "cpu_only"
    assert result_12b["mode"] == "cpu_only"


def test_performance_tier_ordering_is_monotonic_with_model_size():
    """Same hardware, bigger model → same or worse tier, never better."""
    from backend.core.performance_tiers import estimate_performance_tier, TIER_ORDER

    cpu, ram, gpu, storage = _high_end_system()
    sizes = [12_000_000_000, 30_000_000_000, 70_000_000_000, 405_000_000_000]
    tiers = [
        estimate_performance_tier(cpu, ram, gpu, storage, _model_cfg(f"t-{s}", s))["tier"]
        for s in sizes
    ]
    ranks = [TIER_ORDER.index(t) for t in tiers]
    assert ranks == sorted(ranks), f"tiers must degrade monotonically as model size grows: {tiers}"


# ============================================================
# RUNNER
# ============================================================
def _all_tests():
    return [obj for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]


def main() -> int:
    failures = []
    for test in _all_tests():
        name = test.__name__
        try:
            test()
            print(f"PASS  {name}")
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            failures.append(name)
        except Exception as e:
            print(f"ERROR {name}: {e}")
            traceback.print_exc()
            failures.append(name)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1

    print("All tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
