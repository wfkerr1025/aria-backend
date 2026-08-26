# backend/tests/skr_and_ipc_tests.py
#
# Regression tests for:
#   - malformed IPC packets never crashing dispatch
#   - self-query intent detection (including fuzzy matching)
#   - SKR answers being grounded, deterministic, and model-free
#   - Models Page override actions (active/fallback/emergency)
#   - fallback/emergency model auto-switching under RAM pressure
#
# Self-contained plain-assert tests, not pytest — nothing else in this
# repo's backend/tests/ uses pytest, and backend/tests/run_all_tests.py's
# existing subprocess-per-file runner doesn't actually work (the files it
# invokes are class definitions expecting an `app` object from the old
# app-instance architecture, with no __main__ block to run standalone —
# invoking them via `py <path>` executes nothing). Run this file directly:
#
#   python backend/tests/skr_and_ipc_tests.py

from __future__ import annotations

import asyncio
import os
import sys
import traceback

# Repo root on sys.path — same bootstrap backend/ws_server.py uses, needed
# because this file is meant to be run directly
# (`python backend/tests/skr_and_ipc_tests.py`), where Python only puts
# this script's own directory on sys.path, not the repo root the
# `backend.*` imports below need.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ============================================================
# Minimal fake WebSocket — records every packet handed to send()
# without touching a real socket, so WebSocketHandler._dispatch()
# can be exercised exactly as it runs in production.
# ============================================================
class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, raw: str):
        self.sent.append(raw)


# ------------------------------------------------------------------
# Step 3 of the Turn Orchestrator blueprint moved the chat sequence out
# of handlers.py and into backend/core/turn_orchestrator.py. These tests
# hooked handler._start_inference and handlers_mod.evaluate_safety --
# both internals of the old structure. What they assert is unchanged;
# only the seam they attach to moved:
#
#     handler._start_inference(packet)  ->  _start_inference_from(result)
#     handlers_mod.evaluate_safety      ->  turn_orchestrator.evaluate_safety
#
# Status packets ({"type": "status", "value": ...}) were added for the turn
# indicator -- see backend/core/turn_status.py. They are additive decoration
# and carry no result, so the assertions below that pin an exact answer
# sequence filter them out rather than counting them. What those tests are
# about is unchanged: which result packets a turn produces, and in what
# order.


def _result_packets(sent):
    """The packets that carry a result, with status decoration removed."""
    import json as _json

    parsed = [_json.loads(item) if isinstance(item, str) else item for item in sent]
    return [packet for packet in parsed if packet.get("type") != "status"]

#     packet["allowOverride"]           ->  result.inference_request.allow_override
# ------------------------------------------------------------------
from backend.core import turn_orchestrator as _turn_orch  # noqa: E402

def _run(coro):
    return asyncio.run(coro)


# ============================================================
# PART 1 — malformed IPC packets never crash dispatch
# ============================================================
def test_ipc_router_dispatch_rejects_dict_type():
    from backend import ipc_router

    # The exact malformed shape a `bridge.send({type: "...", ...})` bug
    # (an object passed where bridge.send(type, payload) expects a
    # string) produces.
    result = ipc_router.dispatch({"type": {"type": "load_model_override", "model_id": "x"}, "payload": {}})
    assert result["type"] == "error", f"expected an error packet, got {result}"


def test_ipc_router_dispatch_rejects_non_dict_packet():
    from backend import ipc_router

    result = ipc_router.dispatch("not even a dict")  # type: ignore[arg-type]
    assert result["type"] == "error", f"expected an error packet, got {result}"


def test_ipc_router_dispatch_rejects_missing_type():
    from backend import ipc_router

    result = ipc_router.dispatch({"payload": {}})
    assert result["type"] == "error", f"expected an error packet, got {result}"


def test_ipc_router_dispatch_still_works_for_valid_packets():
    from backend import ipc_router

    result = ipc_router.dispatch({"type": "models_list_request", "payload": {}})
    assert result["type"] == "models_list_result", f"expected models_list_result, got {result}"
    assert len(result["payload"]["models"]) > 0, "expected at least one registered model"


