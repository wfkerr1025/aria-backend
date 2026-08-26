# backend/tests/safety_projection_tests.py
#
# Regression tests for the safety/projection engine fix:
#   - RAM projection includes the fixed IPC runtime overhead constant
#     exactly once (no double-counting)
#   - CPU projection scales down with GPU-layer offload instead of
#     ignoring it
#   - evaluate_safety()'s three severity tiers (ok / caution / block)
#     fire at the right thresholds, including the "fails minimum
#     requirements" hard-block case
#   - sanity bounds (0-100%, positive speed) across small/medium/large
#     synthetic models using the REAL resource snapshot, matching the
#     existing test suite's tolerance for real-machine-dependent numbers
#     (see backend/tests/skr_and_ipc_tests.py's
#     test_safety_check_still_fires_without_skip_flag)
#
# Self-contained plain-assert tests, matching backend/tests/skr_and_ipc_tests.py
# — not pytest. Run directly:
#
#   python backend/tests/safety_projection_tests.py

from __future__ import annotations

import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _snapshot(cpu_usage=20.0, ram_used_gb=8.0, ram_total_gb=32.0,
              vram_used_gb=0.0, vram_total_gb=0.0, unity_running=False):
    from backend.core.resource_monitor import ResourceSnapshot
    return ResourceSnapshot(
        cpu_usage=cpu_usage, ram_used_gb=ram_used_gb, ram_total_gb=ram_total_gb,
        vram_used_gb=vram_used_gb, vram_total_gb=vram_total_gb, unity_running=unity_running,
    )


def _model_cfg(model_id="test-model", params=7_000_000_000, quant="Q4_K_M",
                min_ram_gb=4, rec_ram_gb=8, min_vram_gb=0, rec_vram_gb=0,
                min_cores=1, rec_cores=1, default_gpu_layers=0, max_context=4096,
                rec_speed=None):
    requirements = {
        "minRamGB": min_ram_gb, "recRamGB": rec_ram_gb,
        "minVramGB": min_vram_gb, "recVramGB": rec_vram_gb,
        "minCpuCores": min_cores, "recCpuCores": rec_cores,
        "minCpuFeatures": [], "recCpuFeatures": [],
        "difficulty": "Test",
    }
    if rec_speed is not None:
        requirements["recSpeedTokSec"] = rec_speed
    return {
        "id": model_id, "params": params, "quant": quant,
        # get_model_metadata.get_quant_bytes() only exact-matches bare
        # "Q4_K"/"Q5_K"/etc keys — real quant strings like "Q4_K_M" never
        # exact-match, so it resolves via substring-in-filename instead
        # (see backend/core/model_metadata.py). Setting this mirrors how
        # real models.json entries actually resolve, rather than silently
        # falling through to the Q5_K fallback regardless of `quant`.
        "defaultFilename": f"{model_id}.{quant}.gguf",
        "defaultGpuLayers": default_gpu_layers, "maxContext": max_context,
        "requirements": requirements,
    }


def _with_patched_snapshot(snapshot, fn):
    """
    evaluate_safety() calls get_resource_snapshot() internally rather
    than accepting one as a parameter — monkeypatch it at module scope
    for the duration of fn(), same pattern
    backend/tests/skr_and_ipc_tests.py already uses for
    backend.websocket.handlers.evaluate_safety.
    """
    from backend.core import safety_manager

    original = safety_manager.get_resource_snapshot
    safety_manager.get_resource_snapshot = lambda: snapshot
    try:
        return fn()
    finally:
        safety_manager.get_resource_snapshot = original


# ============================================================
# PART 1 — RAM projection includes IPC overhead exactly once
# ============================================================
def test_ram_projection_includes_ipc_overhead_once():
    from backend.core import safety_manager
    from backend.core.safety_profiles import PerformanceProfile

    snapshot = _snapshot(ram_used_gb=8.0, ram_total_gb=32.0)
    # n_gpu_layers=0 → gpu_layer_ratio=0 → ram_model_gb uses the full
    # model size, isolating the overhead constant's contribution.
    profile = PerformanceProfile(name="test", n_threads=4, n_gpu_layers=0, max_ctx=4096)
    model_cfg = _model_cfg(params=7_000_000_000, quant="Q4_K_M")

    projected_pct = safety_manager.estimate_ram_usage(snapshot, profile, model_cfg)

    params = 7_000_000_000
    bytes_per_param = 1.0  # Q4_K substring-matches in get_quant_bytes via "Q4_K_M"
    ram_model_gb = (params * bytes_per_param) / (1024 ** 3)
    ram_ctx_gb = 0.000015 * 4096
    expected_gb = 8.0 + ram_model_gb + ram_ctx_gb + safety_manager.IPC_RUNTIME_OVERHEAD_RAM_GB
    expected_pct = (expected_gb / 32.0) * 100

    assert abs(projected_pct - expected_pct) < 0.01, f"expected {expected_pct:.4f}%, got {projected_pct:.4f}%"

    # And exactly once: removing the constant from the expected value
    # must NOT also match (guards against someone later adding it twice).
    without_overhead_gb = 8.0 + ram_model_gb + ram_ctx_gb
    without_overhead_pct = (without_overhead_gb / 32.0) * 100
    assert abs(projected_pct - without_overhead_pct) > 0.001, "overhead constant does not appear to be applied at all"


