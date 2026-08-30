# backend/tests/model_routing_tests.py
#
# Regression tests for the task-complexity-based local model routing
# feature:
#   - backend/core/complexity_router.py's ladder + hardware gates
#   - backend/core/streaming_engine.py's _describe_provider() helper and
#     the new active_model_changed broadcast (sent before any tokens,
#     never altering stream_start/stream_token/stream_end)
#   - Local Mode and Automatic Model Routing's local branch both route
#     through the same complexity ladder (no divergence between the two)
#
# Self-contained plain-assert tests, matching backend/tests/skr_and_ipc_tests.py
# — not pytest. Run directly:
#
#   python backend/tests/model_routing_tests.py

from __future__ import annotations

import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _snapshot(cpu_usage=10.0, ram_used_gb=8.0, ram_total_gb=32.0, vram_used_gb=0.0, vram_total_gb=0.0, unity_running=False):
    from backend.core.resource_monitor import ResourceSnapshot
    return ResourceSnapshot(
        cpu_usage=cpu_usage, ram_used_gb=ram_used_gb, ram_total_gb=ram_total_gb,
        vram_used_gb=vram_used_gb, vram_total_gb=vram_total_gb, unity_running=unity_running,
    )


def _with_patched_snapshot(snapshot, fn):
    from backend.core import complexity_router

    original = complexity_router.get_resource_snapshot
    complexity_router.get_resource_snapshot = lambda: snapshot
    try:
        return fn()
    finally:
        complexity_router.get_resource_snapshot = original


# ============================================================
# PART 1 — complexity_router.py's ladder
# ============================================================
def test_trivial_prompt_picks_the_lightest_model_the_ladder_has():
    """The raw ladder still bottoms out at the trivial tier.

    Asked for the ladder's own answer -- allow_below_chat_floor -- a
    trivial prompt gets the trivial model, which is what this test was
    written to prove and still proves.
    """
    from backend.core.complexity_router import select_local_model_for_prompt, TRIVIAL_MODEL_ID

    result = _with_patched_snapshot(
        _snapshot(),
        lambda: select_local_model_for_prompt("hi", allow_below_chat_floor=True))
    assert result == TRIVIAL_MODEL_ID, f"expected {TRIVIAL_MODEL_ID}, got {result}"


def test_a_trivial_chat_prompt_is_lifted_off_the_0_5b_by_the_chat_floor():
    """And for an actual chat turn, the capability gate wins.

    This test used to assert the 0.5B answered "hi". The chat capability
    gate (f7ef7c3) exists because it could not: it answered with a
    leaked system prompt in a live session. The gate is required
    behaviour -- "it must still redirect 0.5B" -- so the old expectation
    is obsolete rather than broken.

    Nothing collected this file until pytest.ini did, which is why an
    assertion contradicting a deliberate, tested feature sat green-by-
    absence for months.
    """
    from backend.core.complexity_router import select_local_model_for_prompt, TRIVIAL_MODEL_ID

    result = _with_patched_snapshot(_snapshot(), lambda: select_local_model_for_prompt("hi"))
    assert result != TRIVIAL_MODEL_ID, "the chat floor must lift a chat turn off the 0.5B"


def test_high_complexity_with_ample_ram_selects_difficult_tier():
    from backend.core.complexity_router import select_local_model_for_prompt, DIFFICULT_MODEL_ID

    # >=48GB FREE — 64GB total, only 4GB used.
    ample = _snapshot(cpu_usage=10.0, ram_used_gb=4.0, ram_total_gb=64.0)
    long_prompt = "Please write a detailed multi-step analysis. " * 60  # long enough for "high"
    result = _with_patched_snapshot(ample, lambda: select_local_model_for_prompt(long_prompt))
    assert result == DIFFICULT_MODEL_ID, f"expected {DIFFICULT_MODEL_ID} with ample free RAM, got {result}"


def test_high_complexity_with_insufficient_free_ram_steps_down():
    from backend.core.complexity_router import select_local_model_for_prompt, DIFFICULT_MODEL_ID, MEDIUM_MODEL_ID

    # 32GB total, 20GB used -> 12GB free, well under the 48GB gate.
    tight = _snapshot(cpu_usage=10.0, ram_used_gb=20.0, ram_total_gb=32.0)
    long_prompt = "Please write a detailed multi-step analysis. " * 60
    result = _with_patched_snapshot(tight, lambda: select_local_model_for_prompt(long_prompt))
    assert result != DIFFICULT_MODEL_ID, "must not select the difficult tier without >=48GB free RAM"
    assert result == MEDIUM_MODEL_ID, f"expected step-down to {MEDIUM_MODEL_ID}, got {result}"