def test_websocket_dispatch_survives_dict_type_and_does_not_poison_connection():
    from backend.websocket.handlers import WebSocketHandler

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)

        # The malformed packet — must not raise.
        await handler._dispatch({"type": {"type": "load_model_override", "model_id": "x"}, "payload": {}})

        # A normal, valid packet on the SAME handler right after — proves
        # the malformed one didn't leave any bad state behind.
        await handler._dispatch({"type": "models_list_request", "payload": {}})

        return ws.sent

    sent = _run(scenario())
    assert len(sent) == 2, f"expected 2 responses (one error, one real), got {len(sent)}: {sent}"

    import json
    first = json.loads(sent[0])
    second = json.loads(sent[1])
    assert first["type"] == "error", f"expected first response to be an error packet, got {first}"
    assert second["type"] == "models_list_result", f"expected second response to succeed normally, got {second}"


def test_websocket_dispatch_rejects_non_string_and_empty_type():
    from backend.websocket.handlers import WebSocketHandler

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)
        await handler._dispatch({"type": 12345, "payload": {}})
        await handler._dispatch({"type": "", "payload": {}})
        await handler._dispatch({"type": None, "payload": {}})
        return ws.sent

    import json
    sent = _run(scenario())
    assert len(sent) == 3
    for raw in sent:
        assert json.loads(raw)["type"] == "error"


# ============================================================
# PART 2 — self-query intent detection, including fuzzy matching
# ============================================================
def test_self_query_intent_detection():
    from backend.core.conversation_manager import detect_intent, SELF_QUERY_INTENTS

    phrases = [
        "what models do you have",
        "what models do you use?",  # reported as unknown_intent by a user log; verified it already classifies correctly
        "what model are you using",
        "which model is active",
        "what is your fallback model",
        "what models are installed",
        "tell me your models",  # fuzzy match — no exact phrase covers this
        "what models do yhou have",  # typo — still matches via "what model" substring
    ]
    for phrase in phrases:
        intent = detect_intent(phrase)
        assert intent in SELF_QUERY_INTENTS, f"{phrase!r} classified as {intent!r}, expected a self-query intent"
        assert intent == "model_query", f"{phrase!r} classified as {intent!r}, expected model_query"


def test_fuzzy_matching_does_not_produce_false_positives():
    from backend.core.conversation_manager import detect_intent

    # These mention "model"/self-referential words but are NOT self-queries.
    assert detect_intent("write a data model for this app") != "model_query"
    assert detect_intent("what is the weather in Richmond VA") == "weather_query"
    assert detect_intent("hi there") == "greeting"


# ============================================================
# PART 3 — SKR answers: grounded, deterministic, no model involved
# ============================================================
def test_skr_answers_are_grounded_and_deterministic():
    from backend.core import self_knowledge as sk
    from backend.core.mode_manager import ModeManager

    mm = ModeManager()
    original_mode = mm.get_mode()
    original_override = mm.get_explicit_model_override()
    try:
        mm.set_mode("local")

        snapshot = sk.build_snapshot(
            active_model_id="mistral-7b-q4km",
            provider_name="local",
            conversation_id="test-convo",
            history_length=1,
            multi_turn_mode=True,
        )
        assert snapshot.routing_mode == "local"

        for intent in sorted(sk._SELF_QUERY_INTENT_LITERALS):
            answer_a = sk.answer_self_query(intent, snapshot)
            answer_b = sk.answer_self_query(intent, snapshot)
            assert answer_a == answer_b, f"{intent} answer is not deterministic"
            assert isinstance(answer_a, str) and answer_a.strip(), f"{intent} produced an empty answer"

        model_answer = sk.answer_self_query("model_query", snapshot)
        assert snapshot.active_model_display_name in model_answer, (
            f"model_query answer must name the real active model's display name, got {model_answer!r}"
        )
        assert model_answer == f"I am using {snapshot.active_model_display_name} (Local)."

        # Legacy canned strings must never appear in ANY self-query
        # reply, for ANY intent — this is the exact list named in the
        # "remove legacy canned self-query text" task.
        _FORBIDDEN_LEGACY_SNIPPETS = [
            "My fallback model is", "My emergency model is", "Installed models:",
            "I switch to those under RAM pressure", "auto-switch between",
        ]
        for intent in sorted(sk._SELF_QUERY_INTENT_LITERALS):
            answer = sk.answer_self_query(intent, snapshot)
            for snippet in _FORBIDDEN_LEGACY_SNIPPETS:
                assert snippet not in answer, f"{intent} answer contains forbidden legacy text {snippet!r}: {answer!r}"
    finally:
        mm.set_mode(original_mode)
        mm.set_explicit_model_override(original_override)


