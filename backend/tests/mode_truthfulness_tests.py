# backend/tests/mode_truthfulness_tests.py
#
# Regression tests for the mode/model truthfulness fixes:
#   - self_knowledge.resolve_active_model_and_provider() is mode-aware
#     (fixes "I'm running via the 'local' provider, in Cloud Mode" — an
#     impossible statement the self-query short-circuit used to produce
#     by always reporting the persisted LOCAL default regardless of
#     actual mode)
#   - backend.llm.providers.provider_registry.get_provider_display_name()
#     produces the proper capitalized names ("OpenAI", "Anthropic") the
#     bottom-left status bar and mode_status_result now rely on
#   - ipc_router's mode_status_result includes those display-name fields
#   - streaming_engine's active_model_changed packet includes
#     displayName/providerDisplayName
#
# Self-contained plain-assert tests, matching backend/tests/skr_and_ipc_tests.py
# — not pytest. Run directly:
#
#   python backend/tests/mode_truthfulness_tests.py

from __future__ import annotations

import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _with_mode_manager(fn):
    """
    Run fn(mm) against the REAL ModeManager, restoring its mode/cloud
    provider/override afterward — same pattern
    backend/tests/routing_and_keys_tests.py already uses for ModeManager
    state.
    """
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
# PART 1 — self_knowledge.resolve_active_model_and_provider()
# ============================================================
def test_resolve_reports_cloud_provider_in_cloud_mode_never_local():
    from backend.core.self_knowledge import resolve_active_model_and_provider

    def scenario(mm):
        mm.set_explicit_model_override(None)
        mm.set_mode("cloud")
        mm.set_cloud_provider("anthropic")
        return resolve_active_model_and_provider(mm)

    active_id, provider_name = _with_mode_manager(scenario)
    assert active_id is None, f"expected no specific model_id in cloud mode, got {active_id!r}"
    assert provider_name == "anthropic", f"expected provider_name='anthropic', got {provider_name!r}"
    assert provider_name != "local", "must never report 'local' provider while in Cloud Mode"


def test_resolve_reports_local_default_in_local_mode():
    from backend.core.self_knowledge import resolve_active_model_and_provider
    from backend.core.model_registry import get_default_model_id, get_model

    def scenario(mm):
        mm.set_explicit_model_override(None)
        mm.set_mode("local")
        return resolve_active_model_and_provider(mm)

    active_id, provider_name = _with_mode_manager(scenario)
    expected_id = get_default_model_id()
    expected_cfg = get_model(expected_id) if expected_id else None
    assert active_id == expected_id, f"expected {expected_id!r}, got {active_id!r}"
    assert provider_name == (expected_cfg.get("provider") if expected_cfg else None)


def test_resolve_explicit_override_wins_when_mode_compatible():
    from backend.core.self_knowledge import resolve_active_model_and_provider
    from backend.core.model_registry import get_model

    def scenario(mm):
        mm.set_mode("local")  # mode-compatible with a local override
        mm.set_explicit_model_override("nemo-12b-q5")
        return resolve_active_model_and_provider(mm)

    active_id, provider_name = _with_mode_manager(scenario)
    assert active_id == "nemo-12b-q5", f"expected the pinned override to win, got {active_id!r}"
    expected_cfg = get_model("nemo-12b-q5")
    assert provider_name == (expected_cfg.get("provider") if expected_cfg else None)


def test_resolve_ignores_local_override_while_in_cloud_mode():
    """
    ABSOLUTE MODE SEPARATION (updated behavior): a local-model override
    pinned via "switch to X" must NOT be honored while the mode is
    "cloud" — this used to be the exact source of the "local provider,
    in Cloud Mode" contradiction. The override is ignored and this must
    fall through to the mode-based cloud default instead — which, as of
    Batch 2, is a REAL cloud model_id (backend.core.model_selector), not
    None; model_selector.select_cloud_model() is monkeypatched here so
    the outcome doesn't depend on what's actually configured in this
    environment's OS keyring.
    """
    from backend.core import self_knowledge
    from backend.core import model_selector

    def scenario(mm):
        mm.set_mode("cloud")  # mode says cloud...
        mm.set_cloud_provider("openai")
        mm.set_explicit_model_override("nemo-12b-q5")  # ...but the pinned override is a local model

        original_select_cloud_model = model_selector.select_cloud_model
        fake_model_info = model_selector.ModelInfo(
            model_id="gpt-4", display_name="GPT-4", provider="openai",
            provider_display_name="OpenAI", location="cloud",
        )
        self_knowledge.model_selector.select_cloud_model = lambda provider_name: fake_model_info
        try:
            return self_knowledge.resolve_active_model_and_provider(mm)
        finally:
            self_knowledge.model_selector.select_cloud_model = original_select_cloud_model

    active_id, provider_name = _with_mode_manager(scenario)
    assert active_id != "nemo-12b-q5", f"a local override must never be honored in Cloud Mode, got active_id={active_id!r}"
    assert active_id == "gpt-4", active_id
    assert provider_name == "openai", f"expected fallthrough to the cloud-mode provider, got {provider_name!r}"
    assert provider_name != "local", "must never report 'local' provider while in Cloud Mode"


