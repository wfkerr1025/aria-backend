# backend/tests/routing_and_keys_tests.py
#
# Regression tests for the newer feature set:
#   - key_manager: provider API keys in OS secure storage (real Windows
#     Credential Manager round-trip on this machine), AES-encrypted
#     module keys with the master key itself in OS secure storage
#   - mode_manager: Auto/Local/Cloud mode persists across ModeManager
#     instances (i.e. across reconnects/restarts), not just in-memory
#   - provider_router: auto mode re-evaluates every request rather than
#     collapsing into a fixed mode after the first message
#   - conversation_manager: model_switch_intent detection + resolution,
#     including false-positive guards ("use your best judgment" must
#     NOT be treated as a switch)
#   - the live dispatch path: a natural-language "switch to X" chat
#     message actually switches the model/mode, with no model invoked
#     for the confirmation
#   - the new IPC endpoints: providers_list_request, provider_key_set/
#     delete_request, modules_list_request, module_key_set/delete_request
#
# Self-contained, plain-assert, no test framework — same convention as
# backend/tests/skr_and_ipc_tests.py. Every test that touches real
# persisted state (keyring, models.json, mode_state.json) restores it in
# a finally block. Run directly:
#
#   python backend/tests/routing_and_keys_tests.py

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, raw: str):
        self.sent.append(json.loads(raw))


def _run(coro):
    return asyncio.run(coro)


# ============================================================
# key_manager — provider keys (OS secure storage)
# ============================================================
def test_provider_key_round_trip_via_os_secure_storage():
    from backend.core import key_manager

    provider = "custom_http"  # a real entry in PROVIDER_ENV_VARS, used only for this test
    env_var = key_manager.PROVIDER_ENV_VARS[provider]
    original_env = os.environ.get(env_var)

    try:
        assert key_manager.get_provider_key(provider) is None
        assert key_manager.set_provider_key(provider, "test-secret-key-12345")
        assert key_manager.get_provider_key(provider) == "test-secret-key-12345"
        assert os.environ.get(env_var) == "test-secret-key-12345", "sync to os.environ failed"

        status = key_manager.list_configured_providers()
        assert status[provider] is True
        assert status["anthropic"] is False, "an unrelated provider must not show as configured"

        assert key_manager.delete_provider_key(provider)
        assert key_manager.get_provider_key(provider) is None
    finally:
        # Real Windows Credential Manager entries — must not leave test
        # data behind even if an assertion above failed midway.
        try:
            key_manager.delete_provider_key(provider)
        except Exception:
            pass
        if original_env is None:
            os.environ.pop(env_var, None)
        else:
            os.environ[env_var] = original_env


def test_set_provider_key_rejects_unknown_provider_and_empty_key():
    from backend.core import key_manager

    assert key_manager.set_provider_key("not_a_real_provider", "x") is False
    assert key_manager.set_provider_key("openai", "") is False


def test_provider_key_reload_updates_live_provider_instance():
    """
    Provider.__init__ reads its API key via os.getenv(...) exactly once
    at import/instantiation time — set_provider_key() must trigger
    provider_registry.reload_providers() so a key entered through the
    settings UI takes effect without a process restart, not just update
    os.environ in a vacuum.
    """
    from backend.core import key_manager
    from backend.llm.providers import provider_registry

    provider = "custom_http"
    env_var = key_manager.PROVIDER_ENV_VARS[provider]
    original_env = os.environ.get(env_var)

    try:
        key_manager.delete_provider_key(provider)
        provider_registry.reload_providers()
        instance_before = provider_registry.get_provider(provider)
        assert instance_before is not None, "custom_http provider must load"
        assert getattr(instance_before, "api_key", None) in ("", None)

        key_manager.set_provider_key(provider, "live-reload-test-key")
        instance_after = provider_registry.get_provider(provider)
        assert instance_after.api_key == "live-reload-test-key", (
            "the freshly-reloaded provider instance must carry the new key"
        )
    finally:
        key_manager.delete_provider_key(provider)
        if original_env is None:
            os.environ.pop(env_var, None)
        else:
            os.environ[env_var] = original_env
        provider_registry.reload_providers()


# ============================================================
# key_manager — module keys (AES-encrypted file, master key in OS storage)
# ============================================================
def test_module_key_round_trip_and_file_is_actually_encrypted():
    from backend.core import key_manager, module_manager

    module_name = "__test_module__"

    try:
        assert module_manager.get_module_key(module_name) is None

        result = module_manager.set_module_key(module_name, "plaintext-secret-value")
        assert result["ok"] is True

        assert module_manager.get_module_key(module_name) == "plaintext-secret-value"

        with open(key_manager._MODULE_KEYS_PATH, "r", encoding="utf-8") as f:
            raw_disk_contents = f.read()
        assert "plaintext-secret-value" not in raw_disk_contents, (
            "module key must be AES-encrypted on disk, not stored in plaintext"
        )

        names = module_manager.list_modules()
        assert any(m["name"] == module_name and m["configured"] for m in names)

        del_result = module_manager.delete_module_key(module_name)
        assert del_result["ok"] is True
        assert module_manager.get_module_key(module_name) is None
    finally:
        try:
            module_manager.delete_module_key(module_name)
        except Exception:
            pass


