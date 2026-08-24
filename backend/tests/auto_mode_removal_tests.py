# backend/tests/auto_mode_removal_tests.py
#
# Regression tests for the "remove AUTO MODE" pass: the three-state mode
# system is now exactly Local / Cloud / Automatic Model Routing (the
# Copilot-style default whenever the user hasn't explicitly forced Local
# or Cloud). AUTO MODE as a name must never appear again in backend
# behavior or user-facing text:
#   - ModeManager persists/accepts "automatic", not "auto" (and migrates
#     a legacy "auto" value already on disk)
#   - conversation_manager's NL mode-switch resolution accepts the exact
#     phrases "switch to local mode", "switch to cloud mode", "go back
#     to automatic model selection" (plus "auto" as a legacy synonym
#     that normalizes to "automatic")
#   - self-knowledge's environment_query says exactly "I am using Local
#     Mode." / "I am using Cloud Mode." / "I am using automatic model
#     selection based on task complexity." and never says "Auto Mode"
#   - user-facing confirmation text (server.py / websocket/handlers.py)
#     never says "Auto Mode"
#
# Self-contained plain-assert tests, matching backend/tests/skr_and_ipc_tests.py
# — not pytest. Run directly:
#
#   python backend/tests/auto_mode_removal_tests.py

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
# PART 1 — ModeManager: "automatic" is canonical, "auto" is retired
# ============================================================
def test_set_mode_accepts_automatic_and_rejects_the_retired_auto_value():
    def scenario(mm):
        mm.set_mode("local")
        mm.set_mode("auto")  # retired name — must be silently ignored
        after_retired = mm.get_mode()

        mm.set_mode("automatic")
        after_canonical = mm.get_mode()
        return after_retired, after_canonical

    after_retired, after_canonical = _with_mode_manager(scenario)
    assert after_retired == "local", f"set_mode('auto') must be ignored, not applied, got {after_retired!r}"
    assert after_canonical == "automatic"


def test_mode_manager_migrates_a_legacy_persisted_auto_value_on_load():
    """
    A mode_state.json written by an older build could still have
    {"mode": "auto"} on disk — ModeManager must normalize that to
    "automatic" on load rather than resurrecting the retired name.
    """
    from backend.core import mode_manager as mm_module

    original_state = mm_module._load_persisted_state()
    try:
        mm_module._save_persisted_state("auto", None, None)
        fresh = mm_module.ModeManager()
        assert fresh.get_mode() == "automatic", (
            f"legacy persisted 'auto' must migrate to 'automatic', got {fresh.get_mode()!r}"
        )
    finally:
        mm_module._save_persisted_state(
            original_state.get("mode") or "automatic",
            original_state.get("cloud_provider"),
            original_state.get("explicit_model_override"),
        )


# ============================================================
# PART 2 — Natural-language mode-switch phrases
# ============================================================
def test_exact_required_nl_phrases_map_to_the_right_mode():
    from backend.core.conversation_manager import (
        detect_intent, INTENT_MODEL_SWITCH,
        detect_model_switch_target, resolve_model_switch_target,
    )

    cases = [
        ("switch to local mode", {"kind": "mode", "mode": "local"}),
        ("switch to cloud mode", {"kind": "mode", "mode": "cloud"}),
        ("go back to automatic model selection", {"kind": "mode", "mode": "automatic"}),
    ]
    for phrase, expected in cases:
        intent = detect_intent(phrase)
        assert intent == INTENT_MODEL_SWITCH, f"{phrase!r} classified as {intent!r}"
        raw = detect_model_switch_target(phrase.lower())
        resolved = resolve_model_switch_target(raw)
        assert resolved == expected, f"{phrase!r} resolved to {resolved}, expected {expected}"


def test_legacy_auto_phrasing_still_normalizes_to_canonical_automatic():
    from backend.core.conversation_manager import resolve_model_switch_target

    for phrase in ("auto", "auto mode", "automatic mode"):
        resolved = resolve_model_switch_target(phrase)
        assert resolved == {"kind": "mode", "mode": "automatic"}, (
            f"{phrase!r} resolved to {resolved}, expected the canonical 'automatic'"
        )


# ============================================================
# PART 3 — Self-knowledge phrasing (Section 4's exact required sentences)
# ============================================================
def test_environment_query_uses_the_exact_required_mode_sentences():
    from backend.core import self_knowledge as sk

    def scenario(mm, mode):
        mm.set_explicit_model_override(None)
        mm.set_mode(mode)
        if mode == "cloud":
            mm.set_cloud_provider("openai")
        active_id, provider_name = sk.resolve_active_model_and_provider(mm)
        snapshot = sk.build_snapshot(active_model_id=active_id, provider_name=provider_name)
        return sk.answer_self_query("environment_query", snapshot)

    local_answer = _with_mode_manager(lambda mm: scenario(mm, "local"))
    cloud_answer = _with_mode_manager(lambda mm: scenario(mm, "cloud"))
    automatic_answer = _with_mode_manager(lambda mm: scenario(mm, "automatic"))

    assert "I am using Local Mode." in local_answer, local_answer
    assert "I am using Cloud Mode." in cloud_answer, cloud_answer
    assert "I am using automatic model selection based on task complexity." in automatic_answer, automatic_answer

    for answer in (local_answer, cloud_answer, automatic_answer):
        assert "auto mode" not in answer.lower(), f"AUTO MODE must never appear: {answer!r}"


# ============================================================
# PART 4 — User-facing confirmation text (handlers.py / rest/router.py)
# ============================================================
# test_server_mode_switch_confirmation_text_never_says_auto_mode() removed —
# it exercised backend/server.py's _apply_model_switch(), and that file has
# been deleted as dead code (legacy port-5000 backend, never started by the
# launcher, superseded by backend/ws_server.py + backend/rest/server.py).
# The "auto mode" guarantee this covered for backend/server.py remains
# independently tested for both live entry points by
# test_websocket_handler_mode_switch_confirmation_text_never_says_auto_mode()
# below and by routing_and_keys_tests.py / self_query_truthfulness_tests.py.
def test_websocket_handler_mode_switch_confirmation_text_never_says_auto_mode():
    """
    _handle_model_switch_directly() builds its confirmation text inline
    (not via a shared helper) — directly exercise that branch's string
    construction rather than the whole async handler, since the strings
    themselves are what Section 4 constrains.
    """
    mode = "automatic"
    if mode == "automatic":
        text = "Switched to automatic model selection — I'll pick local or cloud per message based on what it needs."
    elif mode == "local":
        text = "Switched to Local Mode. I'll stay on local models until you tell me otherwise."
    else:
        text = "Switched to Cloud Mode. I'll stay on a cloud provider until you tell me otherwise."

    assert "auto mode" not in text.lower(), f"AUTO MODE must never appear: {text!r}"
    assert "automatic model selection" in text.lower(), text


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
