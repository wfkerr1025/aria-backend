# backend/tests/rest_health_and_models_tests.py
#
# Regression tests for the new REST API v1 scaffolding
# (backend/rest/router.py, backend/rest/server.py):
#   - GET /v1/health returns 200 with status "ok"
#   - GET /v1/models returns 200 and matches
#     backend.core.model_manager.list_models() exactly (same data
#     source, not a duplicate)
#   - errors use the {"error": {"code", "message"}} shape backed by
#     backend/ipc_errors.py's codes
#
# Uses FastAPI's TestClient (httpx-backed, already an installed
# dependency) against backend.rest.server.app directly — no real port
# binding needed. Self-contained plain-assert tests, matching
# backend/tests/skr_and_ipc_tests.py — not pytest. Run directly:
#
#   python backend/tests/rest_health_and_models_tests.py

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


# ============================================================
# PART 1 — /v1/health
# ============================================================
def test_health_returns_200_and_ok_status():
    client = _client()
    resp = client.get("/v1/health")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"

    body = resp.json()
    assert body.get("status") == "ok", f"expected status=ok, got {body}"


def test_health_includes_grounded_version_and_uptime():
    from backend.core.self_knowledge import BACKEND_VERSION

    client = _client()
    body = client.get("/v1/health").json()

    assert body.get("version") == BACKEND_VERSION, (
        f"expected version to match self_knowledge.BACKEND_VERSION ({BACKEND_VERSION!r}), got {body.get('version')!r} — "
        "this must reuse the existing constant, not a separate hardcoded string"
    )
    assert isinstance(body.get("uptime"), (int, float)), f"expected numeric uptime, got {body.get('uptime')!r}"
    assert body["uptime"] >= 0.0


# ============================================================
# PART 2 — /v1/models
# ============================================================
def test_models_returns_200_and_matches_list_models():
    from backend.core.model_manager import list_models

    client = _client()
    resp = client.get("/v1/models")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"

    body = resp.json()
    assert "models" in body, f"expected a 'models' key, got {body.keys()}"

    expected = list_models()
    assert len(body["models"]) == len(expected), (
        f"REST /v1/models returned {len(body['models'])} models, "
        f"but the real backend.core.model_manager.list_models() returns {len(expected)} — "
        "these must be the same data source"
    )

    expected_ids = {m["model_cfg"]["id"] for m in expected}
    actual_ids = {m["model_cfg"]["id"] for m in body["models"]}
    assert actual_ids == expected_ids, f"model id sets differ: expected {expected_ids}, got {actual_ids}"


# ============================================================
# PART 3 — error shape
# ============================================================
def test_models_error_uses_shared_ipc_error_code():
    """
    A handler failure must surface as {"error": {"code", "message"}}
    using a code from backend/ipc_errors.py — not FastAPI's default
    {"detail": "..."} shape, and not a REST-only error vocabulary.
    """
    from backend import ipc_errors
    from backend.rest import router as router_mod

    original = router_mod.list_models
    router_mod.list_models = lambda: (_ for _ in ()).throw(RuntimeError("simulated failure"))

    try:
        client = _client()
        resp = client.get("/v1/models")
    finally:
        router_mod.list_models = original

    assert resp.status_code == 500, f"expected 500, got {resp.status_code}: {resp.text}"

    body = resp.json()
    # FastAPI wraps a raised HTTPException's `detail` verbatim under a
    # top-level "detail" key — the {"error": {...}} shape this task asks
    # for lives one level in, at body["detail"].
    error = body.get("detail", {}).get("error")
    assert error is not None, f"expected body['detail']['error'], got {body}"
    assert error.get("code") == ipc_errors.HANDLER_EXCEPTION, f"expected code={ipc_errors.HANDLER_EXCEPTION}, got {error}"
    assert "simulated failure" in error.get("message", "")


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