def test_module_key_set_rejects_missing_fields():
    from backend.core import module_manager

    assert module_manager.set_module_key("", "x")["ok"] is False
    assert module_manager.set_module_key("weather", "")["ok"] is False


def _snapshot_module_keys_file():
    from backend.core import key_manager
    if not os.path.exists(key_manager._MODULE_KEYS_PATH):
        return None
    with open(key_manager._MODULE_KEYS_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _restore_module_keys_file(snapshot):
    from backend.core import key_manager
    if snapshot is None:
        if os.path.exists(key_manager._MODULE_KEYS_PATH):
            os.remove(key_manager._MODULE_KEYS_PATH)
        return
    with open(key_manager._MODULE_KEYS_PATH, "w", encoding="utf-8") as f:
        f.write(snapshot)


def test_known_module_gets_a_real_empty_entry_materialized_in_the_key_store():
    """
    Self-discovered modules (module_manager.KNOWN_MODULES) must behave
    exactly like user-added ones: list_modules() must materialize a REAL
    {"module_name": ..., "api_key": None} entry in the encrypted key
    store for "weather" if it doesn't already have one — not just report
    it as present via an on-the-fly KNOWN_MODULES union.
    """
    from backend.core import key_manager, module_manager

    snapshot = _snapshot_module_keys_file()
    try:
        # Start from a state where "weather" has no entry at all.
        raw = key_manager._load_module_keys_raw()
        raw.pop("weather", None)
        key_manager._save_module_keys_raw(raw)

        module_manager.list_modules()

        raw_after = key_manager._load_module_keys_raw()
        assert "weather" in raw_after, "list_modules() must materialize an empty entry for a KNOWN_MODULES member"
        assert raw_after["weather"] == {"module_name": "weather", "api_key": None}
    finally:
        _restore_module_keys_file(snapshot)


def test_list_modules_includes_both_self_discovered_and_user_added_modules():
    from backend.core import module_manager

    snapshot = _snapshot_module_keys_file()
    try:
        result = module_manager.set_module_key("__user_added_test_module__", "some-key")
        assert result["ok"] is True

        modules = module_manager.list_modules()
        names = {m["name"] for m in modules}
        assert "weather" in names, "self-discovered KNOWN_MODULES entries must be in the list"
        assert "__user_added_test_module__" in names, "user-added modules must still be in the list"

        user_entry = next(m for m in modules if m["name"] == "__user_added_test_module__")
        assert user_entry["configured"] is True
    finally:
        module_manager.delete_module_key("__user_added_test_module__")
        _restore_module_keys_file(snapshot)


def test_deleting_a_self_discovered_module_removes_it_then_it_reappears_empty_on_next_list():
    from backend.core import key_manager, module_manager

    snapshot = _snapshot_module_keys_file()
    try:
        module_manager.set_module_key("weather", "temp-weather-key")
        assert module_manager.get_module_key("weather") == "temp-weather-key"

        del_result = module_manager.delete_module_key("weather")
        assert del_result["ok"] is True
        # Immediately after deletion, before any list_modules() call, the
        # entry is genuinely gone from the store.
        assert "weather" not in key_manager._load_module_keys_raw()

        # The next list_modules() call re-materializes it as empty — a
        # known module never truly disappears, it just resets to
        # unconfigured (same UX class as the Cloud Providers list always
        # showing every known provider).
        modules = module_manager.list_modules()
        weather_entry = next(m for m in modules if m["name"] == "weather")
        assert weather_entry["configured"] is False
        assert module_manager.get_module_key("weather") is None
    finally:
        module_manager.delete_module_key("weather")
        _restore_module_keys_file(snapshot)


def test_deleting_a_user_added_module_does_not_reappear():
    from backend.core import module_manager

    snapshot = _snapshot_module_keys_file()
    try:
        module_manager.set_module_key("__never_reappears_test_module__", "x")
        module_manager.delete_module_key("__never_reappears_test_module__")

        modules = module_manager.list_modules()
        names = {m["name"] for m in modules}
        assert "__never_reappears_test_module__" not in names, (
            "a user-added (non-KNOWN_MODULES) module must stay gone after deletion"
        )
    finally:
        module_manager.delete_module_key("__never_reappears_test_module__")
        _restore_module_keys_file(snapshot)


def test_legacy_flat_module_key_format_is_transparently_migrated():
    """
    Before self-discovered modules got real key-store entries, the file
    was a flat {module_name: <encrypted str>} map. Any file written by an
    older build must still work — get_module_key() must still decrypt it,
    and it must be silently upgraded to the nested format on next save.
    """
    from cryptography.fernet import Fernet
    from backend.core import key_manager

    snapshot = _snapshot_module_keys_file()
    try:
        fernet = Fernet(key_manager._get_or_create_master_key())
        legacy_encrypted = fernet.encrypt(b"legacy-plaintext-value").decode("utf-8")
        with open(key_manager._MODULE_KEYS_PATH, "w", encoding="utf-8") as f:
            json.dump({"__legacy_test_module__": legacy_encrypted}, f)

        assert key_manager.get_module_key("__legacy_test_module__") == "legacy-plaintext-value"

        migrated = key_manager._load_module_keys_raw()
        assert migrated["__legacy_test_module__"] == {
            "module_name": "__legacy_test_module__",
            "api_key": legacy_encrypted,
        }
    finally:
        _restore_module_keys_file(snapshot)


# ============================================================
# mode_manager — persistence across instances (across "reconnects")
# ============================================================
def test_mode_persists_across_new_mode_manager_instances():
    from backend.core.mode_manager import ModeManager

    original = ModeManager()
    original_mode = original.get_mode()
    original_provider = original.get_cloud_provider()

    try:
        m1 = ModeManager()
        m1.set_mode("local")
        m2 = ModeManager()  # simulates a brand-new WebSocket connection
        assert m2.get_mode() == "local", "mode must persist across ModeManager instances"

        m2.set_mode("cloud")
        m2.set_cloud_provider("anthropic")
        m3 = ModeManager()
        assert m3.get_mode() == "cloud"
        assert m3.get_cloud_provider() == "anthropic"
    finally:
        restore = ModeManager()
        restore.set_mode(original_mode)
        restore.set_cloud_provider(original_provider)


def test_automatic_mode_does_not_collapse_after_one_resolve_call():
    """
    Regression for a real bug found while wiring mode persistence:
    ProviderRouter.resolve()'s automatic branch used to call
    self.mode_manager.set_mode("local"/"cloud") after every AutoSelector
    decision — harmless when mode_manager was in-memory-only, but once
    it persists to disk that would have permanently pinned "automatic" to
    whatever the very first message happened to route to.
    """
    from backend.core.mode_manager import ModeManager
    from backend.core.provider_router import ProviderRouter

    original = ModeManager()
    original_mode = original.get_mode()

    try:
        mm = ModeManager()
        mm.set_mode("automatic")
        router = ProviderRouter(mode_manager=mm)

        router.resolve(None, prompt="hello")
        assert mm.get_mode() == "automatic", "automatic mode must not be overwritten by a single resolve() call"

        router.resolve(None, prompt="write a detailed story about a dragon")
        assert mm.get_mode() == "automatic", "automatic mode must still not be overwritten after a second call"
    finally:
        restore = ModeManager()
        restore.set_mode(original_mode)


# ============================================================
# conversation_manager — model_switch_intent
# ============================================================
def test_model_switch_intent_detects_and_resolves_real_targets():
    from backend.core.conversation_manager import (
        detect_intent, INTENT_MODEL_SWITCH,
        detect_model_switch_target, resolve_model_switch_target,
    )

    cases = [
        ("switch to mistral-7b-q4km", {"kind": "model", "model_id": "mistral-7b-q4km"}),
        ("use qwen2.5-0.5b-instruct-q4_k_m", {"kind": "model", "model_id": "qwen2.5-0.5b-instruct-q4_k_m"}),
        ("switch to local mode", {"kind": "mode", "mode": "local"}),
        ("change to local mode", {"kind": "mode", "mode": "local"}),
        ("go back to automatic model selection", {"kind": "mode", "mode": "automatic"}),
        ("switch to cloud mode", {"kind": "mode", "mode": "cloud"}),
        ("use openai", {"kind": "provider", "provider": "openai"}),
        ("set model to phi-3-mini-4k-instruct-q4", {"kind": "model", "model_id": "phi-3-mini-4k-instruct-q4"}),
    ]
    for phrase, expected in cases:
        intent = detect_intent(phrase)
        assert intent == INTENT_MODEL_SWITCH, f"{phrase!r} classified as {intent!r}, expected model_switch_intent"

        raw = detect_model_switch_target(phrase.lower())
        resolved = resolve_model_switch_target(raw)
        assert resolved == expected, f"{phrase!r} resolved to {resolved}, expected {expected}"


def test_model_switch_intent_does_not_misfire_on_ordinary_sentences():
    from backend.core.conversation_manager import detect_intent, INTENT_MODEL_SWITCH

    non_switch_phrases = [
        "use your best judgment here",
        "use the weather tool to check the forecast",
        "switch to a different approach for this problem",
        "can you use python for this",
        "switch the order of these two paragraphs",
    ]
    for phrase in non_switch_phrases:
        intent = detect_intent(phrase)
        assert intent != INTENT_MODEL_SWITCH, f"{phrase!r} was misclassified as model_switch_intent"


# ============================================================
# Live dispatch — a real "switch to X" chat message actually switches
# ============================================================
def test_live_chat_request_switches_model_with_no_provider_call():
    from backend.core import model_registry
    from backend.websocket.handlers import WebSocketHandler

    original_active = model_registry.get_active_model_id()
    target = "qwen2.5-0.5b-instruct-q4_k_m" if original_active != "qwen2.5-0.5b-instruct-q4_k_m" else "mistral-7b-q4km"
    assert model_registry.get_model(target) is not None

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)

        async def _explode(*_a, **_kw):
            raise AssertionError("_start_inference() was called for a model_switch_intent — it must never invoke a provider")

        handler._start_inference = _explode

        await handler._dispatch({
            "type": "chat_request",
            "payload": {
                "messages": [{"role": "user", "content": f"switch to {target}"}],
                "conversationId": "switch-intent-live-test",
                "multiTurn": True,
            },
        })
        return ws.sent

    try:
        sent = _run(scenario())
        types_seen = [p["type"] for p in sent]
        assert "model_set_active_result" in types_seen, f"expected model_set_active_result, got {types_seen}"
        result_pkt = next(p for p in sent if p["type"] == "model_set_active_result")
        assert result_pkt["payload"]["model_id"] == target
        assert model_registry.get_active_model_id() == target, "the switch must actually persist"
        assert "stream_start" in types_seen and "stream_end" in types_seen
    finally:
        if model_registry.get_active_model_id() != original_active:
            from backend.core import model_manager
            model_manager.set_active_model(original_active)