def test_environment_query_never_composes_local_provider_in_cloud_mode():
    """
    Direct regression for the exact reported bug: "I'm running via the
    'local' provider, in Cloud Mode" must never be produced.
    """
    from backend.core import self_knowledge

    def scenario(mm):
        mm.set_explicit_model_override(None)
        mm.set_mode("cloud")
        mm.set_cloud_provider("openai")
        active_id, provider_name = self_knowledge.resolve_active_model_and_provider(mm)
        snapshot = self_knowledge.build_snapshot(active_model_id=active_id, provider_name=provider_name)
        return self_knowledge.answer_self_query("environment_query", snapshot)

    answer = _with_mode_manager(scenario)
    assert "local' provider" not in answer, f"impossible statement produced: {answer!r}"
    assert "Cloud Mode" in answer, answer
    assert "'openai'" in answer, answer


# ============================================================
# PART 2 — provider display names
# ============================================================
def test_provider_display_name_known_providers():
    from backend.llm.providers.provider_registry import get_provider_display_name

    assert get_provider_display_name("openai") == "OpenAI"
    assert get_provider_display_name("anthropic") == "Anthropic"
    assert get_provider_display_name("local") == "Local"


def test_provider_display_name_unknown_falls_back_to_title_case():
    from backend.llm.providers.provider_registry import get_provider_display_name

    assert get_provider_display_name("totallynewprovider") == "Totallynewprovider"


def test_provider_display_name_none_or_empty_returns_none():
    from backend.llm.providers.provider_registry import get_provider_display_name

    assert get_provider_display_name(None) is None
    assert get_provider_display_name("") is None


# ============================================================
# PART 3 — ipc_router mode_status_result includes display names
# ============================================================
def test_mode_status_result_includes_display_names():
    from backend import ipc_router

    def scenario(mm):
        mm.set_explicit_model_override(None)
        mm.set_mode("cloud")
        mm.set_cloud_provider("anthropic")
        return ipc_router.dispatch({"type": "mode_status_request", "payload": {}})

    result = _with_mode_manager(scenario)
    payload = result["payload"]
    assert payload["cloud_provider"] == "anthropic"
    assert payload["cloud_provider_display_name"] == "Anthropic", payload


def test_mode_status_result_local_mode_includes_active_model_display_name():
    from backend import ipc_router
    from backend.core.model_registry import get_active_model_id, get_model

    def scenario(mm):
        mm.set_explicit_model_override(None)
        mm.set_mode("local")
        return ipc_router.dispatch({"type": "mode_status_request", "payload": {}})

    result = _with_mode_manager(scenario)
    payload = result["payload"]
    expected_cfg = get_model(get_active_model_id())
    expected_name = expected_cfg.get("name") if expected_cfg else None
    assert payload["active_model_display_name"] == expected_name, payload


# ============================================================
# PART 4 — streaming_engine active_model_changed includes display names
# ============================================================
def test_active_model_changed_includes_display_name_fields():
    from backend.core.streaming_engine import StreamingEngine
    from backend.core.local_inference_engine import InferenceRequest, InferenceMessage
    from backend.llm.providers.provider_registry import get_provider
    from backend import ipc_schema as schema

    engine = StreamingEngine()
    local_provider = get_provider("local")
    original_stream_method = local_provider.stream
    local_provider.stream = lambda request, callback: callback({"content": "hi"})
    original_resolve = engine.provider_router.resolve
    engine.provider_router.resolve = lambda model_id, prompt: (local_provider, "nemo-12b-q5")

    packets = []
    try:
        request = InferenceRequest(model_id=None, messages=[InferenceMessage(role="user", content="hi")])
        engine.stream(request, packets.append)
    finally:
        engine.provider_router.resolve = original_resolve
        local_provider.stream = original_stream_method

    active_model_packet = next(p for p in packets if p["type"] == schema.ACTIVE_MODEL_CHANGED)
    assert active_model_packet["displayName"] == "Mistral Nemo 12B Instruct (Q5_K_M)", active_model_packet
    assert active_model_packet["providerDisplayName"] == "Local", active_model_packet


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
