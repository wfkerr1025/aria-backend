# backend/tests/self_query_truthfulness_tests.py
#
# Regression tests for the "fix legacy canned self-query replies" pass:
# "which model are you using?" / "what mode are you using?" must answer
# from real backend state (self_knowledge.resolve_active_model_and_provider()
# + build_snapshot()'s derived display names), never from legacy canned
# text. Specifically verifies:
#   1. Self-query replies never contain fallback/emergency/installed
#      models text, for ANY self-query intent, in ANY mode.
#   2. Self-query replies always reflect backend truth (the real active
#      model_id / provider / mode, not a hardcoded default).
#   3. Cloud Mode never reports a local model in model_query/capability_query.
#   4. Local Mode never reports a cloud provider in model_query/capability_query.
#   5. Automatic Model Selection reports the correct (persisted default)
#      active model.
#   6. The exact legacy canned strings named in the task are fully removed.
#
# Self-contained plain-assert tests, matching backend/tests/skr_and_ipc_tests.py
# — not pytest. Run directly:
#
#   python backend/tests/self_query_truthfulness_tests.py

from __future__ import annotations

import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Exact legacy strings named in "SECTION 1 — REMOVE LEGACY CANNED
# RESPONSES" — must never appear in any self-query reply again.
_LEGACY_SNIPPETS = [
    "Right now I'm running on",
    "My fallback model is",
    "My emergency model is",
    "Installed models:",
    "I switch to those under RAM pressure",
    "auto-switch between",
]


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


def _snapshot_for_mode(mm, mode, cloud_provider="openai"):
    from backend.core import self_knowledge as sk

    mm.set_explicit_model_override(None)
    mm.set_mode(mode)
    if mode == "cloud":
        mm.set_cloud_provider(cloud_provider)
    active_id, provider_name = sk.resolve_active_model_and_provider(mm)
    return sk.build_snapshot(active_model_id=active_id, provider_name=provider_name)


# ============================================================
# PART 1 — legacy canned strings never appear, in any mode, for any intent
# ============================================================
def test_no_legacy_canned_strings_in_any_self_query_reply_in_any_mode():
    from backend.core import self_knowledge as sk

    def scenario(mm):
        for mode in ("local", "cloud", "automatic"):
            snapshot = _snapshot_for_mode(mm, mode)
            for intent in sorted(sk._SELF_QUERY_INTENT_LITERALS):
                answer = sk.answer_self_query(intent, snapshot)
                for snippet in _LEGACY_SNIPPETS:
                    assert snippet not in answer, (
                        f"mode={mode!r} intent={intent!r} contains forbidden legacy text "
                        f"{snippet!r}: {answer!r}"
                    )

    _with_mode_manager(scenario)


def test_nemo_never_mentioned_unless_it_is_the_actual_active_model():
    """
    "Any mention of nemo-12b-q5 unless it is the actual active model" —
    force the active local model to something else and confirm
    nemo-12b-q5 never leaks into a model_query/capability_query answer.
    """
    from backend.core import self_knowledge as sk
    from backend.core import model_registry

    def scenario(mm):
        other_model = "qwen2.5-0.5b-instruct-q4_k_m"
        assert model_registry.get_model(other_model) is not None, "test fixture model missing from registry"
        mm.set_explicit_model_override(None)
        mm.set_mode("local")
        snapshot = sk.build_snapshot(active_model_id=other_model, provider_name="local")
        assert snapshot.active_model_id != "nemo-12b-q5"

        for intent in ("model_query", "capability_query", "mode_query"):
            answer = sk.answer_self_query(intent, snapshot)
            assert "nemo-12b-q5" not in answer, f"{intent} mentioned nemo-12b-q5 while it isn't the active model: {answer!r}"

    _with_mode_manager(scenario)


# ============================================================
# PART 2 — replies always reflect backend truth
# ============================================================
def test_model_query_names_the_real_active_model_in_local_mode():
    from backend.core import self_knowledge as sk

    def scenario(mm):
        snapshot = _snapshot_for_mode(mm, "local")
        answer = sk.answer_self_query("model_query", snapshot)
        assert answer == f"I am using {snapshot.active_model_display_name} (Local)."
        assert snapshot.active_model_display_name is not None

    _with_mode_manager(scenario)


def test_model_query_names_the_real_provider_in_cloud_mode():
    from backend.core import self_knowledge as sk

    def scenario(mm):
        snapshot = _snapshot_for_mode(mm, "cloud", cloud_provider="anthropic")
        answer = sk.answer_self_query("model_query", snapshot)
        assert answer == "I am using Anthropic (Cloud)."

    _with_mode_manager(scenario)


def test_model_query_names_the_real_active_model_under_automatic_selection():
    from backend.core import self_knowledge as sk

    def scenario(mm):
        snapshot = _snapshot_for_mode(mm, "automatic")
        answer = sk.answer_self_query("model_query", snapshot)
        assert answer == f"I am using {snapshot.active_model_display_name} via automatic model selection."
        assert snapshot.active_model_display_name is not None

    _with_mode_manager(scenario)