def test_live_chat_request_switches_mode_with_no_provider_call():
    from backend.core.mode_manager import ModeManager
    from backend.websocket.handlers import WebSocketHandler

    original_mode = ModeManager().get_mode()

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)

        async def _explode(*_a, **_kw):
            raise AssertionError("_start_inference() was called for a model_switch_intent — it must never invoke a provider")

        handler._start_inference = _explode

        await handler._dispatch({
            "type": "chat_request",
            "payload": {
                "messages": [{"role": "user", "content": "switch to local mode"}],
                "conversationId": "mode-switch-intent-live-test",
                "multiTurn": True,
            },
        })
        return ws.sent

    try:
        sent = _run(scenario())
        types_seen = [p["type"] for p in sent]
        assert "mode_set_result" in types_seen, f"expected mode_set_result, got {types_seen}"
        result_pkt = next(p for p in sent if p["type"] == "mode_set_result")
        assert result_pkt["payload"] == {"ok": True, "mode": "local"}
        assert ModeManager().get_mode() == "local", "the mode switch must actually persist"
    finally:
        ModeManager().set_mode(original_mode)


# ============================================================
# provider_router — mode-aware routing is actually reachable from live
# chat, and Cloud Mode only ever hands back a configured provider
# ============================================================
def test_auto_mode_is_reachable_from_live_chat_request():
    """
    Regression for the actual root cause of "Auto mode does nothing":
    _handle_chat_request used to ALWAYS resolve a concrete model_id
    (packet.get("modelId") or get_default_model_id()) and hand it to
    ProviderRouter.resolve() as an explicit override — which always wins
    over mode-based routing, on every single request, regardless of
    what mode was selected. AutoSelector.select_provider() was correct
    but permanently unreachable. Fixed by passing model_id=None through
    to _start_inference()/ProviderRouter.resolve() when mode is auto/
    cloud and no explicit modelId was requested.
    """
    from backend.core.mode_manager import ModeManager
    from backend.core import auto_selector as auto_selector_mod
    from backend.websocket.handlers import WebSocketHandler

    original_mode = ModeManager().get_mode()
    calls = []
    original_select = auto_selector_mod.AutoSelector.select_provider

    def spy_select(self, prompt):
        calls.append(prompt)
        return original_select(self, prompt)

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)

        captured = {}

        async def _capture(result):
            captured["packet"] = result.inference_request

        handler._start_inference_from = _capture

        await handler._dispatch({
            "type": "chat_request",
            "payload": {
                "messages": [{"role": "user", "content": "hi"}],
                "conversationId": "auto-reachability-test",
                "multiTurn": True,
            },
        })
        return captured

    try:
        ModeManager().set_mode("automatic")
        auto_selector_mod.AutoSelector.select_provider = spy_select

        captured = _run(scenario())

        assert "packet" in captured, "the turn never reached inference at all"
        assert captured["packet"].model_id is None, (
            "Automatic Model Routing with no explicit request must pass model_id=None through to inference"
        )
    finally:
        auto_selector_mod.AutoSelector.select_provider = original_select
        ModeManager().set_mode(original_mode)


