# backend/tests/metrics_docs_and_rest_v2_tests.py
#
# Regression tests for Phase 2:
#   - backend/core/metrics.py: GET /v1/metrics's payload assembly —
#     real perf_profiler samples + real resource_monitor/
#     hardware_snapshot_cache data, no network call anywhere.
#   - backend/core/docs_generator.py: real Markdown generation from
#     live module introspection.
#   - New REST routes: /v1/metrics, /v1/providers/unified(/{name}),
#     /v1/tools, /v1/tools/{name}/execute, /v1/plugins,
#     /v1/diagnostics/plugins.
#   - New IPC packet types: metrics_request, tools_list_request,
#     tool_execute_request, plugins_list_request,
#     providers_unified_list_request.
#
# Self-contained plain-assert tests. Run directly:
#
#   python backend/tests/metrics_docs_and_rest_v2_tests.py

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


TEST_KEY = "test-key-456"


def _client():
    from fastapi.testclient import TestClient
    from backend.rest.server import app
    return TestClient(app)


def _with_test_api_key(fn):
    """Same monkeypatch-and-restore convention as
    backend/tests/rest_chat_and_stream_tests.py's identical helper."""
    from backend.rest import auth as auth_mod

    original = auth_mod.configured_key
    auth_mod.configured_key = lambda: TEST_KEY
    try:
        return fn()
    finally:
        auth_mod.configured_key = original


# ============================================================
# PART 1 — metrics.py
# ============================================================
def test_get_metrics_snapshot_has_all_three_sections():
    from backend.core.metrics import get_metrics_snapshot

    snapshot = get_metrics_snapshot()
    assert set(snapshot.keys()) == {"streaming", "resources", "instrumented_functions"}


def test_streaming_metrics_are_null_before_any_turn_and_populated_after():
    from backend.core import perf_profiler
    from backend.core.metrics import get_streaming_metrics
    from backend.core.streaming_engine_v2 import StreamingEngineV2

    perf_profiler.clear()
    before = get_streaming_metrics()
    assert before["v2_total"] is None

    class FastProvider:
        def stream(self, request, callback):
            callback({"content": "x"})

    handle = StreamingEngineV2().stream(object(), FastProvider(), lambda p: None)
    handle.wait(timeout=5)

    after = get_streaming_metrics()
    assert after["v2_total"] is not None
    assert after["v2_total"]["count"] >= 1


def test_resource_metrics_report_real_nonzero_ram():
    from backend.core.metrics import get_resource_metrics

    metrics = get_resource_metrics()
    assert metrics["ram_total_gb"] > 0


# ============================================================
# PART 2 — docs_generator.py
# ============================================================
def test_generate_all_writes_a_readme_index_and_every_target_file():
    from backend.core.docs_generator import generate_all, _DOC_TARGETS

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        written = generate_all(output_dir=out_dir)

        assert (out_dir / "README.md").exists()
        for filename, _title, _module in _DOC_TARGETS:
            assert (out_dir / filename).exists(), f"expected {filename} to be generated"
        assert len(written) == len(_DOC_TARGETS) + 1  # + README.md


def test_generated_doc_contains_the_real_module_docstring():
    from backend.core.docs_generator import generate_all
    from backend.core import tool_registry

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        generate_all(output_dir=out_dir)
        content = (out_dir / "tool-registry.md").read_text(encoding="utf-8")

        first_doc_line = tool_registry.__doc__.strip().splitlines()[0]
        assert first_doc_line in content


def test_generated_doc_lists_real_public_functions():
    from backend.core.docs_generator import generate_all

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        generate_all(output_dir=out_dir)
        content = (out_dir / "tool-registry.md").read_text(encoding="utf-8")

        assert "execute_tool" in content
        assert "register_tool" in content


# ============================================================
# PART 3 — new REST routes
# ============================================================
def test_rest_metrics_endpoint():
    resp = _client().get("/v1/metrics")
    assert resp.status_code == 200
    assert "streaming" in resp.json()


def test_rest_providers_unified_list():
    from backend.llm.providers import provider_registry

    resp = _client().get("/v1/providers/unified")
    assert resp.status_code == 200
    names = {p["metadata"]["provider_name"] for p in resp.json()["providers"]}
    assert names == set(provider_registry.list_providers())


def test_rest_providers_unified_detail_404_for_unknown():
    resp = _client().get("/v1/providers/unified/not-a-real-one")
    assert resp.status_code == 404


def test_rest_tools_list():
    resp = _client().get("/v1/tools")
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()["tools"]}
    assert "weather" in names and "web_search" in names