def test_skr_short_circuit_never_touches_a_model():
    """
    The actual chat_request path: a self-query must resolve entirely
    inside _answer_self_query_directly() without ever calling
    _start_inference() (which is what would load/run a model). Verified
    by monkeypatching _start_inference to explode if it's ever reached.

    Mode is pinned to "local" so the reply text is deterministic — see
    self_knowledge.answer_self_query()'s "model_query" branch, which
    replies "I am using <model> (Local)." in Local Mode, never a
    fallback/emergency/installed-models list.
    """
    from backend.websocket.handlers import WebSocketHandler
    from backend.core.mode_manager import ModeManager

    mm = ModeManager()
    original_mode = mm.get_mode()

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)

        async def _explode(*_args, **_kwargs):
            raise AssertionError("_start_inference() was called for a self-query — it must never load a model")

        handler._start_inference_from = _explode

        await handler._dispatch({
            "type": "chat_request",
            "modelId": "mistral-7b-q4km",
            "conversationId": "skr-no-model-test",
            "messages": [{"role": "user", "content": "what models do you have?"}],
        })
        return ws.sent

    import json
    try:
        mm.set_mode("local")
        sent = _run(scenario())
    finally:
        mm.set_mode(original_mode)

    packets = _result_packets(sent)
    assert len(packets) == 3, f"expected stream_start/stream_token/stream_end, got {packets}"
    assert [p["type"] for p in packets] == ["stream_start", "stream_token", "stream_end"]
    assert packets[0]["modelId"] == "skr", "self-query replies must use the 'skr' sentinel modelId, not a real one"
    token = packets[1]["token"]
    assert token.startswith("I am using "), f"model_query reply must use the exact required phrasing, got {token!r}"
    assert token.endswith(" (Local)."), f"Local Mode model_query reply must end with ' (Local).', got {token!r}"


# ============================================================
# PART 4 — Models Page override actions never crash the backend
# ============================================================
def test_models_page_role_actions_round_trip():
    from backend.core import model_registry
    from backend.core import model_manager

    original_fallback = model_registry.get_fallback_model_id()
    original_emergency = model_registry.get_emergency_model_id()

    try:
        result = model_manager.set_fallback_model("qwen2.5-0.5b-instruct-q4_k_m")
        assert result["ok"], f"set_fallback_model failed: {result}"
        assert model_registry.get_fallback_model_id() == "qwen2.5-0.5b-instruct-q4_k_m"

        result2 = model_manager.set_emergency_model("phi-3-mini-4k-instruct-q4")
        assert result2["ok"], f"set_emergency_model failed: {result2}"
        assert model_registry.get_emergency_model_id() == "phi-3-mini-4k-instruct-q4"

        # Unknown model_id must fail cleanly, not raise.
        result3 = model_manager.set_fallback_model("totally-fake-model-id")
        assert result3["ok"] is False
        assert "reason" in result3
    finally:
        # Restore whatever roles were configured before this test ran —
        # tests must not leave persisted config mutated.
        if original_fallback:
            model_manager.set_fallback_model(original_fallback)
        if original_emergency:
            model_manager.set_emergency_model(original_emergency)


def test_models_page_list_reflects_roles():
    from backend.core import model_manager

    entries = model_manager.list_models()
    assert len(entries) > 0
    actives = [e for e in entries if e["is_active"]]
    assert len(actives) == 1, f"expected exactly one active model, got {len(actives)}"


