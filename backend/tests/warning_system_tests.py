# backend/tests/warning_system_tests.py
#
# Regression tests for the session-scoped + risk-scoped warning system:
#   - backend/core/warning_manager.py's once-per-session "normal"
#     warnings vs. always-fires "critical" warnings
#   - backend/core/runtime_health_monitor.py's sustained-CPU tracking
#     (unit-level; the poll thread itself isn't exercised here)
#   - backend/core/hardware_detector.py's detect_thermal() honest
#     "unsupported" degradation
#
# Self-contained plain-assert tests, matching backend/tests/auto_balancer_tests.py
# — not pytest. Run directly:
#
#   python backend/tests/warning_system_tests.py

from __future__ import annotations

import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _decision(severity="ok"):
    from backend.core.safety_manager import SafetyDecision
    return SafetyDecision(
        safe_to_run=(severity != "block"),
        requires_warning=(severity != "ok"),
        severity=severity,
        message="test",
        profile=None,
        snapshot=None,
    )


def _model_cfg(model_id="test-model"):
    return {"id": model_id, "provider": "local"}


# ============================================================
# PART 1 — normal warnings: once per session
# ============================================================
def test_normal_warnings_fire_once_per_session():
    from backend.core import warning_manager as wm

    state = wm.new_session_state()
    cfg = _model_cfg()

    first = wm.evaluate_warnings(state, cfg, safety_decision=_decision("caution"))
    ids_first = {e.id for e in first}
    assert wm.WARNING_HEAVY_MODEL in ids_first
    assert wm.WARNING_PERFORMANCE_REDUCED in ids_first

    second = wm.evaluate_warnings(state, cfg, safety_decision=_decision("caution"))
    assert second == [], f"normal warnings must not repeat within the same session, got {second}"


def test_normal_warnings_are_per_session_state_not_global():
    from backend.core import warning_manager as wm

    cfg = _model_cfg()
    state_a = wm.new_session_state()
    state_b = wm.new_session_state()

    wm.evaluate_warnings(state_a, cfg, safety_decision=_decision("caution"))
    second_connection = wm.evaluate_warnings(state_b, cfg, safety_decision=_decision("caution"))
    ids = {e.id for e in second_connection}
    assert wm.WARNING_HEAVY_MODEL in ids, "a fresh session_state must not inherit another connection's dedup history"


def test_auto_balance_active_warning_fires_once_when_tier_above_zero():
    from backend.core import warning_manager as wm

    state = wm.new_session_state()
    cfg = _model_cfg()
    balancer_snapshot = {"tier": 1}

    first = wm.evaluate_warnings(state, cfg, balancer_snapshot=balancer_snapshot)
    assert any(e.id == wm.WARNING_AUTO_BALANCE_ACTIVE for e in first)

    second = wm.evaluate_warnings(state, cfg, balancer_snapshot=balancer_snapshot)
    assert not any(e.id == wm.WARNING_AUTO_BALANCE_ACTIVE for e in second)


def test_no_normal_warnings_when_severity_is_ok_and_balancer_idle():
    from backend.core import warning_manager as wm

    state = wm.new_session_state()
    cfg = _model_cfg()
    events = wm.evaluate_warnings(state, cfg, safety_decision=_decision("ok"), balancer_snapshot={"tier": 0})
    assert events == []


# ============================================================
# PART 2 — critical warnings: always fire, override the once-per-session rule
# ============================================================
def test_cpu_sustained_critical_warning_always_fires():
    from backend.core import warning_manager as wm

    state = wm.new_session_state()
    cfg = _model_cfg()

    first = wm.evaluate_warnings(state, cfg, cpu_sustained_critical=True)
    second = wm.evaluate_warnings(state, cfg, cpu_sustained_critical=True)

    assert any(e.id == wm.WARNING_CPU_SUSTAINED and e.level == wm.LEVEL_CRITICAL for e in first)
    assert any(e.id == wm.WARNING_CPU_SUSTAINED for e in second), \
        "critical warnings must re-fire every time, unlike normal warnings"


def test_ram_critical_warning_fires_above_threshold():
    from backend.core import warning_manager as wm
    from backend.core.resource_monitor import ResourceSnapshot

    state = wm.new_session_state()
    cfg = _model_cfg()
    snapshot = ResourceSnapshot(cpu_usage=10.0, ram_used_gb=29.0, ram_total_gb=32.0, vram_used_gb=0.0, vram_total_gb=0.0, unity_running=False)

    events = wm.evaluate_warnings(state, cfg, resource_snapshot=snapshot)
    assert any(e.id == wm.WARNING_RAM_CRITICAL for e in events)


