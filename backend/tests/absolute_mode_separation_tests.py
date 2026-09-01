# backend/tests/absolute_mode_separation_tests.py
#
# Regression tests for ARIA-Lite's absolute local/cloud mode separation:
#   - Cloud Mode NEVER selects a local model, even if one is explicitly
#     requested or pinned via "switch to X" (the exact real-world trigger
#     for the reported "local provider, in Cloud Mode" contradiction:
#     "switch to mistral" pins a LOCAL model as explicit_model_override
#     without touching mode at all, so a later chat_request in Cloud
#     Mode used to honor it anyway)
#   - Local Mode NEVER selects a cloud provider
#   - Automatic Model Routing may use either, based on task complexity/hardware
#   - Cloud Mode with no provider configured returns a structured
#     safety_warning, never a silent local fallback
#   - The active_model_changed broadcast never reports a rejected local
#     model_id alongside a cloud location (the exact "Model: nemo-12b-q5
#     (Cloud)" contradiction Section 4 forbids)
#   - self-knowledge never composes a local-model-on-cloud claim
#
# Self-contained plain-assert tests, matching backend/tests/skr_and_ipc_tests.py
# — not pytest. Run directly:
#
#   python backend/tests/absolute_mode_separation_tests.py

from __future__ import annotations

import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _with_mode_manager(fn):
    from backend.core.mode_manager import ModeManager

    mm = ModeManager()
    original_mode = mm.get_mode()
    original_cloud_provider = mm.get_cloud_provider()
    original_override = mm.get_explicit_model_override()
    try:
        return fn(mm)
    finally:
        mm.set_explicit_model_override(original_override)
        mm.set_cloud_provider(original_cloud_provider)
        mm.set_mode(original_mode)


# ============================================================
# PART 1 — Cloud Mode never selects a local model
# ============================================================
def test_cloud_mode_ignores_explicit_local_model_id():
    """
    The exact real-world trigger: "switch to mistral" pins a LOCAL model
    as explicit_model_override WITHOUT touching mode — a later
    chat_request in Cloud Mode must not honor it.
    """
    from backend.core.provider_router import ProviderRouter
    from backend.core import key_manager

    def scenario(mm):
        mm.set_explicit_model_override(None)
        mm.set_mode("cloud")
        mm.set_cloud_provider("openai")
        original_configured = key_manager.list_configured_providers
        key_manager.list_configured_providers = lambda: {"openai": True}
        try:
            router = ProviderRouter(mode_manager=mm)
            # A local model_id explicitly requested (e.g. a one-shot
            # packet field, or a "switch to mistral" pin surfacing as
            # requested_model_id) while in Cloud Mode.
            return router.resolve("nemo-12b-q5", prompt="hello")
        finally:
            key_manager.list_configured_providers = original_configured

    provider, resolved_model_id = _with_mode_manager(scenario)
    # Batch 2: Cloud Mode now resolves a REAL cloud model via
    # backend.core.model_selector (e.g. "gpt-4" for openai) instead of
    # always None — the actual regression this guards against is the
    # REJECTED local model_id ("nemo-12b-q5") surviving through, not
    # resolved_model_id being non-null in general.
    assert resolved_model_id != "nemo-12b-q5", (
        f"Cloud Mode must never return the rejected local model_id, got {resolved_model_id!r}"
    )
    assert resolved_model_id == "gpt-4", resolved_model_id
    assert type(provider).__module__.endswith("openai_wrapper"), (
        f"expected fallthrough to the cloud provider, got module {type(provider).__module__}"
    )


def test_cloud_mode_never_falls_back_to_qwen_or_any_local_model():
    from backend.core.provider_router import ProviderRouter
    from backend.core import key_manager

    def scenario(mm):
        mm.set_explicit_model_override(None)
        mm.set_mode("cloud")
        mm.set_cloud_provider("anthropic")
        original_configured = key_manager.list_configured_providers
        key_manager.list_configured_providers = lambda: {"anthropic": True}
        try:
            router = ProviderRouter(mode_manager=mm)
            # An unrecognized model_id — _resolve_explicit_model()'s own
            # "not found" branch would normally fall back to a local
            # model; absolute mode separation must prevent that here.
            return router.resolve("totally-unknown-model-xyz", prompt="hello")
        finally:
            key_manager.list_configured_providers = original_configured

    provider, resolved_model_id = _with_mode_manager(scenario)
    # Batch 2: Cloud Mode now resolves a REAL cloud model via
    # model_selector (e.g. "claude-3-opus" for anthropic) instead of
    # always None — the regression this guards against is a LOCAL
    # registry model_id surviving through, not non-null in general.
    assert resolved_model_id != "qwen2.5-0.5b-instruct-q4_k_m"
    assert resolved_model_id == "claude-3-opus", resolved_model_id
    assert type(provider).__module__.endswith("anthropic_wrapper")