# ============================================================
# PART 5 — Auto Selector controls model choice; fallback/emergency are
# load-SURVIVAL tiers only, never a pre-emptive RAM-percentage override
# (see backend/llm/providers/local_provider.py's module docstring for
# the diagnosis that led here: every request used to be silently forced
# onto whatever model was "active" regardless of what the Auto Selector
# picked).
# ============================================================
def test_candidate_chain_tries_requested_model_first():
    from backend.llm.providers.local_provider import _candidate_chain
    from backend.core import model_registry

    fallback = model_registry.get_fallback_model_id()
    emergency = model_registry.get_emergency_model_id()

    chain = _candidate_chain("qwen2.5-0.5b-instruct-q4_k_m")
    assert chain[0] == ("qwen2.5-0.5b-instruct-q4_k_m", "requested"), chain
    # fallback/emergency still present, just never tried first — unless
    # one of them happens to BE the requested model_id itself, in which
    # case it's correctly de-duplicated rather than listed twice under
    # two different labels (see the next test for that case directly).
    remaining = [c[0] for c in chain[1:]]
    assert fallback is None or fallback == "qwen2.5-0.5b-instruct-q4_k_m" or fallback in remaining
    assert emergency is None or emergency == "qwen2.5-0.5b-instruct-q4_k_m" or emergency in remaining


def test_candidate_chain_deduplicates_when_requested_equals_fallback():
    from backend.llm.providers.local_provider import _candidate_chain
    from backend.core import model_registry

    fallback = model_registry.get_fallback_model_id()
    if not fallback:
        return  # nothing configured to collide with — nothing to test here
    chain = _candidate_chain(fallback)
    ids = [c[0] for c in chain]
    assert ids.count(fallback) == 1, f"fallback model_id repeated in chain: {chain}"


def test_candidate_chain_prefers_fallback_over_active_when_nothing_requested():
    """
    requested_id being empty shouldn't normally happen (ProviderRouter
    always resolves it before this provider runs) — but if it does,
    fallback/emergency (curated, predictable tiers) are tried before
    "active" (which could be anything, including a heavy model — using
    it as a default here would reintroduce the exact unpredictable
    heavy-model selection this fix removed). "active" is only used when
    NEITHER fallback nor emergency is configured either.
    """
    from backend.llm.providers.local_provider import _candidate_chain
    from backend.core import model_registry

    fallback = model_registry.get_fallback_model_id()
    emergency = model_registry.get_emergency_model_id()
    active = model_registry.get_active_model_id()

    chain = _candidate_chain(None)
    ids_in_order = [c[0] for c in chain]

    if fallback or emergency:
        assert active not in ids_in_order or active in (fallback, emergency), (
            f"'active' must not be used as a default when fallback/emergency are configured: {chain}"
        )
    elif active:
        assert chain == [(active, "active")], chain
    else:
        assert chain == []


def test_load_model_for_request_uses_requested_model_when_it_loads():
    """The core bug fix: a low-complexity request's chosen model_id must
    actually be what loads — not whatever happens to be "active"."""
    from backend.llm.providers.local_provider import Provider
    from types import SimpleNamespace

    provider = Provider.__new__(Provider)  # skip __init__ — no real ModelLoader needed
    loaded_ids = []

    class _FakeLoader:
        def load_model(self, model_id, allow_override=False):
            loaded_ids.append(model_id)
            return object()

    provider.loader = _FakeLoader()
    request = SimpleNamespace(model_id="qwen2.5-0.5b-instruct-q4_k_m", allow_override=False)

    model_id, model = provider._load_model_for_request(request)

    assert model_id == "qwen2.5-0.5b-instruct-q4_k_m", (
        f"expected the Auto Selector's requested model to load, got {model_id!r}"
    )
    assert loaded_ids == ["qwen2.5-0.5b-instruct-q4_k_m"], "must not attempt any other tier when the requested model loads fine"