def test_cpu_saturation_steps_down_one_rung_regardless_of_tier():
    from backend.core.complexity_router import select_local_model_for_prompt, MEDIUM_MODEL_ID, SIMPLE_MODEL_ID
    from backend.core.safety_manager import BLOCK_CPU_PCT

    # Ample RAM (so the RAM gate itself wouldn't cause a step-down) but
    # CPU pinned at/above the saturation threshold.
    saturated = _snapshot(cpu_usage=BLOCK_CPU_PCT, ram_used_gb=4.0, ram_total_gb=64.0)
    # >600 chars (the medium-tier split point) but still under 1200/20
    # lines, so classify_task_complexity() calls it "medium" and
    # _pick_medium_tier() lands it on MEDIUM_MODEL_ID rather than
    # SIMPLE_MODEL_ID.
    medium_prompt = "Explain how X works and why it matters in detail. " * 15
    assert 600 < len(medium_prompt) < 1200, f"test prompt length {len(medium_prompt)} not in the intended medium-tier-proper range"

    not_saturated = _snapshot(cpu_usage=10.0, ram_used_gb=4.0, ram_total_gb=64.0)
    baseline = _with_patched_snapshot(not_saturated, lambda: select_local_model_for_prompt(medium_prompt))
    stepped_down = _with_patched_snapshot(saturated, lambda: select_local_model_for_prompt(medium_prompt))

    assert baseline == MEDIUM_MODEL_ID, f"test setup: expected medium tier baseline, got {baseline}"
    assert stepped_down == SIMPLE_MODEL_ID, f"expected CPU saturation to step down to {SIMPLE_MODEL_ID}, got {stepped_down}"


def test_missing_ladder_model_falls_through_to_next_installed():
    from backend.core import complexity_router

    # Simulate an install where the difficult-tier model isn't
    # registered at all — must fall through to the next lighter ladder
    # entry rather than crashing or silently returning an unusable id.
    from backend.core import model_registry
    original_get_model = model_registry.get_model

    def patched_get_model(model_id):
        if model_id == complexity_router.DIFFICULT_MODEL_ID:
            return None
        return original_get_model(model_id)

    complexity_router_get_model = complexity_router.get_model
    complexity_router.get_model = patched_get_model
    try:
        ample = _snapshot(cpu_usage=10.0, ram_used_gb=4.0, ram_total_gb=64.0)
        long_prompt = "Please write a detailed multi-step analysis. " * 60
        result = _with_patched_snapshot(ample, lambda: complexity_router.select_local_model_for_prompt(long_prompt))
        assert result == complexity_router.MEDIUM_MODEL_ID, f"expected fallthrough to {complexity_router.MEDIUM_MODEL_ID}, got {result}"
    finally:
        complexity_router.get_model = complexity_router_get_model


def test_never_returns_none_or_empty():
    from backend.core.complexity_router import select_local_model_for_prompt

    for prompt in ("", "   ", "x" * 5000):
        result = _with_patched_snapshot(_snapshot(), lambda p=prompt: select_local_model_for_prompt(p))
        assert result, f"expected a non-empty model_id for prompt {prompt[:20]!r}, got {result!r}"


# ============================================================
# PART 2 — Local Mode and Automatic Model Routing's local branch use the
# SAME ladder
# ============================================================
def test_local_mode_and_automatic_local_branch_agree_on_the_same_prompt():
    from backend.core.provider_router import ProviderRouter
    from backend.core.mode_manager import ModeManager

    mm = ModeManager()
    original_mode = mm.get_mode()
    long_prompt = "Please write a detailed multi-step analysis. " * 60

    try:
        mm.set_mode("local")
        router = ProviderRouter(mm)
        _, local_mode_model = router.resolve(None, prompt=long_prompt)

        mm.set_mode("automatic")
        router_auto = ProviderRouter(mm)
        # Force Automatic Model Routing's local path by making cloud look
        # unconfigured is unnecessary here — we only care whether local
        # mode and automatic routing's local selection would agree on
        # complexity given the SAME resource snapshot; call
        # complexity_router directly for the automatic equivalent to
        # avoid depending on live cloud key state.
        from backend.core.complexity_router import select_local_model_for_prompt
        auto_equivalent = select_local_model_for_prompt(long_prompt)

        assert local_mode_model == auto_equivalent, (
            f"Local Mode ({local_mode_model}) and the complexity ladder Automatic Model Routing's "
            f"local branch also uses ({auto_equivalent}) disagree for the same prompt"
        )
    finally:
        mm.set_mode(original_mode)


