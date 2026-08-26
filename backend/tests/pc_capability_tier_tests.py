# backend/tests/pc_capability_tier_tests.py
#
# Regression tests for the universal Tier 0-6 PC-capability system:
#   - backend/core/pc_capability_tier.py's get_pc_tier() ladder, driven
#     with literal, named hardware specs (mirrors backend/tests/
#     hardware_and_performance_tests.py's own pattern of testing
#     classification logic without needing that exact physical machine)
#   - backend/core/model_size_requirements.py's new 20B/140B buckets
#   - backend/core/compatibility_checker.py's derived "verdict" field
#
# Self-contained plain-assert tests, matching backend/tests/auto_balancer_tests.py
# — not pytest. Run directly:
#
#   python backend/tests/pc_capability_tier_tests.py

from __future__ import annotations

import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _hw(ram_gb=8.0, cores=4, avx2=True, vram_gb=0.0):
    return {
        "cpu": {"physical_cores": cores, "logical_threads": cores * 2, "avx2": avx2, "avx512": False},
        "ram": {"total_gb": ram_gb},
        "gpu": ({"vram_total_gb": vram_gb, "vram_used_gb": 0.0} if vram_gb > 0 else None),
    }


# ============================================================
# PART 1 — get_pc_tier() ladder
# ============================================================
def test_tiny_machine_is_tier_0():
    from backend.core.pc_capability_tier import get_pc_tier

    result = get_pc_tier(_hw(ram_gb=2.0, cores=1, avx2=False))
    assert result["tier"] == 0
    assert result["enabled_size_classes"] == ["0.5B"]


def test_modest_machine_reaches_tier_1_phi3():
    from backend.core.pc_capability_tier import get_pc_tier

    result = get_pc_tier(_hw(ram_gb=4.0, cores=2, avx2=False))
    assert result["tier"] == 1
    assert "Phi-3" in result["enabled_size_classes"]


def test_7b_capable_machine_reaches_tier_2():
    from backend.core.pc_capability_tier import get_pc_tier

    result = get_pc_tier(_hw(ram_gb=8.0, cores=4, avx2=True))
    assert result["tier"] == 2
    assert "7B" in result["enabled_size_classes"]


def test_12b_capable_machine_reaches_tier_3():
    from backend.core.pc_capability_tier import get_pc_tier

    result = get_pc_tier(_hw(ram_gb=16.0, cores=8, avx2=True))
    assert result["tier"] == 3
    assert "12B" in result["enabled_size_classes"]


def test_large_ram_without_gpu_is_capped_at_tier_3():
    # Meets 20B/30B RAM+core minimums (both have minVramGB=0) but has no
    # GPU — Tier 4+ explicitly requires one per the spec's own "(GPU)"
    # tier labels, so this must NOT climb past Tier 3.
    from backend.core.pc_capability_tier import get_pc_tier

    result = get_pc_tier(_hw(ram_gb=64.0, cores=16, avx2=True, vram_gb=0.0))
    assert result["tier"] == 3, f"expected capped at Tier 3 without a GPU, got {result['tier']}"


def test_gpu_machine_meeting_30b_minimums_reaches_tier_4():
    from backend.core.pc_capability_tier import get_pc_tier

    result = get_pc_tier(_hw(ram_gb=40.0, cores=12, avx2=True, vram_gb=8.0))
    assert result["tier"] == 4
    assert "30B" in result["enabled_size_classes"]


def test_high_vram_machine_meeting_70b_minimums_reaches_tier_5():
    from backend.core.pc_capability_tier import get_pc_tier
    from backend.core.model_size_requirements import get_requirements_for_size_tier

    req_70b = get_requirements_for_size_tier("70B")
    result = get_pc_tier(_hw(ram_gb=64.0, cores=16, avx2=True, vram_gb=req_70b["recVramGB"]))
    assert result["tier"] == 5
    assert "70B" in result["enabled_size_classes"]


def test_extreme_machine_meeting_140b_minimums_reaches_tier_6():
    from backend.core.pc_capability_tier import get_pc_tier
    from backend.core.model_size_requirements import get_requirements_for_size_tier

    req_140b = get_requirements_for_size_tier("140B")
    result = get_pc_tier(_hw(ram_gb=112.0, cores=20, avx2=True, vram_gb=req_140b["recVramGB"]))
    assert result["tier"] == 6
    assert "140B" in result["enabled_size_classes"]