def test_local_mode_still_resolves_an_explicit_model_id():
    """
    Non-regression companion to the test above: Local Mode must keep
    resolving to the persisted active model explicitly (not None) —
    that's what keeps the pre-flight RAM safety check meaningful for
    local inference, which is exactly where it matters.

    Uses skipSafetyCheck so this doesn't flake on real system RAM
    pressure (this dev machine's usage swings widely enough to
    genuinely trigger evaluate_safety() sometimes) — the safety-check
    behavior itself is covered separately by test_safety_check_still_
    fires_without_skip_flag and test_skip_safety_check_bypasses_
    safety_gate_and_never_calls_evaluate_safety in skr_and_ipc_tests.py;
    this test only cares whether model_id resolution itself is explicit.
    """
    from backend.core.mode_manager import ModeManager
    from backend.websocket.handlers import WebSocketHandler

    original_mode = ModeManager().get_mode()

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)

        captured = {}

        async def _capture(result):
            captured["packet"] = result.inference_request

        handler._start_inference_from = _capture

        await handler._dispatch({
            "type": "chat_request",
            "payload": {
                "messages": [{"role": "user", "content": "hi"}],
                "conversationId": "local-mode-explicit-test",
                "multiTurn": True,
                "skipSafetyCheck": True,
            },
        })
        return captured

    try:
        ModeManager().set_mode("local")
        captured = _run(scenario())
        assert getattr(captured.get("packet"), "model_id", None) is not None, (
            "Local mode must still resolve an explicit model_id"
        )
    finally:
        ModeManager().set_mode(original_mode)