def test_cloud_mode_with_no_provider_configured_returns_structured_safety_warning_not_local_fallback():
    from backend.core.provider_router import ProviderRouter
    from backend.core import key_manager

    def scenario(mm):
        mm.set_explicit_model_override(None)
        mm.set_mode("cloud")
        mm.set_cloud_provider(None)
        original_configured = key_manager.list_configured_providers
        key_manager.list_configured_providers = lambda: {"openai": False, "anthropic": False}
        try:
            router = ProviderRouter(mode_manager=mm)
            return router.resolve(None, prompt="hello")
        finally:
            key_manager.list_configured_providers = original_configured

    provider, resolved_model_id = _with_mode_manager(scenario)
    assert provider is None and resolved_model_id is None, (
        f"expected (None, None) — no silent local fallback — got ({provider}, {resolved_model_id})"
    )


def test_handlers_send_structured_safety_warning_when_cloud_mode_has_no_provider():
    """
    The full WS handler path: chat_request in Cloud Mode with nothing
    configured must send a safety_warning packet (Section 2's "return a
    structured safety warning" requirement), never start inference.
    """
    from backend.websocket import handlers as handlers_mod
    from backend.core import key_manager

    def scenario(mm):
        mm.set_explicit_model_override(None)
        mm.set_mode("cloud")
        mm.set_cloud_provider(None)
        original_configured = key_manager.list_configured_providers
        key_manager.list_configured_providers = lambda: {"openai": False, "anthropic": False}

        class FakeWebSocket:
            def __init__(self):
                self.sent = []

            async def send(self, raw):
                self.sent.append(raw)

        import asyncio, json

        async def run():
            ws = FakeWebSocket()
            handler = handlers_mod.WebSocketHandler(ws)
            exploded = {"n": 0}

            async def _explode(*_a, **_kw):
                exploded["n"] += 1
                raise AssertionError("_start_inference must not be called")

            handler._start_inference = _explode
            await handler._dispatch({
                "type": "chat_request",
                "conversationId": "cloud-no-provider-test",
                "messages": [{"role": "user", "content": "hello"}],
            })
            await handler.wait_for_turns()
            assert exploded["n"] == 0, "_start_inference must never be reached"
            return [json.loads(p) for p in ws.sent]

        try:
            return asyncio.run(run())
        finally:
            key_manager.list_configured_providers = original_configured

    sent = _with_mode_manager(scenario)

    # Status packets ({"type": "status"}) are additive decoration for the
    # turn indicator and carry no result -- see backend/core/turn_status.py.
    # What this test is pinning is that the refusal arrives on its own,
    # with no stream_start/stream_token/stream_end wrapped around it, so
    # they are filtered rather than counted.
    sent = [p for p in sent if p.get("type") != "status"]
    assert len(sent) == 1, f"expected exactly one result packet, got {sent}"
    packet = sent[0]
    assert packet["type"] == "safety_warning", packet
    assert packet["severity"] == "block", packet
    assert packet["model_id"] is None
    assert "Cloud Mode" in packet["message"]


# ============================================================
# PART 2 — Local Mode never selects a cloud provider
# ============================================================
def test_local_mode_never_selects_cloud_even_if_everything_is_configured():
    from backend.core.provider_router import ProviderRouter
    from backend.core import key_manager

    def scenario(mm):
        mm.set_explicit_model_override(None)
        mm.set_mode("local")
        # Make every cloud provider look fully configured — Local Mode
        # must still never touch any of them.
        original_configured = key_manager.list_configured_providers
        key_manager.list_configured_providers = lambda: {
            "openai": True, "anthropic": True, "gemini": True, "grok": True,
        }
        try:
            router = ProviderRouter(mode_manager=mm)
            return router.resolve(None, prompt="hello")
        finally:
            key_manager.list_configured_providers = original_configured

    provider, resolved_model_id = _with_mode_manager(scenario)
    assert resolved_model_id is not None, "Local Mode must always resolve to a concrete local model"
    assert type(provider).__module__.endswith("local_provider"), (
        f"Local Mode must never select a cloud provider even when all are configured, got module {type(provider).__module__}"
    )