def test_tiers_climb_in_strict_order_never_skipping():
    # A machine with huge RAM/cores but only just enough for Tier 2's
    # AVX2 requirement and nothing above must not jump straight to a
    # higher tier just because RAM alone would qualify.
    from backend.core.pc_capability_tier import get_pc_tier

    result = get_pc_tier(_hw(ram_gb=8.0, cores=4, avx2=True, vram_gb=0.0))
    assert result["tier"] == 2


# ============================================================
# PART 2 — model_size_requirements.py's new 20B/140B buckets
# ============================================================
def test_20b_bucket_is_distinct_from_12b_and_30b():
    from backend.core.model_size_requirements import get_requirements_for_size_tier

    req_12b = get_requirements_for_size_tier("12B")
    req_20b = get_requirements_for_size_tier("20B")
    req_30b = get_requirements_for_size_tier("30B")

    assert req_20b is not None
    assert req_12b["minRamGB"] < req_20b["minRamGB"] < req_30b["minRamGB"]


def test_140b_bucket_is_distinct_from_70b_and_405b():
    from backend.core.model_size_requirements import get_requirements_for_size_tier

    req_70b = get_requirements_for_size_tier("70B")
    req_140b = get_requirements_for_size_tier("140B")
    req_405b = get_requirements_for_size_tier("405B")

    assert req_140b is not None
    assert req_70b["minRamGB"] < req_140b["minRamGB"]
    assert req_140b["recVramGB"] > req_70b["recVramGB"]
    assert req_405b is not None


def test_a_20b_param_model_lands_in_the_20b_bucket_not_30b():
    from backend.core.model_size_requirements import get_requirements_for_params, get_requirements_for_size_tier

    result = get_requirements_for_params(20_000_000_000)
    expected = get_requirements_for_size_tier("20B")
    assert result["minRamGB"] == expected["minRamGB"], "a 20B model must use the 20B bucket, not silently share 30B's"


def test_a_140b_param_model_lands_in_the_140b_bucket_not_405b():
    from backend.core.model_size_requirements import get_requirements_for_params, get_requirements_for_size_tier

    result = get_requirements_for_params(140_000_000_000)
    expected = get_requirements_for_size_tier("140B")
    assert result["minRamGB"] == expected["minRamGB"], "a 140B model must use the 140B bucket, not the 405B catch-all"


# ============================================================
# PART 3 — compatibility_checker.py's derived "verdict" field
# ============================================================
def _snapshot(ram_total_gb=32.0, vram_total_gb=0.0):
    from backend.core.resource_monitor import ResourceSnapshot
    return ResourceSnapshot(cpu_usage=10.0, ram_used_gb=4.0, ram_total_gb=ram_total_gb, vram_used_gb=0.0, vram_total_gb=vram_total_gb, unity_running=False)


def test_verdict_is_block_when_minimum_requirements_are_not_met():
    from backend.core.compatibility_checker import check_requirements

    model_cfg = {"id": "huge", "provider": "local", "params": 405_000_000_000_000}
    result = check_requirements(_snapshot(ram_total_gb=4.0), model_cfg)
    assert result["meets_minimum"] is False
    assert result["verdict"] == "block"


def test_verdict_is_allow_when_recommended_requirements_are_met():
    from backend.core.compatibility_checker import check_requirements

    model_cfg = {"id": "tiny", "provider": "local", "params": 500_000_000, "requirements": {
        "minRamGB": 1, "recRamGB": 1, "minCpuCores": 1, "recCpuCores": 1,
        "minCpuFeatures": [], "recCpuFeatures": [], "minVramGB": 0, "recVramGB": 0,
    }}
    result = check_requirements(_snapshot(ram_total_gb=32.0), model_cfg)
    assert result["meets_recommended"] is True
    assert result["verdict"] == "allow"


def test_verdict_is_warn_when_minimum_met_but_not_recommended():
    from backend.core.compatibility_checker import check_requirements

    model_cfg = {"id": "borderline", "provider": "local", "params": 500_000_000, "requirements": {
        "minRamGB": 4, "recRamGB": 64, "minCpuCores": 1, "recCpuCores": 1,
        "minCpuFeatures": [], "recCpuFeatures": [], "minVramGB": 0, "recVramGB": 0,
    }}
    result = check_requirements(_snapshot(ram_total_gb=8.0), model_cfg)
    assert result["meets_minimum"] is True
    assert result["meets_recommended"] is False
    assert result["verdict"] == "warn"


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