def test_cloud_mode_falls_back_to_a_configured_provider():
    """
    Cloud Mode must never hand back a provider with no key configured —
    it would only fail once the real API call is made. Verifies the
    fallback chain: requested provider unconfigured -> next configured
    provider in AutoSelector.cloud_rank order -> None if nothing at all
    is configured (never a silent, doomed-to-fail keyless provider).
    """
    import os
    from backend.core.mode_manager import ModeManager
    from backend.core.provider_router import ProviderRouter
    from backend.core import key_manager
    from backend.llm.providers import provider_registry

    original_mode = ModeManager().get_mode()
    env_var = key_manager.PROVIDER_ENV_VARS["anthropic"]
    original_env = os.environ.get(env_var)

    try:
        key_manager.delete_provider_key("openai")
        key_manager.delete_provider_key("anthropic")

        mm = ModeManager()
        mm.set_mode("cloud")
        mm.set_cloud_provider("openai")
        router = ProviderRouter(mode_manager=mm)

        provider, model = router.resolve(None, prompt="hello")
        assert provider is None and model is None, "no providers configured must resolve to (None, None)"

        key_manager.set_provider_key("anthropic", "fallback-test-key")
        provider, model = router.resolve(None, prompt="hello")
        assert provider is not None
        assert provider.__class__.__module__.endswith("anthropic_wrapper"), (
            "must fall back to the configured provider, not the unconfigured requested one"
        )
    finally:
        key_manager.delete_provider_key("anthropic")
        if original_env is None:
            os.environ.pop(env_var, None)
        else:
            os.environ[env_var] = original_env
        provider_registry.reload_providers()
        ModeManager().set_mode(original_mode)
        ModeManager().set_cloud_provider(None)


def test_cloud_rank_names_match_known_providers():
    """
    Regression for a real typo bug found while wiring this: cloud_rank
    used to list "groq" (never a registered provider — the real wrapper
    loads as "grok") and omitted "azure" entirely, silently making both
    unreachable from Automatic Model Routing's cloud fallback.
    """
    from backend.core.auto_selector import AutoSelector
    from backend.core.key_manager import PROVIDER_ENV_VARS

    rank = AutoSelector().cloud_rank
    assert "grok" in rank and "groq" not in rank
    assert "azure" in rank
    assert set(rank) <= set(PROVIDER_ENV_VARS.keys()), (
        f"cloud_rank contains names that aren't real providers: {set(rank) - set(PROVIDER_ENV_VARS.keys())}"
    )


# ============================================================
# Explicit model override — permanent cross-mode pin ("switch to X"),
# cleared by a mode switch, reflected in self-knowledge answers
# ============================================================
def test_explicit_model_override_persists_and_wins_over_auto_routing():
    from backend.core.mode_manager import ModeManager
    from backend.core import model_registry, model_manager
    from backend.core import auto_selector as auto_selector_mod
    from backend.websocket.handlers import WebSocketHandler

    original_mode = ModeManager().get_mode()
    original_active = model_registry.get_active_model_id()
    target = "qwen2.5-0.5b-instruct-q4_k_m"
    calls = []
    original_select = auto_selector_mod.AutoSelector.select_provider

    def spy(self, prompt):
        calls.append(prompt)
        return original_select(self, prompt)

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)

        # Arm the override via the real switch-intent path.
        await handler._dispatch({
            "type": "chat_request",
            "payload": {
                "messages": [{"role": "user", "content": f"switch to {target}"}],
                "conversationId": "override-persist-test",
                "multiTurn": True,
            },
        })

        captured = {}

        async def _capture(result):
            captured["packet"] = result.inference_request

        handler._start_inference_from = _capture

        # An ordinary message afterward must use the pinned model, not
        # AutoSelector, even though mode is auto.
        #
        # skipSafetyCheck for the same reason the sibling test above gives:
        # evaluate_safety() reads live CPU/RAM, and the pinned model here
        # trips it whenever the machine is busy -- which short-circuits the
        # turn to a safety_warning and leaves `captured` empty. This test is
        # about model precedence, not the safety gate, and the gate has its
        # own coverage in skr_and_ipc_tests.py.
        await handler._dispatch({
            "type": "chat_request",
            "payload": {
                "messages": [{"role": "user", "content": "ordinary message"}],
                "conversationId": "override-persist-test",
                "multiTurn": True,
                "skipSafetyCheck": True,
            },
        })
        return captured

    try:
        ModeManager().set_mode("automatic")
        auto_selector_mod.AutoSelector.select_provider = spy

        captured = _run(scenario())

        assert ModeManager().get_explicit_model_override() == target
        assert getattr(captured.get("packet"), "model_id", None) == target
        assert len(calls) == 0, "AutoSelector must not run while an explicit override is pinned"
    finally:
        auto_selector_mod.AutoSelector.select_provider = original_select
        ModeManager().set_mode(original_mode)
        ModeManager().set_explicit_model_override(None)
        if model_registry.get_active_model_id() != original_active:
            model_manager.set_active_model(original_active)


def test_mode_switch_clears_explicit_model_override():
    from backend.core.mode_manager import ModeManager

    original_mode = ModeManager().get_mode()
    try:
        mm = ModeManager()
        mm.set_mode("automatic")
        mm.set_explicit_model_override("mistral-7b-q4km")
        assert mm.get_explicit_model_override() == "mistral-7b-q4km"

        mm.set_mode("local")
        assert mm.get_explicit_model_override() is None, "switching modes must clear the explicit override"
    finally:
        ModeManager().set_mode(original_mode)
        ModeManager().set_explicit_model_override(None)