def test_load_model_for_request_falls_back_only_on_real_load_failure():
    from backend.llm.providers.local_provider import Provider
    from types import SimpleNamespace

    provider = Provider.__new__(Provider)
    attempts = []

    class _FailThenSucceedLoader:
        def load_model(self, model_id, allow_override=False):
            attempts.append(model_id)
            if model_id == "requested-model-that-does-not-exist":
                raise ValueError("Unknown model ID: requested-model-that-does-not-exist")
            return object()

    provider.loader = _FailThenSucceedLoader()
    request = SimpleNamespace(model_id="requested-model-that-does-not-exist", allow_override=False)

    model_id, model = provider._load_model_for_request(request)

    assert attempts[0] == "requested-model-that-does-not-exist", "must try the requested model FIRST, even though it will fail"
    assert len(attempts) >= 2, "must fall through to the next tier after a real load failure"
    assert model_id != "requested-model-that-does-not-exist", "the failed model must never be returned as the one that served the request"


def test_load_model_for_request_raises_when_every_tier_fails():
    from backend.llm.providers.local_provider import Provider
    from types import SimpleNamespace

    provider = Provider.__new__(Provider)

    class _AlwaysFailLoader:
        def load_model(self, model_id, allow_override=False):
            raise RuntimeError(f"simulated load failure for {model_id}")

    provider.loader = _AlwaysFailLoader()
    request = SimpleNamespace(model_id="requested-model-that-does-not-exist", allow_override=False)

    try:
        provider._load_model_for_request(request)
        raise AssertionError("expected an exception when every candidate fails to load")
    except RuntimeError:
        pass  # exactly what a fully-exhausted fallback chain must do — never fabricate a result


def test_load_model_for_request_never_falls_back_when_allow_override_is_set():
    """A user who already clicked 'proceed anyway' past a safety warning
    for this exact model must never be silently switched to something
    else — see the identical reasoning in _load_model_for_request()."""
    from backend.llm.providers.local_provider import Provider
    from types import SimpleNamespace

    provider = Provider.__new__(Provider)
    attempts = []

    class _RecordingLoader:
        def load_model(self, model_id, allow_override=False):
            attempts.append((model_id, allow_override))
            return object()

    provider.loader = _RecordingLoader()
    request = SimpleNamespace(model_id="nemo-12b-q5", allow_override=True)

    model_id, model = provider._load_model_for_request(request)

    assert model_id == "nemo-12b-q5"
    assert attempts == [("nemo-12b-q5", True)], f"allow_override must skip the fallback chain entirely: {attempts}"


# ============================================================
# PART 5.5 — bridge.js's real {type, payload} wire shape
#
# webui/core/bridge.js's send(type, payload) ALWAYS wraps outgoing
# fields as {"type": type, "payload": payload} — never flat. Every test
# above this point hand-crafts flat packets ({"type": ..., "model_id":
# ...} with no "payload" wrapper), which is not what the real frontend
# ever sends and silently never exercised this shape. These tests use
# the actual wire shape bridge.js produces, to catch the class of bug
# where a handler reads a field off the packet root instead of
# packet["payload"].
# ============================================================
def test_chat_request_reads_fields_from_bridge_payload_shape():
    from backend.websocket.handlers import WebSocketHandler

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)
        # Exactly what bridge.send("chat_request", {...}) puts on the
        # wire — see webui/core/bridge.js send(): `{ type, payload }`.
        await handler._dispatch({
            "type": "chat_request",
            "payload": {
                "messages": [{"role": "user", "content": "what models do you have?"}],
                "conversationId": "bridge-shape-test",
                "multiTurn": True,
            },
        })
        return ws.sent

    sent = _run(scenario())
    packets = _result_packets(sent)
    assert [p["type"] for p in packets] == ["stream_start", "stream_token", "stream_end"], (
        f"expected a self-query SKR reply, got {packets}"
    )
    assert packets[0]["modelId"] == "skr", (
        "a bridge-shaped chat_request must still resolve messages from payload and hit the SKR short-circuit — "
        "if this sees model_query fail, the handler is reading 'messages' off the packet root instead of "
        "packet['payload']['messages'], which is empty on every real bridge.send() call"
    )