def test_mode_query_reports_the_real_persisted_mode():
    from backend.core import self_knowledge as sk

    def scenario(mm):
        local_answer = sk.answer_self_query("mode_query", _snapshot_for_mode(mm, "local"))
        cloud_answer = sk.answer_self_query("mode_query", _snapshot_for_mode(mm, "cloud"))
        automatic_answer = sk.answer_self_query("mode_query", _snapshot_for_mode(mm, "automatic"))
        return local_answer, cloud_answer, automatic_answer

    local_answer, cloud_answer, automatic_answer = _with_mode_manager(scenario)
    assert local_answer == "I am using Local Mode."
    assert cloud_answer == "I am using Cloud Mode."
    assert automatic_answer == "I am using automatic model selection based on task complexity."


# ============================================================
# PART 3 — Cloud Mode never reports a local model
# ============================================================
def test_cloud_mode_model_query_never_mentions_a_local_model():
    from backend.core import self_knowledge as sk
    from backend.core import model_registry

    def scenario(mm):
        snapshot = _snapshot_for_mode(mm, "cloud", cloud_provider="openai")
        answer = sk.answer_self_query("model_query", snapshot)
        capability_answer = sk.answer_self_query("capability_query", snapshot)
        for model_cfg in model_registry.get_all_models():
            assert model_cfg["id"] not in answer, f"model_query leaked local model_id {model_cfg['id']!r} in Cloud Mode: {answer!r}"
            assert model_cfg["id"] not in capability_answer, (
                f"capability_query leaked local model_id {model_cfg['id']!r} in Cloud Mode: {capability_answer!r}"
            )
        assert "(Local)" not in answer
        assert answer == "I am using OpenAI (Cloud)."

    _with_mode_manager(scenario)


# ============================================================
# PART 4 — Local Mode never reports a cloud provider
# ============================================================
def test_local_mode_model_query_never_mentions_a_cloud_provider():
    from backend.core import self_knowledge as sk
    from backend.llm.providers.provider_registry import get_provider_display_name

    def scenario(mm):
        snapshot = _snapshot_for_mode(mm, "local")
        answer = sk.answer_self_query("model_query", snapshot)
        capability_answer = sk.answer_self_query("capability_query", snapshot)
        for provider_name in ("openai", "anthropic", "grok", "gemini", "cohere"):
            display = get_provider_display_name(provider_name)
            assert display not in answer, f"model_query leaked cloud provider {display!r} in Local Mode: {answer!r}"
            assert display not in capability_answer, (
                f"capability_query leaked cloud provider {display!r} in Local Mode: {capability_answer!r}"
            )
        assert "(Cloud)" not in answer
        assert answer.endswith("(Local).")

    _with_mode_manager(scenario)


# ============================================================
# PART 5 — Automatic Model Selection reports the correct active model
# ============================================================
def test_automatic_mode_model_query_reports_the_persisted_default_model():
    from backend.core import self_knowledge as sk
    from backend.core import model_registry

    def scenario(mm):
        snapshot = _snapshot_for_mode(mm, "automatic")
        expected_id = model_registry.get_default_model_id()
        expected_cfg = model_registry.get_model(expected_id) if expected_id else None
        expected_name = (expected_cfg.get("name") if expected_cfg else None) or expected_id
        answer = sk.answer_self_query("model_query", snapshot)
        assert answer == f"I am using {expected_name} via automatic model selection.", answer

    _with_mode_manager(scenario)


# ============================================================
# PART 6 — exact legacy strings are fully removed (direct string checks)
# ============================================================
def test_exact_legacy_strings_removed_from_source():
    """
    Direct source-level guard, independent of any particular snapshot —
    confirms the literal legacy phrases are gone from
    self_knowledge.answer_self_query, not just unreachable for the
    fixtures the other tests happen to construct.
    """
    import inspect
    from backend.core import self_knowledge as sk

    source = inspect.getsource(sk.answer_self_query)
    for snippet in _LEGACY_SNIPPETS:
        assert snippet not in source, f"answer_self_query() source still contains legacy text {snippet!r}"


def test_conversation_manager_resolve_self_query_also_removed_legacy_text():
    """
    Section 5: conversation_manager.py's LLM-hint template (the dead-code
    fallback path, kept for defense-in-depth) must also be free of the
    legacy fallback/emergency/installed-models phrasing.
    """
    import inspect
    from backend.core import conversation_manager as cm

    source = inspect.getsource(cm.resolve_self_query)
    for snippet in ("fallback model", "emergency model", "installed models", "auto-switch between"):
        assert snippet not in source, f"resolve_self_query() source still contains legacy text {snippet!r}"


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