def test_mode_query_phrases_do_not_shadow_model_query():
    """
    Regression: "mode" is a literal prefix of "model" ("which mode" is
    the first 10 characters of "which model"), so mode_query's
    mode-related phrase matching must use word boundaries — a naive
    substring check misclassified "tell me your models" as a mode query
    before model_query's fuzzy match ever got a chance, breaking
    self-query answers for that phrasing. "what/which mode" phrasing
    routes to the dedicated INTENT_MODE_QUERY (not
    INTENT_ENVIRONMENT_QUERY) — see that constant's comment in
    conversation_manager.py for why the two are kept separate.
    """
    from backend.core.conversation_manager import detect_intent, INTENT_MODEL_QUERY, INTENT_MODE_QUERY

    assert detect_intent("tell me your models") == INTENT_MODEL_QUERY
    assert detect_intent("what models do you have") == INTENT_MODEL_QUERY
    assert detect_intent("which model are you using") == INTENT_MODEL_QUERY
    assert detect_intent("what mode are you in") == INTENT_MODE_QUERY
    assert detect_intent("which mode are you using") == INTENT_MODE_QUERY


def test_self_knowledge_reports_mode_and_override():
    from backend.core.mode_manager import ModeManager
    from backend.core import self_knowledge as sk

    original_mode = ModeManager().get_mode()
    try:
        mm = ModeManager()
        mm.set_mode("automatic")
        mm.set_explicit_model_override("qwen2.5-0.5b-instruct-q4_k_m")

        snapshot = sk.build_snapshot(conversation_id="skr-mode-test")
        assert snapshot.routing_mode == "automatic"
        assert snapshot.explicit_model_override == "qwen2.5-0.5b-instruct-q4_k_m"

        answer = sk.answer_self_query("environment_query", snapshot)
        assert "automatic model selection" in answer.lower()
        assert "auto mode" not in answer.lower(), f"AUTO MODE must never appear: {answer!r}"
        assert "qwen2.5-0.5b-instruct-q4_k_m" in answer
        assert "pinned" in answer.lower()
    finally:
        ModeManager().set_mode(original_mode)
        ModeManager().set_explicit_model_override(None)


def test_self_knowledge_reports_cloud_fallback_when_provider_unconfigured():
    from backend.core.mode_manager import ModeManager
    from backend.core import key_manager
    from backend.core import self_knowledge as sk

    original_mode = ModeManager().get_mode()
    original_provider = ModeManager().get_cloud_provider()
    key_manager.delete_provider_key("openai")  # ensure genuinely unconfigured for this test

    try:
        mm = ModeManager()
        mm.set_mode("cloud")
        mm.set_cloud_provider("openai")

        snapshot = sk.build_snapshot(conversation_id="skr-fallback-test")
        assert snapshot.cloud_provider == "openai"
        assert snapshot.cloud_provider_configured is False

        answer = sk.answer_self_query("environment_query", snapshot)
        assert "falling back" in answer.lower()
    finally:
        ModeManager().set_mode(original_mode)
        ModeManager().set_cloud_provider(original_provider)


# ============================================================
# Weather / search tool execution (backend.core.tool_executor)
# ============================================================
def test_weather_intent_short_circuits_to_real_tool_no_model_call():
    """
    Batch 3.6: NL weather is routed through backend.core.weather_router's
    fusion-engine path (weather_nl.resolve_weather_reply()), not the
    retired single-provider tools.get_weather.get_weather() chain — the
    modelId sentinel is "weather-fusion", never the retired "weather".
    """
    from backend.websocket.handlers import WebSocketHandler
    from backend.core import weather_router
    from backend.core import open_meteo_provider
    from backend.core.weather_types import WeatherFusionResult, ProviderWeatherSample

    original_geocode = open_meteo_provider.geocode
    original_fuse = weather_router.weather_fusion.get_fused_weather

    open_meteo_provider.geocode = lambda location: {
        "lat": 37.5407, "lon": -77.4360, "name": "Richmond", "region": "Virginia", "country": "United States", "elevation": 50.0,
        "geocodeConfidence": "high",
    }
    weather_router.weather_fusion.get_fused_weather = lambda lat, lon, target_elevation=None: WeatherFusionResult(
        fusedTemperatureC=20.0, fusedConditionsText="Clear sky", fusedWindSpeedMps=2.2, fusedHumidityPercent=55.0,
        confidence="high", samples=[ProviderWeatherSample("open_meteo", 20.0, "Clear sky", 2.2, 55.0, 0)],
    )

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)

        async def _explode(*_a, **_kw):
            raise AssertionError("_start_inference() was called for a weather query — it must never invoke a model")

        handler._start_inference = _explode
        await handler._dispatch({
            "type": "chat_request",
            "payload": {
                "messages": [{"role": "user", "content": "what is the weather in Richmond VA"}],
                "conversationId": "weather-tool-test",
                "multiTurn": True,
            },
        })
        return ws.sent

    try:
        sent = _run(scenario())
    finally:
        open_meteo_provider.geocode = original_geocode
        weather_router.weather_fusion.get_fused_weather = original_fuse

    # Status packets are additive turn-indicator decoration carrying no
    # result (backend/core/turn_status.py); this test is about the answer
    # packets, so they are filtered rather than counted.
    sent = [p for p in sent if p.get("type") != "status"]
    assert [p["type"] for p in sent] == ["stream_start", "stream_token", "stream_end"]
    assert sent[0]["modelId"] == "weather-fusion", "must never be the retired 'weather' sentinel"
    assert "Richmond" in sent[1]["token"]
    assert "68" in sent[1]["token"]  # 20.0C -> 68F