# ============================================================
# PART 3 — streaming_engine.py's provider description + broadcast
# ============================================================
def test_describe_provider_identifies_local():
    from backend.core.streaming_engine import _describe_provider
    from backend.llm.providers.provider_registry import get_provider

    local = get_provider("local")
    assert local is not None, "expected a 'local' provider to be registered"
    location, name = _describe_provider(local, "nemo-12b-q5")
    assert location == "local", f"expected local, got {location}"
    assert name == "local", f"expected provider name 'local', got {name!r}"


def test_describe_provider_identifies_cloud():
    from backend.core.streaming_engine import _describe_provider
    from backend.llm.providers.provider_registry import get_provider

    openai = get_provider("openai")
    assert openai is not None, "expected an 'openai' provider to be registered"
    location, name = _describe_provider(openai, None)
    assert location == "cloud", f"expected cloud, got {location}"
    assert name == "openai", f"expected provider name 'openai', got {name!r}"


def test_describe_provider_handles_none():
    from backend.core.streaming_engine import _describe_provider

    location, name = _describe_provider(None, None)
    assert location == "unknown"
    assert name is None


def test_stream_sends_active_model_changed_before_any_token():
    from backend.core.streaming_engine import StreamingEngine
    from backend.core.local_inference_engine import InferenceRequest, InferenceMessage
    from backend.llm.providers.provider_registry import get_provider
    from backend import ipc_schema as schema

    engine = StreamingEngine()

    # Use the REAL registered "local" provider instance (not a fake
    # class) so _describe_provider()'s module-name-based derivation
    # resolves exactly as it would in production — a fake class defined
    # in this test file would have __module__ == "__main__" and defeat
    # the very thing being tested. Its .stream() is monkeypatched so
    # this never attempts real model loading/inference.
    local_provider = get_provider("local")
    assert local_provider is not None, "expected a 'local' provider to be registered"

    original_stream_method = local_provider.stream
    local_provider.stream = lambda request, callback: callback({"content": "hi"})

    original_resolve = engine.provider_router.resolve
    engine.provider_router.resolve = lambda model_id, prompt, *a: (local_provider, "mistral-7b-q4km")

    packets = []
    try:
        request = InferenceRequest(model_id=None, messages=[InferenceMessage(role="user", content="hi")])
        engine.stream(request, packets.append)
    finally:
        engine.provider_router.resolve = original_resolve
        local_provider.stream = original_stream_method

    types_in_order = [p["type"] for p in packets]
    assert schema.ACTIVE_MODEL_CHANGED in types_in_order, f"expected active_model_changed in {types_in_order}"

    active_model_index = types_in_order.index(schema.ACTIVE_MODEL_CHANGED)
    token_indices = [i for i, t in enumerate(types_in_order) if t == schema.STREAM_TOKEN]
    assert all(active_model_index < i for i in token_indices), (
        f"active_model_changed must be sent before any stream_token, got order {types_in_order}"
    )

    active_model_packet = packets[active_model_index]
    assert active_model_packet["modelId"] == "mistral-7b-q4km", active_model_packet
    assert active_model_packet["location"] == "local", active_model_packet
    assert active_model_packet["provider"] == "local", active_model_packet

    # Existing packets' own shape must be completely unaffected.
    assert types_in_order[0] == schema.STREAM_START
    assert types_in_order[-1] == schema.STREAM_END