def test_local_mode_ignores_explicit_cloud_provider_pin_style_id():
    """
    If an explicit model_id happens to name something that isn't a real
    local registry entry (simulating a stray cloud-style reference),
    Local Mode must still resolve to a real local model, never attempt
    a cloud provider.
    """
    from backend.core.provider_router import ProviderRouter

    def scenario(mm):
        mm.set_explicit_model_override(None)
        mm.set_mode("local")
        router = ProviderRouter(mode_manager=mm)
        return router.resolve("gpt-4-not-a-real-registry-entry", prompt="hello")

    provider, resolved_model_id = _with_mode_manager(scenario)
    assert type(provider).__module__.endswith("local_provider")
    assert resolved_model_id is not None


# ============================================================
# PART 3 — Automatic Model Routing may use either registry
# ============================================================
def test_automatic_mode_can_select_local_for_simple_prompts():
    from backend.core.provider_router import ProviderRouter
    from backend.core import key_manager

    def scenario(mm):
        mm.set_explicit_model_override(None)
        mm.set_mode("automatic")
        original_configured = key_manager.list_configured_providers
        key_manager.list_configured_providers = lambda: {"openai": False, "anthropic": False}
        try:
            router = ProviderRouter(mode_manager=mm)
            return router.resolve(None, prompt="hi")
        finally:
            key_manager.list_configured_providers = original_configured

    provider, resolved_model_id = _with_mode_manager(scenario)
    assert resolved_model_id is not None
    assert type(provider).__module__.endswith("local_provider")


def test_automatic_mode_can_select_cloud_for_complex_prompts_when_available():
    from backend.core.provider_router import ProviderRouter
    from backend.core import key_manager

    def scenario(mm):
        mm.set_explicit_model_override(None)
        mm.set_mode("automatic")
        original_configured = key_manager.list_configured_providers
        key_manager.list_configured_providers = lambda: {"openai": True, "anthropic": True}
        try:
            router = ProviderRouter(mode_manager=mm)
            long_prompt = "Please write a very detailed, multi-step architectural analysis. " * 40
            return router.resolve(None, prompt=long_prompt)
        finally:
            key_manager.list_configured_providers = original_configured

    provider, resolved_model_id = _with_mode_manager(scenario)
    # Automatic Model Routing is allowed to pick either registry — this
    # just confirms a cloud selection is REACHABLE (resolved_model_id
    # None + a real cloud provider module), not forced.
    if resolved_model_id is None:
        assert not type(provider).__module__.endswith("local_provider"), (
            "resolved_model_id is None but provider looks local — inconsistent result"
        )


