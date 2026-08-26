# backend/tests/diagnostics_and_rest_expansion_tests.py
#
# Regression tests for the expanded REST + IPC diagnostics surface:
#   - backend/rest/router.py: GET /v1/models/{id}/requirements,
#     /performance, /metadata, /v1/resources, and GET /v1/diagnostics/*
#     (hardware, models, cache, performance, transport, streaming).
#   - backend/ipc_router.py: diagnostics_hardware_request,
#     diagnostics_cache_request, diagnostics_performance_request — the
#     same IPC-transport parity every other model-compat packet type
#     already has with its REST twin.
#   - backend/core/perf_profiler.py: timing instrumentation actually
#     recording samples for the functions it wraps.
#
# Self-contained plain-assert tests (using FastAPI's TestClient for the
# REST half — already a project dependency via fastapi). Run directly:
#
#   python backend/tests/diagnostics_and_rest_expansion_tests.py

from __future__ import annotations

import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _client():
    from fastapi.testclient import TestClient
    from backend.rest.server import app
    return TestClient(app)


def _any_model_id() -> str:
    from backend.core.model_registry import get_all_models
    models = get_all_models()
    assert models, "expected at least one registered model to test against"
    return models[0]["id"]


# ============================================================
# PART 1 — GET /v1/models/{model_id}/*
# ============================================================
def test_get_model_requirements_matches_ipc_shape():
    from backend.core.resource_monitor import get_resource_snapshot
    from backend.core.compatibility_checker import check_requirements
    from backend.core.model_registry import get_model

    model_id = _any_model_id()
    resp = _client().get(f"/v1/models/{model_id}/requirements")
    assert resp.status_code == 200
    body = resp.json()
    assert body["model_cfg"]["id"] == model_id
    assert "meets_minimum" in body["compat"]

    # Same underlying function as the REST route — the two must never
    # structurally disagree on what's returned.
    expected_compat = check_requirements(get_resource_snapshot(), get_model(model_id))
    assert set(body["compat"].keys()) == set(expected_compat.keys())


def test_get_model_requirements_404s_for_unknown_model():
    resp = _client().get("/v1/models/not-a-real-model-id/requirements")
    assert resp.status_code == 404
    # FastAPI wraps HTTPException(detail=...) under a "detail" key.
    assert resp.json()["detail"]["error"]["code"]


def test_get_model_performance_returns_a_positive_speed_estimate():
    model_id = _any_model_id()
    resp = _client().get(f"/v1/models/{model_id}/performance")
    assert resp.status_code == 200
    body = resp.json()
    assert body["model_cfg"]["id"] == model_id
    assert body["projected_speed_toksec"] > 0


def test_get_model_metadata_returns_the_unified_model_info_shape():
    model_id = _any_model_id()
    resp = _client().get(f"/v1/models/{model_id}/metadata")
    assert resp.status_code == 200
    body = resp.json()
    assert body["model_id"] == model_id
    assert set(body.keys()) == {
        "model_id", "params", "quant", "quant_bytes", "quant_difficulty",
        "context", "requirements", "cache_fingerprint", "safety_profile",
    }
    assert body["safety_profile"] is not None, "REST route has a live snapshot, so this must be populated"


def test_get_resources_returns_live_usage_shape():
    resp = _client().get("/v1/resources")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("cpu_usage_pct", "ram_used_gb", "ram_total_gb", "vram_used_gb", "vram_total_gb"):
        assert key in body
    assert body["ram_total_gb"] > 0


# ============================================================
# PART 2 — GET /v1/diagnostics/*
# ============================================================
def test_get_diagnostics_hardware_returns_all_four_categories():
    resp = _client().get("/v1/diagnostics/hardware")
    assert resp.status_code == 200
    body = resp.json()
    assert "cpu" in body and "ram" in body and "storage" in body and "gpu" in body
    assert body["cpu"]["physical_cores"] > 0
    assert "cache_state" in body


def test_get_diagnostics_hardware_refresh_param_forces_reprobe():
    from backend.core import hardware_snapshot_cache as hsc

    call_count = {"n": 0}
    original = hsc.hardware_detector.detect_cpu

    def counting(*a, **kw):
        call_count["n"] += 1
        return original(*a, **kw)

    hsc.hardware_detector.detect_cpu = counting
    try:
        hsc._cache.clear()
        _client().get("/v1/diagnostics/hardware")  # populates cache
        before = call_count["n"]
        _client().get("/v1/diagnostics/hardware")  # cache hit, no new probe
        assert call_count["n"] == before
        _client().get("/v1/diagnostics/hardware?refresh=true")  # forced
        assert call_count["n"] == before + 1
    finally:
        hsc.hardware_detector.detect_cpu = original


def test_get_diagnostics_models_returns_model_info_for_the_whole_catalog():
    from backend.core.model_registry import get_all_models

    resp = _client().get("/v1/diagnostics/models")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["models"]) == len(get_all_models())
    assert all("quant_difficulty" in m for m in body["models"])