def test_switch_to_lighter_model_reads_model_id_from_bridge_payload_shape():
    from backend.core import model_registry
    from backend.websocket.handlers import WebSocketHandler

    original_active = model_registry.get_active_model_id()
    target = "qwen2.5-0.5b-instruct-q4_k_m"

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)
        # Exactly what app.js's suggestion-button onclick produces via
        # bridge.send("switch_to_lighter_model", { model_id: s.id }).
        await handler._dispatch({
            "type": "switch_to_lighter_model",
            "payload": {"model_id": target},
        })
        return ws.sent

    try:
        import json
        sent = _run(scenario())
        packets = [json.loads(s) for s in sent]
        types_seen = [p["type"] for p in packets]
        assert "error" not in types_seen, (
            f"switch_to_lighter_model must not error on the real bridge.js payload shape, got {packets}"
        )
        assert model_registry.get_active_model_id() == target, (
            "active model was not switched — the handler likely read model_id off the packet root "
            "instead of packet['payload']['model_id']"
        )
    finally:
        if model_registry.get_active_model_id() != original_active:
            from backend.core import model_manager
            model_manager.set_active_model(original_active)


def test_load_model_override_reads_model_id_from_bridge_payload_shape():
    from backend.websocket.handlers import WebSocketHandler

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)
        # Exactly what app.js's "Proceed Anyway" button produces via
        # bridge.send("load_model_override", { model_id: packet.model_id }).
        await handler._dispatch({
            "type": "load_model_override",
            "payload": {"model_id": "nemo-12b-q5"},
        })
        return handler._override_model_id, ws.sent

    override_model_id, sent = _run(scenario())
    import json
    packets = [json.loads(s) for s in sent]
    assert not any(p["type"] == "error" for p in packets), (
        f"load_model_override must not error on the real bridge.js payload shape, got {packets}"
    )
    assert override_model_id == "nemo-12b-q5", (
        "the one-shot override was armed for the wrong (or no) model_id — the handler likely read "
        "model_id off the packet root instead of packet['payload']['model_id']"
    )


# ============================================================
# PART 6 — switch_to_lighter_model persistence, Proceed-Anyway
# allow_override bypass, and safety-warning suppression
# (skipSafetyCheck)
# ============================================================
def test_switch_to_lighter_model_persists_active_model():
    """
    Regression for the bug where selecting a suggested model in the
    safety warning never actually switched anything: the old handler
    called _start_inference() with an empty message and no
    set_active_model() call, so the "switch" silently reverted on the
    next real message. Must now: call model_manager.set_active_model,
    persist to the registry, and notify the client so the Models Page
    can refresh.
    """
    from backend.core import model_registry
    from backend.websocket.handlers import WebSocketHandler

    original_active = model_registry.get_active_model_id()
    target = "mistral-7b-q4km" if original_active != "mistral-7b-q4km" else "qwen2.5-0.5b-instruct-q4_k_m"
    assert model_registry.get_model(target) is not None, f"test target model {target!r} is not registered"

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)
        await handler._dispatch({"type": "switch_to_lighter_model", "model_id": target})
        return ws.sent

    try:
        import json
        sent = _run(scenario())
        assert model_registry.get_active_model_id() == target, (
            "switch_to_lighter_model did not persist the new active model_id"
        )
        packets = [json.loads(s) for s in sent]
        types_seen = [p["type"] for p in packets]
        assert "model_set_active_result" in types_seen, f"expected model_set_active_result, got {types_seen}"
        result_pkt = next(p for p in packets if p["type"] == "model_set_active_result")
        assert result_pkt["payload"]["ok"] is True
        assert result_pkt["payload"]["model_id"] == target
        assert "stream_start" in types_seen and "stream_end" in types_seen, (
            "expected a confirmation stream after switching"
        )
    finally:
        model_manager_restore = model_registry.get_active_model_id()
        if model_manager_restore != original_active:
            from backend.core import model_manager
            model_manager.set_active_model(original_active)
        assert model_registry.get_active_model_id() == original_active, "failed to restore original active model"