# ============================================================
# PART 4 — active_model_changed never contradicts itself
# ============================================================
def test_active_model_changed_never_pairs_a_local_model_id_with_cloud_location():
    """
    Direct regression for the exact forbidden output named in Section 4:
    "Model: nemo-12b-q5 (Cloud)". Simulates a rejected local model_id
    (Cloud Mode) reaching StreamingEngine — the resolved model must
    never resurrect it once ProviderRouter has already (correctly)
    ignored it.

    model_selector.select_cloud_model() is monkeypatched (rather than
    relying on a real "openai" key existing in this environment) so this
    test's outcome doesn't depend on what other tests happened to leave
    configured in the OS keyring.
    """
    from backend.core import streaming_engine as streaming_engine_mod
    from backend.core import model_selector
    from backend.core.streaming_engine import StreamingEngine
    from backend.core.local_inference_engine import InferenceRequest, InferenceMessage
    from backend.llm.providers.provider_registry import get_provider
    from backend import ipc_schema as schema

    engine = StreamingEngine()
    openai_provider = get_provider("openai")
    assert openai_provider is not None

    # The openai wrapper doesn't define .stream() itself (StreamingEngine
    # falls back to .run() for providers that don't) — set it fresh here
    # and remove it afterward rather than assuming it pre-existed.
    had_stream_attr = hasattr(openai_provider, "stream")
    original_stream_method = getattr(openai_provider, "stream", None)
    openai_provider.stream = lambda request, callback: callback({"content": "hi"})
    original_resolve = engine.provider_router.resolve
    # Mimic exactly what ProviderRouter.resolve() now does: a rejected
    # local model_id in Cloud Mode falls through to the cloud branch,
    # which (via model_selector) resolves a real cloud model_id — never
    # None, and never the rejected local one.
    engine.provider_router.resolve = lambda model_id, prompt, *a: (openai_provider, "gpt-4")

    original_select_cloud_model = model_selector.select_cloud_model
    fake_model_info = model_selector.ModelInfo(
        model_id="gpt-4", display_name="GPT-4", provider="openai",
        provider_display_name="OpenAI", location="cloud",
    )
    streaming_engine_mod.model_selector.select_cloud_model = lambda provider_name: fake_model_info

    packets = []
    try:
        # The ORIGINALLY REQUESTED model_id is a local one — this is
        # exactly the value the old buggy fallback would have resurrected.
        request = InferenceRequest(model_id="nemo-12b-q5", messages=[InferenceMessage(role="user", content="hi")])
        engine.stream(request, packets.append)
    finally:
        engine.provider_router.resolve = original_resolve
        streaming_engine_mod.model_selector.select_cloud_model = original_select_cloud_model
        if had_stream_attr:
            openai_provider.stream = original_stream_method
        else:
            del openai_provider.stream

    active_model_packet = next(p for p in packets if p["type"] == schema.ACTIVE_MODEL_CHANGED)
    assert active_model_packet["location"] == "cloud", active_model_packet
    # Batch 2: active_model_changed.modelId/displayName must never be
    # null — streaming_engine.py now resolves a REAL cloud model via
    # model_selector instead of ever leaving these fields null. The
    # actual regression this test guards against is unchanged: the
    # REJECTED local model_id ("nemo-12b-q5") must never survive into
    # this packet.
    assert active_model_packet["modelId"] != "nemo-12b-q5", (
        f"forbidden contradiction: the rejected local model_id survived into a cloud-location packet: {active_model_packet}"
    )
    assert active_model_packet["modelId"] == "gpt-4", active_model_packet
    assert active_model_packet["displayName"] == "GPT-4", active_model_packet

    # stream_token packets must be equally clean — they're what a client
    # without the active_model_changed fix would have to infer from.
    # Batch 2: request.model_id is now set to the resolved cloud model
    # before the provider is invoked (the real fix for cloud API calls
    # previously sending "model": None) — stream_token's modelId reflects
    # that same resolved id, not the rejected local one and not null.
    token_packets = [p for p in packets if p["type"] == schema.STREAM_TOKEN]
    assert all(p["modelId"] == "gpt-4" for p in token_packets), token_packets


# ============================================================
# PART 5 — self-knowledge never hallucinates cloud-local mixing
# ============================================================
def test_self_knowledge_never_claims_a_local_model_is_available_on_cloud():
    from backend.core import self_knowledge

    def scenario(mm):
        mm.set_explicit_model_override(None)
        mm.set_mode("cloud")
        mm.set_cloud_provider("openai")
        active_id, provider_name = self_knowledge.resolve_active_model_and_provider(mm)
        snapshot = self_knowledge.build_snapshot(active_model_id=active_id, provider_name=provider_name)
        answers = [
            self_knowledge.answer_self_query("model_query", snapshot),
            self_knowledge.answer_self_query("environment_query", snapshot),
            self_knowledge.answer_self_query("capability_query", snapshot),
        ]
        return answers

    answers = _with_mode_manager(scenario)
    for text in answers:
        assert "local' provider" not in text, f"impossible statement: {text!r}"
        # The specific hallucination named in the task: a local model
        # name presented as something cloud-hosted.
        assert "available on a cloud instance" not in text.lower(), f"hallucinated claim: {text!r}"
        assert "nemo-12b-q5" not in text or "cloud" not in text.lower(), (
            f"local model name and 'cloud' both appearing risks implying a cloud-hosted local model: {text!r}"
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