# ============================================================
# PART 4 — Cloud Mode task-type-aware provider preference
# ============================================================
def test_cloud_mode_prefers_anthropic_for_reasoning_when_nothing_manually_pinned():
    from backend.core.provider_router import ProviderRouter
    from backend.core.mode_manager import ModeManager
    from backend.core import key_manager

    mm = ModeManager()
    original_mode = mm.get_mode()
    original_cloud_provider = mm.get_cloud_provider()
    original_list_configured = key_manager.list_configured_providers

    # Both configured, so the choice is purely about task-type
    # preference, not availability fallback.
    key_manager.list_configured_providers = lambda: {"openai": True, "anthropic": True}

    try:
        mm.set_mode("cloud")
        mm.set_cloud_provider(None)  # nothing manually pinned
        router = ProviderRouter(mode_manager=mm)

        provider, model_id = router.resolve(None, prompt="Explain and analyze the root cause of this bug in detail.")
        # Batch 2: cloud mode now resolves a real model via
        # backend.core.model_selector instead of always None.
        assert model_id == "claude-3-opus", model_id
        assert type(provider).__module__.endswith("anthropic_wrapper"), (
            f"expected the anthropic provider for a reasoning-heavy prompt, got module {type(provider).__module__}"
        )

        provider2, _ = router.resolve(None, prompt="Write a Python function that parses this file.")
        assert type(provider2).__module__.endswith("openai_wrapper"), (
            f"expected the openai provider for a code prompt, got module {type(provider2).__module__}"
        )
    finally:
        key_manager.list_configured_providers = original_list_configured
        mm.set_cloud_provider(original_cloud_provider)
        mm.set_mode(original_mode)


def test_cloud_mode_manual_pin_wins_over_task_type_preference():
    from backend.core.provider_router import ProviderRouter
    from backend.core.mode_manager import ModeManager
    from backend.core import key_manager

    mm = ModeManager()
    original_mode = mm.get_mode()
    original_cloud_provider = mm.get_cloud_provider()
    original_list_configured = key_manager.list_configured_providers

    key_manager.list_configured_providers = lambda: {"openai": True, "anthropic": True}

    try:
        mm.set_mode("cloud")
        mm.set_cloud_provider("openai")  # manually pinned, even though this is a reasoning prompt
        router = ProviderRouter(mode_manager=mm)

        provider, _ = router.resolve(None, prompt="Explain and analyze the root cause of this bug in detail.")
        assert type(provider).__module__.endswith("openai_wrapper"), (
            f"manual pin must win regardless of task type, got module {type(provider).__module__}"
        )
    finally:
        key_manager.list_configured_providers = original_list_configured
        mm.set_cloud_provider(original_cloud_provider)
        mm.set_mode(original_mode)


# ============================================================
# PART 5 — routing-log ring buffer (dual-context UI's Routing Log panel)
# and last-local-escalation tracking (backend.core.routing_history)
# ============================================================
def test_routing_decision_is_recorded_in_the_ring_buffer():
    from backend.core import complexity_router
    from backend.core.complexity_router import select_local_model_for_prompt

    _with_patched_snapshot(_snapshot(), lambda: select_local_model_for_prompt("hi", context_length=42, tool_use=True))
    snapshot = complexity_router.routing_diagnostics_snapshot()
    assert snapshot["recent_decisions"], "expected at least one recorded routing decision"

    last = snapshot["recent_decisions"][-1]
    assert last["context_length"] == 42
    assert last["tool_use"] is True
    assert last["selected_model"] is not None
    assert last["reasoning_tier"] in ("trivial", "simple", "medium", "difficult")


def test_context_length_defaults_to_prompt_length_when_omitted():
    from backend.core import complexity_router
    from backend.core.complexity_router import select_local_model_for_prompt

    prompt = "a short prompt"
    _with_patched_snapshot(_snapshot(), lambda: select_local_model_for_prompt(prompt))
    last = complexity_router.routing_diagnostics_snapshot()["recent_decisions"][-1]
    assert last["context_length"] == len(prompt)


def test_choosing_the_difficult_tier_records_a_local_escalation():
    from backend.core import complexity_router, routing_history
    from backend.core.complexity_router import select_local_model_for_prompt, DIFFICULT_MODEL_ID

    ample = _snapshot(ram_used_gb=4.0, ram_total_gb=64.0, cpu_usage=10.0)
    long_prompt = "This is a difficult, complex prompt. " * 40

    before = routing_history.get_last_local_escalation()
    result = _with_patched_snapshot(ample, lambda: select_local_model_for_prompt(long_prompt))
    assert result == DIFFICULT_MODEL_ID
    after = routing_history.get_last_local_escalation()
    assert after is not None and (before is None or after >= before)


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
