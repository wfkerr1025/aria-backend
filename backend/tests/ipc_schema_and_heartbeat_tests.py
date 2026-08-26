# backend/tests/ipc_schema_and_heartbeat_tests.py
#
# Regression tests for the WebSocket IPC QoL additions:
#   - backend/ipc_schema.py's constants match ipc_router.HANDLED_TYPES
#     (guards against a typo silently dropping a handler from the dispatch
#     table)
#   - heartbeat round-trip through ipc_router.dispatch()
#   - batch_request happy path and its refusal to run a non-HANDLED_TYPES
#     packet type (chat_request in particular) inside a batch
#   - error_response()/build_error() still produce the flat shape existing
#     consumers (webui/core/bridge.js, chat.js) rely on
#
# Self-contained plain-assert tests, matching backend/tests/skr_and_ipc_tests.py
# — not pytest. Run directly:
#
#   python backend/tests/ipc_schema_and_heartbeat_tests.py

from __future__ import annotations

import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ============================================================
# PART 1 — schema/dispatch-table consistency
# ============================================================
def test_schema_request_constants_all_registered_in_router():
    from backend import ipc_router, ipc_schema as schema

    # Every *_REQUEST constant in the schema (the model-compat + key-mgmt
    # surface ipc_router owns) must have a live handler — if a rename typo
    # ever desyncs schema.py from ipc_router.py's _HANDLERS dict, this
    # catches it immediately instead of as a silent "unknown packet type"
    # at runtime.
    # CHAT_REQUEST is deliberately excluded: it has its own dedicated
    # pipeline in backend/websocket/handlers.py (streaming inference) and
    # is never routed through ipc_router.dispatch() — see that module's
    # docstring and _dispatch()'s explicit "chat_request" branch.
    request_constants = [
        v for k, v in vars(schema).items()
        if k.isupper() and k.endswith("_REQUEST") and isinstance(v, str) and v != schema.CHAT_REQUEST
    ]
    assert request_constants, "expected to find at least one *_REQUEST constant in ipc_schema"

    for value in request_constants:
        assert value in ipc_router.HANDLED_TYPES, f"{value!r} is defined in ipc_schema but has no handler in ipc_router.HANDLED_TYPES"


def test_heartbeat_and_batch_request_are_registered():
    from backend import ipc_router, ipc_schema as schema

    assert schema.HEARTBEAT in ipc_router.HANDLED_TYPES
    assert schema.BATCH_REQUEST in ipc_router.HANDLED_TYPES


# ============================================================
# PART 2 — heartbeat round-trip
# ============================================================
def test_heartbeat_echoes_ts():
    from backend import ipc_router, ipc_schema as schema

    result = ipc_router.dispatch({"type": schema.HEARTBEAT, "payload": {"ts": 1234567}})
    assert result["type"] == schema.HEARTBEAT_ACK, f"expected heartbeat_ack, got {result}"
    assert result["ts"] == 1234567, f"expected echoed ts, got {result}"


def test_heartbeat_with_no_ts_does_not_crash():
    from backend import ipc_router, ipc_schema as schema

    result = ipc_router.dispatch({"type": schema.HEARTBEAT, "payload": {}})
    assert result["type"] == schema.HEARTBEAT_ACK
    assert result["ts"] is None


# ============================================================
# PART 3 — batch_request
# ============================================================
def test_batch_request_happy_path():
    from backend import ipc_router, ipc_schema as schema

    result = ipc_router.dispatch({
        "type": schema.BATCH_REQUEST,
        "payload": {"requests": [
            {"type": schema.MODELS_LIST_REQUEST, "payload": {}},
            {"type": schema.HEARTBEAT, "payload": {"ts": 42}},
        ]},
    })
    assert result["type"] == schema.BATCH_RESPONSE, f"expected batch_response, got {result}"
    responses = result["payload"]["responses"]
    assert len(responses) == 2, f"expected 2 sub-responses, got {responses}"
    assert responses[0]["type"] == schema.MODELS_LIST_RESULT, responses[0]
    assert responses[1]["type"] == schema.HEARTBEAT_ACK, responses[1]
    assert responses[1]["ts"] == 42


def test_batch_request_rejects_chat_request_item():
    from backend import ipc_router, ipc_schema as schema

    result = ipc_router.dispatch({
        "type": schema.BATCH_REQUEST,
        "payload": {"requests": [
            {"type": schema.CHAT_REQUEST, "payload": {"messages": []}},
        ]},
    })
    assert result["type"] == schema.BATCH_RESPONSE
    responses = result["payload"]["responses"]
    assert len(responses) == 1
    assert responses[0]["type"] == schema.ERROR, f"expected chat_request inside a batch to be rejected, got {responses[0]}"


def test_batch_request_rejects_malformed_items_without_crashing():
    from backend import ipc_router, ipc_schema as schema

    result = ipc_router.dispatch({
        "type": schema.BATCH_REQUEST,
        "payload": {"requests": [{"no_type_field": True}, "not even a dict"]},
    })
    assert result["type"] == schema.BATCH_RESPONSE
    responses = result["payload"]["responses"]
    assert len(responses) == 2
    assert all(r["type"] == schema.ERROR for r in responses)


def test_batch_request_missing_requests_list_returns_error_not_crash():
    from backend import ipc_router, ipc_schema as schema

    result = ipc_router.dispatch({"type": schema.BATCH_REQUEST, "payload": {}})
    assert result["type"] == schema.ERROR, f"expected an error packet, got {result}"


# ============================================================
# PART 4 — error_response()/build_error() shape
# ============================================================
def test_error_response_default_shape_unchanged():
    from backend import ipc_packet_formats as fmt

    result = fmt.error_response("something broke")
    assert result["type"] == "error"
    assert result["message"] == "something broke"
    assert "request_type" not in result
    assert "code" in result, "expected a default error code even when the caller doesn't pass one"


def test_error_response_with_request_type_and_code():
    from backend import ipc_packet_formats as fmt, ipc_errors

    result = fmt.error_response("bad model id", "chat_request", ipc_errors.UNKNOWN_MODEL)
    assert result == {
        "type": "error",
        "code": ipc_errors.UNKNOWN_MODEL,
        "message": "bad model id",
        "request_type": "chat_request",
    }


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