def test_rest_tool_execute_requires_api_key():
    resp = _client().post("/v1/tools/weather/execute", json={"args": {"location": "Richmond"}})
    assert resp.status_code == 401


def test_rest_tool_execute_unknown_tool_with_key_returns_404():
    def scenario():
        resp = _client().post(
            "/v1/tools/not_a_real_tool/execute",
            json={"args": {}},
            headers={"X-ARIA-API-Key": TEST_KEY},
        )
        assert resp.status_code == 404

    _with_test_api_key(scenario)


def test_rest_tool_execute_with_key_runs_a_real_registered_tool():
    from backend.core.tool_registry import register_tool, unregister_tool, ToolSchema, PERMISSION_SAFE

    register_tool(ToolSchema(name="_rest_test_tool", description="x", permission=PERMISSION_SAFE), lambda: "rest-tool-result")

    def scenario():
        resp = _client().post(
            "/v1/tools/_rest_test_tool/execute",
            json={"args": {}},
            headers={"X-ARIA-API-Key": TEST_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["value"] == "rest-tool-result"

    try:
        _with_test_api_key(scenario)
    finally:
        unregister_tool("_rest_test_tool")


def test_rest_plugins_list():
    resp = _client().get("/v1/plugins")
    assert resp.status_code == 200
    body = resp.json()
    assert "plugins" in body and "failed" in body


def test_rest_diagnostics_plugins():
    resp = _client().get("/v1/diagnostics/plugins")
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


# ============================================================
# PART 4 — new IPC packet types
# ============================================================
def test_ipc_metrics_request():
    from backend import ipc_router, ipc_schema as schema

    result = ipc_router.dispatch({"type": schema.METRICS_REQUEST, "payload": {}})
    assert result["type"] == schema.METRICS_RESULT
    assert "streaming" in result["payload"]


def test_ipc_tools_list_request():
    from backend import ipc_router, ipc_schema as schema

    result = ipc_router.dispatch({"type": schema.TOOLS_LIST_REQUEST, "payload": {}})
    assert result["type"] == schema.TOOLS_LIST_RESULT
    assert any(t["name"] == "weather" for t in result["payload"]["tools"])


def test_ipc_tool_execute_request_missing_tool_name():
    from backend import ipc_router, ipc_schema as schema

    result = ipc_router.dispatch({"type": schema.TOOL_EXECUTE_REQUEST, "payload": {}})
    assert result["type"] == "error"


def test_ipc_tool_execute_request_runs_a_real_registered_tool():
    from backend import ipc_router, ipc_schema as schema
    from backend.core.tool_registry import register_tool, unregister_tool, ToolSchema, PERMISSION_SAFE

    register_tool(ToolSchema(name="_ipc_test_tool", description="x", permission=PERMISSION_SAFE), lambda: "ipc-tool-result")
    try:
        result = ipc_router.dispatch({
            "type": schema.TOOL_EXECUTE_REQUEST,
            "payload": {"tool_name": "_ipc_test_tool", "args": {}},
        })
        assert result["type"] == schema.TOOL_EXECUTE_RESULT
        assert result["payload"]["ok"] is True
        assert result["payload"]["value"] == "ipc-tool-result"
    finally:
        unregister_tool("_ipc_test_tool")


def test_ipc_plugins_list_request():
    from backend import ipc_router, ipc_schema as schema
    from backend.core import plugin_registry

    plugin_registry.reload_all()
    result = ipc_router.dispatch({"type": schema.PLUGINS_LIST_REQUEST, "payload": {}})
    assert result["type"] == schema.PLUGINS_LIST_RESULT
    assert len(result["payload"]["plugins"]) >= 1


def test_ipc_providers_unified_list_request():
    from backend import ipc_router, ipc_schema as schema

    result = ipc_router.dispatch({"type": schema.PROVIDERS_UNIFIED_LIST_REQUEST, "payload": {}})
    assert result["type"] == schema.PROVIDERS_UNIFIED_LIST_RESULT
    assert len(result["payload"]["providers"]) >= 10


def test_every_new_phase2_ipc_type_is_dispatchable():
    from backend import ipc_router, ipc_schema as schema

    for ptype in (
        schema.METRICS_REQUEST, schema.TOOLS_LIST_REQUEST, schema.TOOL_EXECUTE_REQUEST,
        schema.PLUGINS_LIST_REQUEST, schema.PROVIDERS_UNIFIED_LIST_REQUEST,
    ):
        assert ptype in ipc_router.HANDLED_TYPES


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