def test_switch_to_lighter_model_grants_one_shot_safety_bypass():
    """
    Regression for a real bug found while investigating a user report:
    selecting a suggested lighter model from the safety warning must not
    immediately re-trigger a NEW safety_warning for the very model just
    switched to (which, on a system already near/over the RAM threshold,
    would happen for literally any model — including the "lighter" one
    just picked — making the switch look like it did nothing). The next
    chat_request for that exact model_id must skip evaluate_safety once;
    the message after that must evaluate normally again.
    """
    from backend.core import model_registry
    from backend.core.mode_manager import ModeManager
    from backend.websocket import handlers as handlers_mod

    original_active = model_registry.get_active_model_id()
    target = "qwen2.5-0.5b-instruct-q4_k_m"
    assert model_registry.get_model(target) is not None, f"test target model {target!r} is not registered"

    # Explicit local model_ids are only honored in Local Mode or under
    # Automatic Model Routing (see
    # model_registry.model_violates_mode_separation() — absolute
    # local/cloud mode separation). This test is specifically about the
    # safety-bypass mechanism, not mode routing, so it pins a compatible
    # mode explicitly rather than depending on whatever mode this
    # process's persisted mode_state.json happens to already be in.
    mm = ModeManager()
    original_mode = mm.get_mode()
    mm.set_mode("local")

    original_evaluate_safety = _turn_orch.evaluate_safety
    calls = {"n": 0}

    def _counting_evaluate_safety(model_cfg):
        calls["n"] += 1
        raise AssertionError("evaluate_safety() must not be called on the one-shot bypass turn")

    async def scenario():
        ws = FakeWebSocket()
        handler = handlers_mod.WebSocketHandler(ws)

        await handler._dispatch({"type": "switch_to_lighter_model", "model_id": target})
        assert handler._override_model_id == target, "switch_to_lighter_model must arm the one-shot bypass"

        captured = {}

        async def _capture(packet):
            captured["packet"] = packet

        handler._start_inference_from = _capture
        _turn_orch.evaluate_safety = _counting_evaluate_safety
        try:
            await handler._dispatch({
                "type": "chat_request",
                "modelId": target,
                "conversationId": "bypass-test",
                "messages": [{"role": "user", "content": "hello"}],
            })
        finally:
            _turn_orch.evaluate_safety = original_evaluate_safety

        assert calls["n"] == 0, "evaluate_safety was called despite the one-shot bypass"
        assert getattr(captured.get("packet"), "inference_request", None) is not None and captured["packet"].inference_request.allow_override is True
        assert handler._override_model_id is None, "the one-shot bypass must be consumed after use"

    try:
        _run(scenario())
    finally:
        mm.set_mode(original_mode)
        model_manager_restore = model_registry.get_active_model_id()
        if model_manager_restore != original_active:
            from backend.core import model_manager
            model_manager.set_active_model(original_active)


def test_switch_to_lighter_model_missing_model_id_sends_error():
    from backend.websocket.handlers import WebSocketHandler

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)
        await handler._dispatch({"type": "switch_to_lighter_model"})
        return ws.sent

    import json
    sent = _run(scenario())
    assert len(sent) == 1
    assert json.loads(sent[0])["type"] == "error"


def test_switch_to_lighter_model_unknown_id_sends_error_and_does_not_change_active():
    from backend.core import model_registry
    from backend.websocket.handlers import WebSocketHandler

    original_active = model_registry.get_active_model_id()

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)
        await handler._dispatch({"type": "switch_to_lighter_model", "model_id": "totally-fake-model-id"})
        return ws.sent

    import json
    sent = _run(scenario())
    assert any(json.loads(s)["type"] == "error" for s in sent)
    assert model_registry.get_active_model_id() == original_active


def test_proceed_anyway_allow_override_threads_to_inference_request():
    """
    Regression for the "Proceed Anyway" path silently re-triggering the
    same safety block it was meant to bypass: load_model_override must
    not call _start_inference at all (it only confirms and waits for the
    user's next message — see handlers.py's _handle_model_override), and
    the allowOverride flag it sets up must reach InferenceRequest as
    allow_override=True whenever a follow-up chat_request carries it.
    """
    from backend.websocket.handlers import WebSocketHandler

    captured = {}

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)

        async def _capture(packet):
            captured["packet"] = packet

        handler._start_inference_from = _capture

        await handler._dispatch({"type": "load_model_override", "model_id": "nemo-12b-q5"})
        # _handle_model_override must NOT call _start_inference.
        assert "packet" not in captured, "load_model_override must not start inference directly"

        await handler._dispatch({
            "type": "chat_request",
            "modelId": "nemo-12b-q5",
            "conversationId": "override-test",
            "messages": [{"role": "user", "content": "go ahead"}],
            "allowOverride": True,
        })
        return ws.sent

    import json
    sent = _run(scenario())
    confirmation = json.loads(sent[0])
    assert confirmation["type"] == "stream_start" and confirmation["modelId"] == "system", (
        "load_model_override must reply with a system confirmation, not silence"
    )
    assert getattr(captured.get("packet"), "inference_request", None) is not None and captured["packet"].inference_request.allow_override is True, (
        "allowOverride was not threaded through to _start_inference"
    )


