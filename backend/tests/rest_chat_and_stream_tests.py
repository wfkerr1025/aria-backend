# backend/tests/rest_chat_and_stream_tests.py
#
# Regression tests for REST API v1 Phase 2 (backend/rest/router.py's
# /v1/chat, /v1/chat/stream, and backend/rest/auth.py's API-key gate):
#   - auth: missing/invalid X-ARIA-API-Key -> 401 with a structured
#     {"error": {"code", "message"}} using ipc_errors.UNAUTHORIZED
#   - /v1/health and /v1/models stay open (no auth required)
#   - /v1/chat: self-knowledge short-circuit (no model invoked)
#   - /v1/chat: real-inference path returns a valid completion
#     (StreamingEngine.stream monkeypatched to a deterministic fake —
#     same monkeypatch-the-handler-boundary style as
#     backend/tests/skr_and_ipc_tests.py, so this never depends on a
#     real local model actually loading)
#   - /v1/chat: safety_warning short-circuits with the right shape
#   - /v1/chat/stream: same three cases, as a valid SSE event sequence
#
# Self-contained plain-assert tests, matching backend/tests/skr_and_ipc_tests.py
# — not pytest. Run directly:
#
#   python backend/tests/rest_chat_and_stream_tests.py

from __future__ import annotations

import os
import sys
import traceback
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

TEST_KEY = "test-key-123"


def _client():
    from fastapi.testclient import TestClient
    from backend.rest.server import app
    return TestClient(app)


def _with_test_api_key(fn):
    """
    Pin backend.rest.auth's expected key to a known value for the
    duration of fn(), regardless of whatever ARIA_REST_API_KEY (or lack
    thereof) is set in the real environment — same monkeypatch-and-
    restore pattern backend/tests/skr_and_ipc_tests.py already uses for
    backend.websocket.handlers.evaluate_safety.
    """
    from backend.rest import auth as auth_mod

    original = auth_mod.configured_key
    auth_mod.configured_key = lambda: TEST_KEY
    try:
        return fn()
    finally:
        auth_mod.configured_key = original


def _with_fake_stream(fake_stream_fn, fn):
    from backend.rest import router as router_mod

    original = router_mod._stream_engine.stream
    router_mod._stream_engine.stream = fake_stream_fn
    try:
        return fn()
    finally:
        router_mod._stream_engine.stream = original


def _with_fake_safety_decision(decision, fn):
    """
    Pin the safety gate for the duration of fn().

    The gate itself is unchanged; where it runs is not. REST used to call
    evaluate_safety inline in _prepare_chat_turn, so patching
    backend.rest.router.evaluate_safety was enough. Both transports now
    run the one sequence in backend/core/turn_orchestrator.py, so that is
    where the gate lives and where it has to be patched -- the same
    repoint backend/tests/skr_and_ipc_tests.py needed when the WebSocket
    path moved. The suggester is still reached through the router,
    because REST is what passes it in.
    """
    from backend.core import turn_orchestrator as orch_mod
    from backend.rest import router as router_mod

    original_safety = orch_mod.evaluate_safety
    original_suggest = router_mod._lighter_model_engine.suggest
    orch_mod.evaluate_safety = lambda model_cfg: decision
    router_mod._lighter_model_engine.suggest = lambda snapshot, model_cfg: []
    try:
        return fn()
    finally:
        orch_mod.evaluate_safety = original_safety
        router_mod._lighter_model_engine.suggest = original_suggest


def _fake_stream(request, send_packet):
    send_packet({"type": "stream_start", "modelId": request.model_id, "requestId": 1})
    send_packet({"type": "stream_token", "modelId": request.model_id, "requestId": 1, "token": "Hello "})
    send_packet({"type": "stream_token", "modelId": request.model_id, "requestId": 1, "token": "world"})
    send_packet({"type": "stream_end", "modelId": request.model_id, "requestId": 1})


def _parse_sse_events(text: str):
    """Minimal SSE parser: returns [(event_name, data_dict), ...]."""
    import json as _json

    events = []
    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue
        event_name = None
        data_line = None
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_line = line[len("data:"):].strip()
        if data_line is not None:
            events.append((event_name, _json.loads(data_line)))
    return events


# ============================================================
# PART 1 — auth
# ============================================================
def test_health_and_models_stay_open_without_a_key():
    client = _client()
    assert client.get("/v1/health").status_code == 200
    assert client.get("/v1/models").status_code == 200


def test_chat_missing_key_returns_401_with_structured_error():
    from backend import ipc_errors

    def scenario():
        client = _client()
        resp = client.post("/v1/chat", json={"message": "hi"})
        assert resp.status_code == 401, f"expected 401, got {resp.status_code}: {resp.text}"
        error = resp.json().get("detail", {}).get("error")
        assert error is not None, f"expected detail.error, got {resp.json()}"
        assert error.get("code") == ipc_errors.UNAUTHORIZED, error

    _with_test_api_key(scenario)


def test_chat_invalid_key_returns_401():
    def scenario():
        client = _client()
        resp = client.post("/v1/chat", json={"message": "hi"}, headers={"X-ARIA-API-Key": "wrong-key"})
        assert resp.status_code == 401, f"expected 401, got {resp.status_code}: {resp.text}"

    _with_test_api_key(scenario)


def test_chat_stream_missing_key_returns_401():
    def scenario():
        client = _client()
        resp = client.post("/v1/chat/stream", json={"message": "hi"})
        assert resp.status_code == 401, f"expected 401, got {resp.status_code}: {resp.text}"

    _with_test_api_key(scenario)