def test_search_intent_runs_the_tool_and_then_answers_with_a_model():
    """
    A search request runs a real lookup and hands the result to the model.

    This test used to assert the opposite -- modelId "search", no model
    invoked, the tool's reply sent verbatim -- and that was correct until
    lookups became a planned step inside the reasoning turn. Keeping both
    would search twice; keeping only the bypass meant the model never saw
    what the search returned, which is how a question about a current
    price got answered from the model's weights.

    What has to stay true is the part that mattered: the tool really runs,
    and its output really reaches the prompt. Both are asserted below, so
    this remains a test about not fabricating an answer.
    """
    from backend.core import tool_registry as core_reg
    from backend.websocket.handlers import WebSocketHandler

    calls = []

    def fake_search(query):
        calls.append(query)
        return {"raw": {}, "reply": "SENTINEL-EVIDENCE: asyncio 3.12 release notes"}

    core_reg.register_tool(
        core_reg.ToolSchema(
            name="web_search", description="test double",
            parameters={"query": {"type": "string", "required": True}},
            permission=core_reg.PERMISSION_NETWORK,
        ),
        fake_search,
    )

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)

        captured = {}

        async def _capture(result):
            captured["result"] = result

        handler._start_inference_from = _capture

        await handler._dispatch({
            "type": "chat_request",
            "payload": {
                "messages": [{"role": "user", "content": "search for python asyncio tutorial"}],
                "conversationId": "search-tool-test",
                "multiTurn": True,
            },
        })
        return captured

    try:
        captured = _run(scenario())
    finally:
        core_reg.register_builtin_tools()

    result = captured.get("result")
    assert result is not None, "a search turn must reach the model"
    assert calls, "the planned lookup never reached the tool"
    assert "web_search" in result.metadata["tool_runs"]

    prompt = result.inference_request.messages[-1].content
    assert "SENTINEL-EVIDENCE" in prompt, (
        "the search ran but its result never reached the model"
    )


def test_weather_location_extraction():
    from backend.core.tool_executor import extract_weather_location

    assert extract_weather_location("what is the weather in Richmond VA") == "Richmond VA"
    assert extract_weather_location("weather for Tokyo") == "Tokyo"
    assert extract_weather_location("weather at Paris, France") == "Paris, France"


def test_a_trailing_time_qualifier_is_not_part_of_the_location():
    """
    The capture runs to the end of the sentence, so "weather in Tokyo
    right now" produced "Tokyo right now" -- which no geocoder resolves,
    so an ordinary question came back as "I couldn't get the weather for
    that location". The time is not part of the place.
    """
    from backend.core.tool_executor import extract_weather_location

    assert extract_weather_location("Weather in Tokyo right now") == "Tokyo"
    assert extract_weather_location("Weather in Paris today") == "Paris"
    assert extract_weather_location("Weather for London currently") == "London"
    assert extract_weather_location("weather in New York at the moment") == "New York"
    assert extract_weather_location("weather in Tokyo tomorrow") == "Tokyo"


def test_a_location_that_is_only_a_time_is_no_location():
    """"weather right now" names no place, so the caller must still ask."""
    from backend.core.tool_executor import extract_weather_location

    assert extract_weather_location("weather right now") is None


def test_a_place_whose_name_survives_the_strip():
    """Only a *trailing* qualifier goes, and only as a whole word."""
    from backend.core.tool_executor import extract_weather_location

    assert extract_weather_location("weather in Nowra") == "Nowra"
    assert extract_weather_location("weather in Todays Corner") == "Todays Corner"


def test_weather_reply_formats_error_gracefully():
    from backend.core.tool_executor import format_weather_reply

    reply = format_weather_reply({"status": "error", "error": "All providers failed"})
    assert "couldn't" in reply.lower()


def test_search_query_extraction_strips_common_prefixes():
    from backend.core.tool_executor import extract_search_query

    assert extract_search_query("search for python asyncio tutorial") == "python asyncio tutorial"
    assert extract_search_query("look up quantum computing") == "quantum computing"


def test_weather_module_key_syncs_to_env_and_used_by_tool():
    """
    A weather key entered via the settings/module UI must actually reach
    tools/get_weather.py's HTTP calls without a process restart — the
    same class of bug the LLM provider wrappers had (a frozen
    module-level constant read once at import time).
    """
    import os
    from backend.core import module_manager

    original_env = os.environ.get("WEATHERAPI_KEY")
    try:
        module_manager.delete_module_key("weather")
        os.environ.pop("WEATHERAPI_KEY", None)

        module_manager.set_module_key("weather", "test-weather-key-123")
        assert os.environ.get("WEATHERAPI_KEY") == "test-weather-key-123"

        from tools.get_weather import _weatherapi_key
        assert _weatherapi_key() == "test-weather-key-123"

        module_manager.delete_module_key("weather")
        assert os.environ.get("WEATHERAPI_KEY") is None
    finally:
        module_manager.delete_module_key("weather")
        if original_env is None:
            os.environ.pop("WEATHERAPI_KEY", None)
        else:
            os.environ["WEATHERAPI_KEY"] = original_env


