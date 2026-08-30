# backend/tests/model_switching_tests.py
#
# Regression tests for Batch 2 — smooth, truthful cloud/local model
# switching built on Batch 1's routing invariants and provider loader:
#   - backend.core.model_selector is the single source of truth for
#     "which model backs this mode + provider" (select_cloud_model() /
#     select_local_model())
#   - backend.core.routing_guard.attempt_mode_switch() /
#     attempt_model_override_switch() route every switch through it,
#     rejecting (and leaving state untouched) on CLOUD_MODEL_RESOLUTION_FAILED
#     / LOCAL_MODEL_INVALID
#   - ipc_router.mode_status_result reports routing_mode/cloud_provider/
#     active_model_id/active_model_display_name/location consistently
#   - streaming_engine's active_model_changed never has a null modelId/
#     displayName and never disagrees with routing_mode
#
# Self-contained plain-assert tests, matching backend/tests/routing_invariants_tests.py
# — not pytest. Run directly:
#
#   python backend/tests/model_switching_tests.py

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


def _fake_cloud_model(provider_name: str):
    from backend.core import model_selector

    table = {
        "anthropic": ("claude-3-opus", "Claude 3 Opus"),
        "openai": ("gpt-4", "GPT-4"),
    }
    model_id, display_name = table.get(provider_name, (f"{provider_name}-default", provider_name.title()))
    return model_selector.ModelInfo(
        model_id=model_id, display_name=display_name, provider=provider_name,
        provider_display_name=display_name.split()[0] if provider_name not in table else
        {"anthropic": "Anthropic", "openai": "OpenAI"}[provider_name],
        location="cloud",
    )


def _patch_valid_cloud_provider(provider_names):
    """Monkeypatch provider_config.is_valid_cloud_provider to accept exactly
    `provider_names`, and model_selector.select_cloud_model to resolve a
    deterministic fake model for any of them — isolates these tests from
    whatever is actually configured in this machine's OS keyring."""
    from backend.core import provider_config, model_selector

    original_is_valid = provider_config.is_valid_cloud_provider
    original_select = model_selector.select_cloud_model

    provider_config.is_valid_cloud_provider = lambda name: name in provider_names
    model_selector.select_cloud_model = lambda name: (
        _fake_cloud_model(name) if name in provider_names
        else model_selector.SelectionError("CLOUD_MODEL_RESOLUTION_FAILED", f"'{name}' not configured (test)")
    )

    def restore():
        provider_config.is_valid_cloud_provider = original_is_valid
        model_selector.select_cloud_model = original_select

    return restore


# ============================================================
# PART 1 — Switching to Cloud Mode
# ============================================================
def test_switch_to_cloud_mode_with_anthropic_sets_full_consistent_state():
    from backend.core import routing_guard

    def scenario(mm):
        restore = _patch_valid_cloud_provider({"anthropic"})
        try:
            result = routing_guard.attempt_mode_switch(mm, "cloud", provider="anthropic")
        finally:
            restore()

        assert result.ok is True, result.error_message
        assert result.cloud_provider == "anthropic"
        assert result.model_id == "claude-3-opus"
        assert mm.get_mode() == "cloud"
        assert mm.get_cloud_provider() == "anthropic"

    _with_mode_manager(scenario)


def test_switch_to_cloud_mode_with_openai_sets_full_consistent_state():
    from backend.core import routing_guard

    def scenario(mm):
        restore = _patch_valid_cloud_provider({"openai"})
        try:
            result = routing_guard.attempt_mode_switch(mm, "cloud", provider="openai")
        finally:
            restore()

        assert result.ok is True, result.error_message
        assert result.cloud_provider == "openai"
        assert result.model_id == "gpt-4"
        assert mm.get_mode() == "cloud"
        assert mm.get_cloud_provider() == "openai"

    _with_mode_manager(scenario)


def test_switch_to_cloud_mode_reports_correct_mode_status():
    from backend.core import routing_guard
    from backend import ipc_router

    def scenario(mm):
        restore = _patch_valid_cloud_provider({"anthropic"})
        try:
            switch = routing_guard.attempt_mode_switch(mm, "cloud", provider="anthropic")
            assert switch.ok
            result = ipc_router.dispatch({"type": "mode_status_request", "payload": {}})
        finally:
            restore()
        return result

    result = _with_mode_manager(scenario)
    payload = result["payload"]
    assert payload["routing_mode"] == "cloud"
    assert payload["cloud_provider"] == "anthropic"
    assert payload["active_model_id"] == "claude-3-opus", payload
    assert payload["active_model_display_name"] == "Claude 3 Opus", payload
    assert payload["location"] == "cloud"


# ============================================================
# PART 2 — Switching to Local Mode
# ============================================================
def test_switch_to_local_mode_sets_full_consistent_state():
    from backend.core import routing_guard
    from backend.core.model_registry import get_default_model_id

    def scenario(mm):
        mm.set_mode("cloud")
        mm.set_cloud_provider("openai")

        result = routing_guard.attempt_mode_switch(mm, "local")

        assert result.ok is True, result.error_message
        assert result.model_id == get_default_model_id()
        assert mm.get_mode() == "local"
        assert mm.get_cloud_provider() is None

    _with_mode_manager(scenario)