# ============================================================
# PART 2 — /v1/chat
# ============================================================
def test_chat_self_query_short_circuits_without_invoking_a_model():
    from backend.rest import router as router_mod

    def exploding_stream(*_args, **_kwargs):
        raise AssertionError("StreamingEngine.stream() was called for a self-query — must short-circuit instead")

    def scenario():
        def inner():
            client = _client()
            resp = client.post(
                "/v1/chat",
                json={"message": "what model are you using", "skip_safety_check": True},
                headers={"X-ARIA-API-Key": TEST_KEY},
            )
            assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
            body = resp.json()
            assert body.get("model_id") == "skr", f"expected sentinel model_id 'skr', got {body}"
            assert isinstance(body.get("reply"), str) and len(body["reply"]) > 0
        return _with_fake_stream(exploding_stream, inner)

    _with_test_api_key(scenario)


def test_chat_returns_completion_from_streaming_engine():
    def scenario():
        def inner():
            return _with_fake_stream(_fake_stream, lambda: _client().post(
                "/v1/chat",
                json={
                    "message": "tell me a joke",
                    "model_id": "mistral-7b-q4km",
                    "skip_safety_check": True,
                },
                headers={"X-ARIA-API-Key": TEST_KEY},
            ))
        resp = inner()
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body.get("model_id") == "mistral-7b-q4km", body
        assert body.get("reply") == "Hello world", f"expected joined tokens 'Hello world', got {body.get('reply')!r}"
        assert isinstance(body.get("timestamp"), (int, float))

    _with_test_api_key(scenario)


def test_chat_unknown_model_id_returns_400_with_shared_error_code():
    from backend import ipc_errors

    def scenario():
        client = _client()
        resp = client.post(
            "/v1/chat",
            json={"message": "hi", "model_id": "not-a-real-model-xyz", "skip_safety_check": True},
            headers={"X-ARIA-API-Key": TEST_KEY},
        )
        assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text}"
        error = resp.json().get("detail", {}).get("error")
        assert error.get("code") == ipc_errors.UNKNOWN_MODEL, error

    _with_test_api_key(scenario)


def test_chat_safety_warning_short_circuits_with_expected_shape():
    fake_decision = types.SimpleNamespace(
        requires_warning=True,
        safe_to_run=True,
        severity="caution",
        message="test safety message",
        snapshot=None,
        projected_cpu_pct=50.0,
        projected_ram_pct=90.0,
        projected_vram_pct=0.0,
    )

    def scenario():
        def inner():
            return _with_fake_safety_decision(fake_decision, lambda: _client().post(
                "/v1/chat",
                json={"message": "hi", "model_id": "mistral-7b-q4km"},
                headers={"X-ARIA-API-Key": TEST_KEY},
            ))
        resp = inner()
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body.get("type") == "safety_warning", body
        assert body.get("severity") == "caution", body
        assert body.get("projected") == {"cpu": 50.0, "ram": 90.0, "vram": 0.0}, body

    _with_test_api_key(scenario)


# ============================================================
# PART 3 — /v1/chat/stream
# ============================================================
def test_chat_stream_returns_valid_sse_sequence():
    def scenario():
        def inner():
            return _with_fake_stream(_fake_stream, lambda: _client().post(
                "/v1/chat/stream",
                json={"message": "tell me a joke", "model_id": "mistral-7b-q4km", "skip_safety_check": True},
                headers={"X-ARIA-API-Key": TEST_KEY},
            ))
        resp = inner()
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        assert resp.headers.get("content-type", "").startswith("text/event-stream"), resp.headers

        events = _parse_sse_events(resp.text)
        event_names = [name for name, _ in events]
        assert event_names == ["stream_start", "stream_token", "stream_token", "stream_end"], event_names

        tokens = [data["token"] for name, data in events if name == "stream_token"]
        assert "".join(tokens) == "Hello world", tokens

    _with_test_api_key(scenario)


def test_chat_stream_self_query_sends_single_token_sequence():
    def exploding_stream(*_args, **_kwargs):
        raise AssertionError("StreamingEngine.stream() was called for a self-query — must short-circuit instead")

    def scenario():
        def inner():
            return _with_fake_stream(exploding_stream, lambda: _client().post(
                "/v1/chat/stream",
                json={"message": "what model are you using", "skip_safety_check": True},
                headers={"X-ARIA-API-Key": TEST_KEY},
            ))
        resp = inner()
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"

        events = _parse_sse_events(resp.text)
        event_names = [name for name, _ in events]
        assert event_names == ["stream_start", "stream_token", "stream_end"], event_names
        assert all(data["modelId"] == "skr" for _, data in events), events

    _with_test_api_key(scenario)


def test_chat_stream_safety_warning_sends_single_dedicated_event():
    fake_decision = types.SimpleNamespace(
        requires_warning=True,
        safe_to_run=True,
        severity="block",
        message="test hard block",
        snapshot=None,
        projected_cpu_pct=10.0,
        projected_ram_pct=99.0,
        projected_vram_pct=0.0,
    )

    def scenario():
        def inner():
            return _with_fake_safety_decision(fake_decision, lambda: _client().post(
                "/v1/chat/stream",
                json={"message": "hi", "model_id": "mistral-7b-q4km"},
                headers={"X-ARIA-API-Key": TEST_KEY},
            ))
        resp = inner()
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"

        events = _parse_sse_events(resp.text)
        assert len(events) == 1, f"expected exactly one event (no stream_start/end around a safety_warning), got {events}"
        name, data = events[0]
        assert name == "safety_warning", name
        assert data.get("severity") == "block", data

    _with_test_api_key(scenario)


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