# ============================================================
# Self-improvement suggestions
# ============================================================
def test_self_improvement_intent_and_real_suggestions():
    from backend.core.conversation_manager import detect_intent, INTENT_SELF_IMPROVEMENT_QUERY
    from backend.core import self_knowledge as sk, key_manager

    assert detect_intent("what could you do better") == INTENT_SELF_IMPROVEMENT_QUERY
    assert detect_intent("what is missing") == INTENT_SELF_IMPROVEMENT_QUERY

    key_manager.delete_provider_key("openai")
    key_manager.delete_provider_key("anthropic")
    suggestions = sk.suggest_improvements()
    assert any("cloud provider" in s.lower() or "openai" in s.lower() or "anthropic" in s.lower() for s in suggestions)

    snapshot = sk.build_snapshot(conversation_id="improve-test")
    answer = sk.answer_self_query(INTENT_SELF_IMPROVEMENT_QUERY, snapshot)
    assert isinstance(answer, str) and answer.strip()


# ============================================================
# Task-aware cloud provider preference (Anthropic=reasoning, OpenAI=code)
# ============================================================
def test_cloud_routing_prefers_anthropic_for_reasoning_and_openai_for_code():
    from backend.core.auto_selector import AutoSelector
    from backend.core import key_manager
    from backend.llm.providers import provider_registry

    try:
        key_manager.set_provider_key("openai", "test-openai-key")
        key_manager.set_provider_key("anthropic", "test-anthropic-key")

        selector = AutoSelector()
        long_code_prompt = "please write a python function to sort a list. " * 40 + "handle edge cases."
        long_reasoning_prompt = "explain the root cause and analyze step by step why this fails. " * 40

        _, provider, _ = selector.select_provider(long_code_prompt)
        assert provider.__class__.__module__.endswith("openai_wrapper")

        _, provider2, _ = selector.select_provider(long_reasoning_prompt)
        assert provider2.__class__.__module__.endswith("anthropic_wrapper")
    finally:
        key_manager.delete_provider_key("openai")
        key_manager.delete_provider_key("anthropic")
        provider_registry.reload_providers()


# ============================================================
# mode_status_request (Models page indicators)
# ============================================================
def test_mode_status_request_reports_live_state():
    from backend import ipc_router
    from backend.core.mode_manager import ModeManager

    original_mode = ModeManager().get_mode()
    original_override = ModeManager().get_explicit_model_override()
    try:
        mm = ModeManager()
        mm.set_mode("local")
        mm.set_explicit_model_override(None)

        r = ipc_router.dispatch({"type": "mode_status_request", "payload": {}})
        assert r["type"] == "mode_status_result"
        assert r["payload"]["routing_mode"] == "local"
        assert r["payload"]["explicit_model_override"] is None
        assert "active_model_id" in r["payload"]
    finally:
        ModeManager().set_mode(original_mode)
        ModeManager().set_explicit_model_override(original_override)


# ============================================================
# IPC endpoints — providers/modules
# ============================================================
def test_ipc_providers_and_modules_endpoints():
    from backend import ipc_router
    from backend.core import key_manager

    provider = "custom_http"
    env_var = key_manager.PROVIDER_ENV_VARS[provider]
    original_env = os.environ.get(env_var)

    try:
        r = ipc_router.dispatch({"type": "providers_list_request", "payload": {}})
        assert r["type"] == "providers_list_result"
        names = {p["name"] for p in r["payload"]["providers"]}
        assert names == set(key_manager.PROVIDER_ENV_VARS.keys())

        r = ipc_router.dispatch({"type": "provider_key_set_request", "payload": {"provider": provider, "api_key": "ipc-test-key"}})
        assert r == {"type": "provider_key_set_result", "payload": {"ok": True, "provider": provider}}

        r = ipc_router.dispatch({"type": "providers_list_request", "payload": {}})
        status = {p["name"]: p["configured"] for p in r["payload"]["providers"]}
        assert status[provider] is True

        r = ipc_router.dispatch({"type": "provider_key_delete_request", "payload": {"provider": provider}})
        assert r == {"type": "provider_key_delete_result", "payload": {"ok": True, "provider": provider}}

        r = ipc_router.dispatch({"type": "provider_key_set_request", "payload": {"provider": "nope", "api_key": "x"}})
        assert r["type"] == "provider_key_set_result"
        assert r["payload"]["ok"] is False

        r = ipc_router.dispatch({"type": "modules_list_request", "payload": {}})
        assert r["type"] == "modules_list_result"

        r = ipc_router.dispatch({"type": "module_key_set_request", "payload": {"module": "__ipc_test_module__", "api_key": "x"}})
        assert r["payload"]["ok"] is True

        r = ipc_router.dispatch({"type": "module_key_delete_request", "payload": {"module": "__ipc_test_module__"}})
        assert r["payload"]["ok"] is True
    finally:
        try:
            key_manager.delete_provider_key(provider)
        except Exception:
            pass
        if original_env is None:
            os.environ.pop(env_var, None)
        else:
            os.environ[env_var] = original_env
        from backend.core import module_manager
        try:
            module_manager.delete_module_key("__ipc_test_module__")
        except Exception:
            pass


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
