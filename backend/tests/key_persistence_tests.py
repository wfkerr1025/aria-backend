# backend/tests/key_persistence_tests.py
#
# Regression tests for Section 3's cloud-key fixes:
#   - saving a provider key (using Anthropic specifically, since that's
#     the one reported as "not persisting") round-trips correctly
#     through the real key_manager / OS secure storage, symmetrically
#     with openai
#   - self_knowledge.suggest_improvements() (Aria's "which cloud
#     providers have keys" self-report) reflects a live key-manager
#     state change immediately, not a hardcoded/stale list
#   - no provider (openai included) is ever reported as configured
#     without a real key actually being set
#
# Root-cause note: investigation found backend/core/key_manager.py,
# backend/ipc_router.py, webui/pages/models/models.js, and
# webui/components/settings/settings.js are all already correctly
# provider-agnostic — anthropic and openai go through byte-identical
# code paths, verified by a live WebSocket round-trip against the real
# backend. The actual bug was webui/pages/models/models.js's init()
# firing 15 redundant providers_list_request/modules_list_request calls
# (see webui/tests/models_page_regression_tests.mjs's new test for the
# fix) — a stale, late-arriving response could clobber a just-saved
# key's correct "configured: true" status. This file exists to pin down
# that the BACKEND side was never actually the problem, and to guard
# against it regressing.
#
# Self-contained plain-assert tests, matching backend/tests/skr_and_ipc_tests.py
# — not pytest. Mutates real OS secure storage for "anthropic" and
# "openai" test keys, always restoring original state in a finally
# block (checked first, same safety pattern
# backend/tests/routing_and_keys_tests.py already uses). Run directly:
#
#   python backend/tests/key_persistence_tests.py

from __future__ import annotations

import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _with_clean_test_key(provider: str, fn):
    """
    Runs fn() with `provider` guaranteed to start "not configured", then
    restores whatever was really there before (a real key if one
    existed, or nothing) — never destroys a real, pre-existing key.
    """
    from backend.core import key_manager

    had_real_key_before = key_manager.list_configured_providers().get(provider, False)
    real_key_value = key_manager.get_provider_key(provider) if had_real_key_before else None

    key_manager.delete_provider_key(provider)
    try:
        return fn()
    finally:
        if had_real_key_before and real_key_value:
            key_manager.set_provider_key(provider, real_key_value)
        else:
            key_manager.delete_provider_key(provider)


# ============================================================
# PART 1 — anthropic key round-trip, symmetric with openai
# ============================================================
def test_anthropic_key_save_and_read_round_trip():
    from backend.core import key_manager

    def scenario():
        assert key_manager.list_configured_providers().get("anthropic") is False, "expected anthropic to start not-configured"
        ok = key_manager.set_provider_key("anthropic", "sk-ant-test-key-12345")
        assert ok is True
        assert key_manager.list_configured_providers().get("anthropic") is True, "anthropic must show configured immediately after saving"
        assert key_manager.get_provider_key("anthropic") == "sk-ant-test-key-12345"

    _with_clean_test_key("anthropic", scenario)


def test_anthropic_and_openai_use_identical_code_path():
    """
    Not a real assertion about equality of VALUES — a structural check
    that saving/reading/deleting anthropic behaves exactly like openai
    (same return types, same round-trip behavior), guarding against a
    future provider-specific special-case being introduced.
    """
    from backend.core import key_manager

    def scenario():
        for provider, key_value in (("anthropic", "sk-ant-structural-test"), ("openai", "sk-openai-structural-test")):
            had_before = key_manager.list_configured_providers().get(provider, False)
            real_value = key_manager.get_provider_key(provider) if had_before else None
            key_manager.delete_provider_key(provider)
            try:
                assert key_manager.list_configured_providers().get(provider) is False
                assert key_manager.set_provider_key(provider, key_value) is True
                assert key_manager.list_configured_providers().get(provider) is True
                assert key_manager.get_provider_key(provider) == key_value
            finally:
                if had_before and real_value:
                    key_manager.set_provider_key(provider, real_value)
                else:
                    key_manager.delete_provider_key(provider)

    scenario()


def test_ipc_provider_key_set_request_persists_anthropic():
    """
    The same path webui/pages/models/models.js and
    webui/components/settings/settings.js actually use — through
    ipc_router.dispatch(), not calling key_manager directly.
    """
    from backend import ipc_router
    from backend.core import key_manager

    def scenario():
        result = ipc_router.dispatch({
            "type": "provider_key_set_request",
            "payload": {"provider": "anthropic", "api_key": "sk-ant-ipc-test"},
        })
        assert result["type"] == "provider_key_set_result"
        assert result["payload"]["ok"] is True

        status = ipc_router.dispatch({"type": "providers_list_request", "payload": {}})
        providers = {p["name"]: p["configured"] for p in status["payload"]["providers"]}
        assert providers["anthropic"] is True, f"expected anthropic configured=True via IPC round trip, got {providers}"

    _with_clean_test_key("anthropic", scenario)


# ============================================================
# PART 2 — no provider is ever reported configured without a real key
# ============================================================
def test_no_provider_reports_configured_without_a_real_key():
    from backend.core import key_manager

    status = key_manager.list_configured_providers()
    # openai is deliberately NOT special-cased here — every provider,
    # openai included, must come from a real check, never an assumed
    # "installed by default".
    for provider, configured in status.items():
        if configured:
            assert key_manager.get_provider_key(provider) or os.getenv(
                key_manager.PROVIDER_ENV_VARS.get(provider, "")
            ), f"{provider} reports configured=True but has neither a stored key nor an env var set"


# ============================================================
# PART 3 — self-report matches key_manager state exactly
# ============================================================
def test_self_report_reflects_live_key_state_not_a_hardcoded_list():
    from backend.core import self_knowledge

    def scenario():
        # With nothing configured, the suggestion must call out real gaps.
        text_before = " ".join(self_knowledge.suggest_improvements())

        from backend.core import key_manager
        key_manager.set_provider_key("anthropic", "sk-ant-selfreport-test")

        text_after = " ".join(self_knowledge.suggest_improvements())
        assert text_before != text_after, "suggest_improvements() must change when live key state changes, not return a cached/hardcoded answer"

    _with_clean_test_key("anthropic", scenario)


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