# ============================================================
# PART 2 — CPU projection scales down with GPU offload
# ============================================================
def test_cpu_projection_ignores_nothing_when_fully_cpu_resident():
    from backend.core import safety_manager
    from backend.core.safety_profiles import PerformanceProfile

    snapshot = _snapshot(cpu_usage=10.0)
    profile = PerformanceProfile(name="test", n_threads=8, n_gpu_layers=0, max_ctx=4096)
    model_cfg = _model_cfg()

    projected = safety_manager.estimate_cpu_usage(snapshot, profile, model_cfg)
    assert abs(projected - (10.0 + 8 * 4)) < 0.01, f"expected {10.0 + 8*4}%, got {projected}%"


def test_cpu_projection_drops_sharply_when_fully_gpu_offloaded():
    from backend.core import safety_manager
    from backend.core.safety_profiles import PerformanceProfile

    snapshot = _snapshot(cpu_usage=10.0)
    # All 80 layers on GPU — cpu_layer_ratio should be ~0.
    profile = PerformanceProfile(name="test", n_threads=8, n_gpu_layers=80, max_ctx=4096)
    model_cfg = _model_cfg()

    projected = safety_manager.estimate_cpu_usage(snapshot, profile, model_cfg)
    assert abs(projected - 10.0) < 0.01, f"expected ~10% (just baseline usage), got {projected}%"


def test_cpu_projection_partial_offload_is_between_the_two_extremes():
    from backend.core import safety_manager
    from backend.core.safety_profiles import PerformanceProfile

    snapshot = _snapshot(cpu_usage=10.0)
    profile_none = PerformanceProfile(name="test", n_threads=8, n_gpu_layers=0, max_ctx=4096)
    profile_half = PerformanceProfile(name="test", n_threads=8, n_gpu_layers=40, max_ctx=4096)
    model_cfg = _model_cfg()

    cpu_none = safety_manager.estimate_cpu_usage(snapshot, profile_none, model_cfg)
    cpu_half = safety_manager.estimate_cpu_usage(snapshot, profile_half, model_cfg)

    assert 10.0 < cpu_half < cpu_none, f"expected 10.0 < {cpu_half} < {cpu_none}"


# ============================================================
# PART 3 — evaluate_safety() three-tier severity
# ============================================================
def test_severity_ok_for_ample_resources():
    from backend.core.safety_manager import evaluate_safety

    # 64GB RAM, barely used, no GPU, tiny model — should be comfortably OK.
    snapshot = _snapshot(cpu_usage=5.0, ram_used_gb=4.0, ram_total_gb=64.0)
    model_cfg = _model_cfg(model_id="tiny-1b", params=1_000_000_000, quant="Q4_K_M",
                            min_ram_gb=2, rec_ram_gb=4)

    decision = _with_patched_snapshot(snapshot, lambda: evaluate_safety(model_cfg))

    assert decision.severity == "ok", f"expected ok, got {decision.severity} (msg={decision.message})"
    assert decision.requires_warning is False
    assert decision.safe_to_run is True