def test_switch_to_local_mode_reports_correct_mode_status():
    from backend.core import routing_guard
    from backend import ipc_router
    from backend.core.model_registry import get_default_model_id, get_model

    def scenario(mm):
        switch = routing_guard.attempt_mode_switch(mm, "local")
        assert switch.ok
        return ipc_router.dispatch({"type": "mode_status_request", "payload": {}})

    result = _with_mode_manager(scenario)
    payload = result["payload"]
    default_id = get_default_model_id()
    default_cfg = get_model(default_id)

    assert payload["routing_mode"] == "local"
    assert payload["active_model_id"] == default_id
    assert payload["active_model_display_name"] == (default_cfg.get("name") if default_cfg else None)
    assert payload["location"] == "local"


def test_switch_to_specific_local_model_via_override():
    from backend.core import routing_guard
    from backend.core.model_registry import get_default_model_id

    def scenario(mm):
        mm.set_mode("local")
        local_id = get_default_model_id()
        assert local_id

        result = routing_guard.attempt_model_override_switch(mm, local_id)

        assert result.ok is True
        assert result.model_id == local_id
        assert mm.get_explicit_model_override() == local_id

    _with_mode_manager(scenario)


# ============================================================
# PART 3 — Invalid switches: state untouched, structured errors
# ============================================================
def test_cloud_switch_with_unconfigured_provider_is_rejected_and_state_untouched():
    from backend.core import routing_guard

    def scenario(mm):
        mm.set_mode("local")
        mm.set_cloud_provider(None)

        restore = _patch_valid_cloud_provider(set())  # nothing configured
        try:
            result = routing_guard.attempt_mode_switch(mm, "cloud", provider="openai")
        finally:
            restore()

        assert result.ok is False
        assert result.error_code in ("NO_CLOUD_PROVIDER", "CLOUD_MODEL_RESOLUTION_FAILED")
        assert mm.get_mode() == "local", "rejected switch must not change routing_mode"
        assert mm.get_cloud_provider() is None

    _with_mode_manager(scenario)


def test_cloud_switch_with_valid_provider_but_no_model_mapping_is_rejected():
    """
    A provider that's configured/enabled but has no default (or
    override) model mapping — model_selector.select_cloud_model()
    fails with CLOUD_MODEL_RESOLUTION_FAILED even though the provider
    itself is valid, and the switch is rejected on that basis.
    """
    from backend.core import routing_guard, provider_config, model_selector

    def scenario(mm):
        mm.set_mode("local")

        original_is_valid = provider_config.is_valid_cloud_provider
        original_select = model_selector.select_cloud_model
        provider_config.is_valid_cloud_provider = lambda name: name == "custom_http"
        model_selector.select_cloud_model = lambda name: model_selector.SelectionError(
            "CLOUD_MODEL_RESOLUTION_FAILED", "No default model mapping (test)",
        )
        try:
            result = routing_guard.attempt_mode_switch(mm, "cloud", provider="custom_http")
        finally:
            provider_config.is_valid_cloud_provider = original_is_valid
            model_selector.select_cloud_model = original_select

        assert result.ok is False
        assert result.error_code == "CLOUD_MODEL_RESOLUTION_FAILED", result.error_code
        assert mm.get_mode() == "local"
        assert mm.get_cloud_provider() is None

    _with_mode_manager(scenario)


def test_invalid_local_model_override_is_rejected_and_state_untouched():
    from backend.core import routing_guard

    def scenario(mm):
        mm.set_mode("local")
        mm.set_explicit_model_override(None)

        result = routing_guard.attempt_model_override_switch(mm, "not-a-real-model-id")

        assert result.ok is False
        assert result.error_code == "LOCAL_MODEL_INVALID", result.error_code
        assert mm.get_explicit_model_override() is None, "rejected override must not be persisted"

    _with_mode_manager(scenario)


def test_local_model_override_pointing_at_a_cloud_style_id_is_rejected():
    from backend.core import routing_guard

    def scenario(mm):
        mm.set_mode("local")
        mm.set_explicit_model_override(None)

        result = routing_guard.attempt_model_override_switch(mm, "gpt-4")

        assert result.ok is False
        assert result.error_code == "LOCAL_MODEL_INVALID", result.error_code
        assert mm.get_explicit_model_override() is None

    _with_mode_manager(scenario)


def test_model_override_incompatible_with_cloud_mode_is_rejected():
    from backend.core import routing_guard
    from backend.core.model_registry import get_default_model_id

    def scenario(mm):
        mm.set_mode("cloud")
        mm.set_cloud_provider("openai")
        mm.set_explicit_model_override(None)
        local_id = get_default_model_id()

        result = routing_guard.attempt_model_override_switch(mm, local_id)

        assert result.ok is False
        assert result.error_code == "MODEL_MODE_MISMATCH", result.error_code
        assert mm.get_explicit_model_override() is None

    _with_mode_manager(scenario)