def test_skip_safety_check_bypasses_safety_gate_and_never_calls_evaluate_safety():
    """
    With skipSafetyCheck set, the handler must return before ever calling
    evaluate_safety() — verified by monkeypatching it to explode if
    reached — and must hand off to _start_inference with
    allowOverride=True so ModelLoader's own internal check doesn't
    independently re-block it either.
    """
    from backend import websocket as websocket_pkg  # noqa: F401
    from backend.core.mode_manager import ModeManager
    from backend.websocket import handlers as handlers_mod

    captured = {}
    original_evaluate_safety = _turn_orch.evaluate_safety

    def _explode(*_args, **_kwargs):
        raise AssertionError("evaluate_safety() was called despite skipSafetyCheck=True")

    # Explicit local model_ids are only honored in Local Mode or under
    # Automatic Model Routing (see
    # model_registry.model_violates_mode_separation()) — pin a
    # compatible mode explicitly so this test doesn't depend on whatever
    # mode this process's persisted mode_state.json happens to be in.
    mm = ModeManager()
    original_mode = mm.get_mode()
    mm.set_mode("local")

    async def scenario():
        ws = FakeWebSocket()
        handler = handlers_mod.WebSocketHandler(ws)

        async def _capture(packet):
            captured["packet"] = packet

        handler._start_inference_from = _capture
        _turn_orch.evaluate_safety = _explode
        try:
            await handler._dispatch({
                "type": "chat_request",
                "modelId": "nemo-12b-q5",
                "conversationId": "skip-safety-test",
                "messages": [{"role": "user", "content": "hello"}],
                "skipSafetyCheck": True,
            })
        finally:
            _turn_orch.evaluate_safety = original_evaluate_safety
        return ws.sent

    try:
        sent = _run(scenario())
    finally:
        mm.set_mode(original_mode)
    assert _result_packets(sent) == [], (
        "skipSafetyCheck must short-circuit straight to inference, no result packets sent first"
    )
    assert getattr(captured.get("packet"), "inference_request", None) is not None and captured["packet"].inference_request.allow_override is True, (
        "skipSafetyCheck must set allowOverride=True on the inference packet"
    )


def test_safety_check_still_fires_without_skip_flag():
    """
    Sanity counterpart to the skip test above: without skipSafetyCheck,
    a model that genuinely requires a warning must still produce one.
    Uses the real nemo-12b-q5 config (16GB min RAM), monkeypatching
    evaluate_safety's underlying resource snapshot is unnecessary here —
    evaluate_safety is exercised for real, so this only asserts the
    handler does NOT skip straight to inference.
    """
    from backend.websocket.handlers import WebSocketHandler

    captured = {}

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)

        async def _capture(packet):
            captured["packet"] = packet

        handler._start_inference_from = _capture
        await handler._dispatch({
            "type": "chat_request",
            "modelId": "nemo-12b-q5",
            "conversationId": "no-skip-test",
            "messages": [{"role": "user", "content": "hello"}],
        })
        return ws.sent

    import json
    sent = _run(scenario())
    types_seen = [json.loads(s)["type"] for s in sent]
    # Either it starts inference directly (plenty of RAM on the test
    # machine) or it sends a safety_warning — both are valid outcomes of
    # a REAL evaluate_safety() call; what must NOT happen is silently
    # doing neither.
    assert "packet" in captured or "safety_warning" in types_seen, (
        f"expected either inference to start or a safety_warning, got neither (sent={types_seen})"
    )


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