def test_severity_caution_between_caution_and_block_thresholds():
    from backend.core.safety_manager import evaluate_safety, CAUTION_RAM_PCT, BLOCK_RAM_PCT

    # Construct a snapshot where baseline usage alone already sits between
    # the caution and block RAM thresholds, so a model with a small
    # additional footprint lands in "caution" without crossing "block".
    ram_total = 32.0
    ram_used = ram_total * ((CAUTION_RAM_PCT + 5) / 100.0)  # a few % above caution
    snapshot = _snapshot(cpu_usage=5.0, ram_used_gb=ram_used, ram_total_gb=ram_total)
    model_cfg = _model_cfg(model_id="small-model", params=1_000_000_000, quant="Q4_K_M",
                            min_ram_gb=2, rec_ram_gb=4)

    decision = _with_patched_snapshot(snapshot, lambda: evaluate_safety(model_cfg))

    assert decision.severity == "caution", f"expected caution, got {decision.severity} (msg={decision.message})"
    assert decision.requires_warning is True
    assert decision.safe_to_run is True, "caution must still be safe_to_run — only block is not"
    assert decision.projected_ram_pct < BLOCK_RAM_PCT


def test_severity_block_when_projected_usage_would_exhaust_ram():
    from backend.core.safety_manager import evaluate_safety, BLOCK_RAM_PCT

    ram_total = 16.0
    ram_used = ram_total * 0.85  # already high baseline
    snapshot = _snapshot(cpu_usage=5.0, ram_used_gb=ram_used, ram_total_gb=ram_total)
    # A large model on top of already-high baseline usage should push
    # projected RAM past the block ceiling.
    model_cfg = _model_cfg(model_id="huge-model", params=30_000_000_000, quant="Q6_K",
                            min_ram_gb=2, rec_ram_gb=4)

    decision = _with_patched_snapshot(snapshot, lambda: evaluate_safety(model_cfg))

    assert decision.projected_ram_pct >= BLOCK_RAM_PCT, f"test setup didn't actually cross the block line: {decision.projected_ram_pct}%"
    assert decision.severity == "block", f"expected block, got {decision.severity} (msg={decision.message})"
    assert decision.requires_warning is True
    assert decision.safe_to_run is False


def test_severity_block_when_fails_minimum_requirements():
    from backend.core.safety_manager import evaluate_safety

    # Plenty of RAM overall, but the model demands far more than exists —
    # meets_minimum must fail regardless of current usage being low.
    snapshot = _snapshot(cpu_usage=5.0, ram_used_gb=1.0, ram_total_gb=8.0)
    model_cfg = _model_cfg(model_id="impossible-model", params=70_000_000_000, quant="Q6_K",
                            min_ram_gb=64, rec_ram_gb=128)

    decision = _with_patched_snapshot(snapshot, lambda: evaluate_safety(model_cfg))

    assert decision.severity == "block", f"expected block, got {decision.severity}"
    assert decision.safe_to_run is False
    assert decision.requires_warning is True
    assert "minimum requirements" in decision.message


# ============================================================
# PART 4 — sanity bounds across model classes, real machine snapshot
# (tolerant of the real machine's actual resource state, like
# skr_and_ipc_tests.py's test_safety_check_still_fires_without_skip_flag)
# ============================================================
def test_sanity_bounds_small_medium_large_models_real_snapshot():
    from backend.core.safety_manager import evaluate_safety

    models = [
        _model_cfg(model_id="sanity-small-1b", params=1_000_000_000, quant="Q4_K_M", min_ram_gb=2, rec_ram_gb=4, rec_speed=15),
        _model_cfg(model_id="sanity-medium-7b", params=7_000_000_000, quant="Q4_K_M", min_ram_gb=8, rec_ram_gb=16, rec_speed=8),
        _model_cfg(model_id="sanity-large-30b", params=30_000_000_000, quant="Q5_K_M", min_ram_gb=32, rec_ram_gb=64, rec_speed=3),
    ]

    for model_cfg in models:
        decision = evaluate_safety(model_cfg)

        assert decision.severity in ("ok", "caution", "block"), f"{model_cfg['id']}: unexpected severity {decision.severity!r}"
        assert 0.0 <= decision.projected_cpu_pct <= 100.0, f"{model_cfg['id']}: CPU {decision.projected_cpu_pct}% out of bounds"
        assert decision.projected_ram_pct >= 0.0, f"{model_cfg['id']}: RAM {decision.projected_ram_pct}% negative"
        assert decision.projected_vram_pct >= 0.0, f"{model_cfg['id']}: VRAM {decision.projected_vram_pct}% negative"
        # A block decision zeroes projected_speed_toksec by design (fails
        # minimum requirements short-circuits before estimate_speed runs).
        if decision.severity != "block":
            assert decision.projected_speed_toksec > 0.0, f"{model_cfg['id']}: speed {decision.projected_speed_toksec} should be positive"
        assert isinstance(decision.message, str) and len(decision.message) > 0


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