# ============================================================
# PART 4 — active_model_changed is always truthful
# ============================================================
def _run_stream_and_collect(engine, request):
    packets = []
    engine.stream(request, packets.append)
    return packets


def test_active_model_changed_cloud_turn_has_non_null_fields_and_correct_location():
    from backend.core.streaming_engine import StreamingEngine
    from backend.core.local_inference_engine import InferenceRequest, InferenceMessage
    from backend.core import model_selector
    from backend.llm.providers.provider_registry import get_provider
    from backend import ipc_schema as schema

    engine = StreamingEngine()
    openai_provider = get_provider("openai")
    assert openai_provider is not None

    had_stream_attr = hasattr(openai_provider, "stream")
    original_stream_method = getattr(openai_provider, "stream", None)
    openai_provider.stream = lambda request, callback: callback({"content": "hi"})
    original_resolve = engine.provider_router.resolve
    engine.provider_router.resolve = lambda model_id, prompt, *a: (openai_provider, None)

    original_select = model_selector.select_cloud_model
    fake_info = _fake_cloud_model("openai")
    model_selector.select_cloud_model = lambda name: fake_info

    try:
        request = InferenceRequest(model_id=None, messages=[InferenceMessage(role="user", content="hi")])
        packets = _run_stream_and_collect(engine, request)
    finally:
        engine.provider_router.resolve = original_resolve
        model_selector.select_cloud_model = original_select
        if had_stream_attr:
            openai_provider.stream = original_stream_method
        else:
            del openai_provider.stream

    packet = next(p for p in packets if p["type"] == schema.ACTIVE_MODEL_CHANGED)
    assert packet["modelId"] is not None
    assert packet["displayName"] is not None
    assert packet["modelId"] == "gpt-4"
    assert packet["displayName"] == "GPT-4"
    assert packet["location"] == "cloud"
    assert packet["provider"] == "openai"
    assert packet["providerDisplayName"] == "OpenAI"
    # The real API-call fix: request.model_id must carry the resolved id,
    # not the original (None) request — cloud wrappers send this
    # straight to the live API as the "model" field.
    assert request.model_id == "gpt-4"


def test_active_model_changed_local_turn_has_non_null_fields_and_correct_location():
    from backend.core.streaming_engine import StreamingEngine
    from backend.core.local_inference_engine import InferenceRequest, InferenceMessage
    from backend.llm.providers.provider_registry import get_provider
    from backend.core.model_registry import get_default_model_id, get_model
    from backend import ipc_schema as schema

    engine = StreamingEngine()
    local_provider = get_provider("local")
    original_stream_method = local_provider.stream
    local_provider.stream = lambda request, callback: callback({"content": "hi"})
    original_resolve = engine.provider_router.resolve
    default_id = get_default_model_id()
    engine.provider_router.resolve = lambda model_id, prompt, *a: (local_provider, default_id)

    try:
        request = InferenceRequest(model_id=None, messages=[InferenceMessage(role="user", content="hi")])
        packets = _run_stream_and_collect(engine, request)
    finally:
        engine.provider_router.resolve = original_resolve
        local_provider.stream = original_stream_method

    packet = next(p for p in packets if p["type"] == schema.ACTIVE_MODEL_CHANGED)
    default_cfg = get_model(default_id)
    assert packet["modelId"] == default_id
    assert packet["displayName"] == (default_cfg.get("name") if default_cfg else None)
    assert packet["displayName"] is not None
    assert packet["location"] == "local"
    assert packet["providerDisplayName"] == "Local"


def test_active_model_changed_never_emitted_when_local_selection_fails():
    """
    An invalid resolved_model_id reaching StreamingEngine (e.g. a stale
    registry reference) must abort with a structured stream_error, never
    an active_model_changed carrying a lie.
    """
    from backend.core.streaming_engine import StreamingEngine
    from backend.core.local_inference_engine import InferenceRequest, InferenceMessage
    from backend.llm.providers.provider_registry import get_provider
    from backend import ipc_schema as schema

    engine = StreamingEngine()
    local_provider = get_provider("local")
    original_resolve = engine.provider_router.resolve
    engine.provider_router.resolve = lambda model_id, prompt, *a: (local_provider, "not-a-real-model-id")

    try:
        request = InferenceRequest(model_id=None, messages=[InferenceMessage(role="user", content="hi")])
        packets = _run_stream_and_collect(engine, request)
    finally:
        engine.provider_router.resolve = original_resolve

    assert not any(p["type"] == schema.ACTIVE_MODEL_CHANGED for p in packets), packets
    error_packet = next(p for p in packets if p["type"] == "stream_error")
    assert error_packet["code"] == "LOCAL_MODEL_INVALID", error_packet


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