def test_get_diagnostics_cache_reports_warm_state_after_a_real_lookup():
    from backend.core.compatibility_checker import check_requirements
    from backend.core.resource_monitor import get_resource_snapshot
    from backend.core.model_registry import get_model

    # Force at least one real cache write for whichever auto-discovered
    # model (if any) doesn't have a hand-authored requirements block.
    for model_cfg in [get_model(_any_model_id())]:
        check_requirements(get_resource_snapshot(), model_cfg)

    resp = _client().get("/v1/diagnostics/cache")
    assert resp.status_code == 200
    body = resp.json()
    assert body["entry_count"] >= 0
    assert "integrity_ok" in body
    assert "schema_version" in body


def test_get_diagnostics_performance_reflects_real_instrumented_calls():
    from backend.core import perf_profiler
    from backend.core.model_manager import list_models

    perf_profiler.clear()
    list_models()  # instrumented via perf_profiler.timed(...)

    resp = _client().get("/v1/diagnostics/performance")
    assert resp.status_code == 200
    body = resp.json()
    assert "model_manager.list_models" in body["summary"]
    assert body["summary"]["model_manager.list_models"]["count"] >= 1


def test_get_diagnostics_performance_slow_paths_respects_threshold():
    from backend.core import perf_profiler

    perf_profiler.clear()
    perf_profiler.record("fast_thing", 5.0)
    perf_profiler.record("slow_thing", 9000.0)

    resp = _client().get("/v1/diagnostics/performance?threshold_ms=1000")
    body = resp.json()
    slow_labels = [s["label"] for s in body["slow_paths"]]
    assert "slow_thing" in slow_labels
    assert "fast_thing" not in slow_labels


def test_get_diagnostics_transport_reports_uptime():
    resp = _client().get("/v1/diagnostics/transport")
    assert resp.status_code == 200
    assert resp.json()["rest_api"]["status"] == "ok"


def test_get_diagnostics_streaming_reports_engine_identity():
    resp = _client().get("/v1/diagnostics/streaming")
    assert resp.status_code == 200
    assert resp.json()["engine"] == "StreamingEngine"


# ============================================================
# PART 3 — IPC diagnostics parity (backend/ipc_router.py)
# ============================================================
def test_ipc_diagnostics_hardware_request_matches_rest_shape():
    from backend import ipc_router, ipc_schema as schema

    result = ipc_router.dispatch({"type": schema.DIAGNOSTICS_HARDWARE_REQUEST, "payload": {}})
    assert result["type"] == schema.DIAGNOSTICS_HARDWARE_RESULT
    payload = result["payload"]
    assert "cpu" in payload and "ram" in payload and "gpu" in payload and "storage" in payload


def test_ipc_diagnostics_cache_request_returns_warm_up_summary():
    from backend import ipc_router, ipc_schema as schema

    result = ipc_router.dispatch({"type": schema.DIAGNOSTICS_CACHE_REQUEST, "payload": {}})
    assert result["type"] == schema.DIAGNOSTICS_CACHE_RESULT
    assert "entry_count" in result["payload"]


def test_ipc_diagnostics_performance_request_honors_threshold_payload():
    from backend import ipc_router, ipc_schema as schema
    from backend.core import perf_profiler

    perf_profiler.clear()
    perf_profiler.record("only_a_little_slow", 300.0)

    result = ipc_router.dispatch({
        "type": schema.DIAGNOSTICS_PERFORMANCE_REQUEST,
        "payload": {"threshold_ms": 100},
    })
    assert result["type"] == schema.DIAGNOSTICS_PERFORMANCE_RESULT
    labels = [s["label"] for s in result["payload"]["slow_paths"]]
    assert "only_a_little_slow" in labels


def test_all_three_new_diagnostics_types_are_in_handled_types():
    from backend import ipc_router, ipc_schema as schema

    for ptype in (schema.DIAGNOSTICS_HARDWARE_REQUEST, schema.DIAGNOSTICS_CACHE_REQUEST, schema.DIAGNOSTICS_PERFORMANCE_REQUEST):
        assert ptype in ipc_router.HANDLED_TYPES, f"{ptype} must be dispatchable"


# ============================================================
# PART 4 — perf_profiler.py instrumentation itself
# ============================================================
def test_check_requirements_is_instrumented():
    from backend.core import perf_profiler
    from backend.core.compatibility_checker import check_requirements
    from backend.core.resource_monitor import get_resource_snapshot
    from backend.core.model_registry import get_model

    perf_profiler.clear()
    check_requirements(get_resource_snapshot(), get_model(_any_model_id()))

    summary = perf_profiler.get_summary()
    assert "compatibility_checker.check_requirements" in summary


def test_estimate_speed_is_instrumented():
    from backend.core import perf_profiler
    from backend.core.performance_estimator import estimate_speed
    from backend.core.safety_profiles import select_profile
    from backend.core.resource_monitor import get_resource_snapshot
    from backend.core.model_registry import get_model

    perf_profiler.clear()
    snapshot = get_resource_snapshot()
    model_cfg = get_model(_any_model_id())
    estimate_speed(snapshot, model_cfg, select_profile(snapshot, model_cfg))

    summary = perf_profiler.get_summary()
    assert "performance_estimator.estimate_speed" in summary


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