def test_ram_below_threshold_does_not_fire():
    from backend.core import warning_manager as wm
    from backend.core.resource_monitor import ResourceSnapshot

    state = wm.new_session_state()
    cfg = _model_cfg()
    snapshot = ResourceSnapshot(cpu_usage=10.0, ram_used_gb=8.0, ram_total_gb=32.0, vram_used_gb=0.0, vram_total_gb=0.0, unity_running=False)

    events = wm.evaluate_warnings(state, cfg, resource_snapshot=snapshot)
    assert not any(e.id == wm.WARNING_RAM_CRITICAL for e in events)


def test_thermal_throttling_critical_warning():
    from backend.core import warning_manager as wm

    state = wm.new_session_state()
    cfg = _model_cfg()
    thermal = {"supported": True, "throttling_detected": True, "max_temp_c": 99.0, "tier": "Throttling"}

    events = wm.evaluate_warnings(state, cfg, thermal=thermal)
    assert any(e.id == wm.WARNING_THERMAL_THROTTLING for e in events)


def test_thermal_unsupported_never_fires_a_warning():
    from backend.core import warning_manager as wm

    state = wm.new_session_state()
    cfg = _model_cfg()
    thermal = {"supported": False, "throttling_detected": False, "max_temp_c": None, "tier": "Unknown"}

    events = wm.evaluate_warnings(state, cfg, thermal=thermal)
    assert not any(e.id == wm.WARNING_THERMAL_THROTTLING for e in events)


def test_balancer_maxed_out_and_still_unsafe_fires_critical():
    from backend.core import warning_manager as wm
    from backend.core import auto_balancer

    state = wm.new_session_state()
    cfg = _model_cfg()
    balancer_snapshot = {"tier": auto_balancer.TIER_QUANT}

    events = wm.evaluate_warnings(state, cfg, safety_decision=_decision("block"), balancer_snapshot=balancer_snapshot)
    assert any(e.id == wm.WARNING_BALANCER_MAXED for e in events)


def test_balancer_maxed_out_but_safe_does_not_fire():
    from backend.core import warning_manager as wm
    from backend.core import auto_balancer

    state = wm.new_session_state()
    cfg = _model_cfg()
    balancer_snapshot = {"tier": auto_balancer.TIER_QUANT}

    events = wm.evaluate_warnings(state, cfg, safety_decision=_decision("ok"), balancer_snapshot=balancer_snapshot)
    assert not any(e.id == wm.WARNING_BALANCER_MAXED for e in events)


def test_smallest_model_unsafe_fires_only_for_the_emergency_model():
    from backend.core import warning_manager as wm

    original = wm.get_emergency_model_id
    wm.get_emergency_model_id = lambda: "tiny-emergency-model"
    try:
        state = wm.new_session_state()
        events = wm.evaluate_warnings(state, _model_cfg("tiny-emergency-model"), safety_decision=_decision("block"))
        assert any(e.id == wm.WARNING_SMALLEST_MODEL_UNSAFE for e in events)

        state2 = wm.new_session_state()
        events2 = wm.evaluate_warnings(state2, _model_cfg("some-other-model"), safety_decision=_decision("block"))
        assert not any(e.id == wm.WARNING_SMALLEST_MODEL_UNSAFE for e in events2)
    finally:
        wm.get_emergency_model_id = original


# ============================================================
# PART 3 — every event names the ACTIVE model, never a different one
# ============================================================
def test_events_always_carry_the_active_model_id():
    from backend.core import warning_manager as wm

    state = wm.new_session_state()
    cfg = _model_cfg("active-model-under-test")
    events = wm.evaluate_warnings(state, cfg, safety_decision=_decision("caution"), cpu_sustained_critical=True)

    assert events, "expected at least one event for this scenario"
    for e in events:
        assert e.model_id == "active-model-under-test"


# ============================================================
# PART 4 — hardware_detector.detect_thermal() honest degradation
# ============================================================
def test_detect_thermal_never_raises_and_has_expected_shape():
    from backend.core import hardware_detector

    result = hardware_detector.detect_thermal()
    assert "supported" in result and "throttling_detected" in result and "tier" in result
    if not result["supported"]:
        assert result["tier"] == "Unknown"
        assert result["throttling_detected"] is False


def test_classify_thermal_unknown_tier_when_unsupported():
    from backend.core import hardware_detector

    result = hardware_detector.classify_thermal(supported=False, throttling_detected=False, max_temp_c=None)
    assert result["tier"] == "Unknown"


def test_classify_thermal_reports_throttling_when_supported():
    from backend.core import hardware_detector

    result = hardware_detector.classify_thermal(supported=True, throttling_detected=True, max_temp_c=99.0)
    assert result["tier"] == "Throttling"


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
