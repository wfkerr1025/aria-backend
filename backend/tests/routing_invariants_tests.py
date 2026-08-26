# backend/tests/routing_invariants_tests.py
#
# Regression tests for the Batch 1 stability fixes:
#   - backend.core.routing_invariants — hard routing invariants
#   - backend.core.routing_guard — the only path allowed to apply a
#     mode/provider/model-override switch; rejects an invalid switch and
#     leaves ModeManager untouched
#   - backend.core.provider_config — single-source-of-truth provider
#     registry backing Cloud Mode provider resolution
#   - ipc_router.mode_status_result is mode-aware (no more reporting a
#     local model_id while routing_mode="cloud" — the literal
#     {"mode": "cloud", "cloud_provider": null} state found on disk)
#   - backend.core.packet_validation — active_model_changed's
#     modelId/displayName must never be null
#
# Self-contained plain-assert tests, matching backend/tests/mode_truthfulness_tests.py
# and backend/tests/routing_and_keys_tests.py — not pytest. Run directly:
#
#   python backend/tests/routing_invariants_tests.py

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
# PART 1 — routing_invariants.validate_routing_state()
# ============================================================
def test_cloud_mode_requires_non_null_provider():
    from backend.core import routing_invariants as inv

    try:
        inv.validate_routing_state(inv.RoutingState(routing_mode="cloud", cloud_provider=None))
        assert False, "expected RoutingInvariantError for cloud_provider=None"
    except inv.RoutingInvariantError as e:
        assert e.code == inv.RoutingInvariantError.CLOUD_PROVIDER_REQUIRED, e.code


def test_cloud_mode_with_valid_provider_is_valid():
    from backend.core import routing_invariants as inv

    inv.validate_routing_state(inv.RoutingState(
        routing_mode="cloud", cloud_provider="anthropic", active_model_id=None, location="cloud",
    ))  # must not raise


def test_cloud_mode_rejects_local_active_model():
    from backend.core import routing_invariants as inv
    from backend.core.model_registry import get_default_model_id

    local_id = get_default_model_id()
    assert local_id, "test requires at least one registered local model"

    try:
        inv.validate_routing_state(inv.RoutingState(
            routing_mode="cloud", cloud_provider="openai", active_model_id=local_id, location="cloud",
        ))
        assert False, f"expected RoutingInvariantError — {local_id!r} is a local model"
    except inv.RoutingInvariantError as e:
        assert e.code == inv.RoutingInvariantError.MODEL_MODE_MISMATCH, e.code


def test_local_mode_rejects_location_mismatch():
    from backend.core import routing_invariants as inv

    try:
        inv.validate_routing_state(inv.RoutingState(routing_mode="local", location="cloud"))
        assert False, "expected RoutingInvariantError for location mismatch"
    except inv.RoutingInvariantError as e:
        assert e.code == inv.RoutingInvariantError.LOCATION_MISMATCH, e.code


def test_automatic_mode_is_unconstrained():
    from backend.core import routing_invariants as inv

    # No cloud_provider, no location, arbitrary active_model_id — none of
    # this is checked under Automatic Model Routing, by design.
    inv.validate_routing_state(inv.RoutingState(
        routing_mode="automatic", cloud_provider=None, active_model_id=None, location=None,
    ))


def test_invalid_mode_string_rejected():
    from backend.core import routing_invariants as inv

    try:
        inv.validate_routing_state(inv.RoutingState(routing_mode="quantum"))
        assert False, "expected RoutingInvariantError for an unknown mode"
    except inv.RoutingInvariantError as e:
        assert e.code == inv.RoutingInvariantError.INVALID_MODE, e.code


# ============================================================
# PART 2 — routing_guard.attempt_mode_switch() / attempt_model_override_switch()
# ============================================================
def test_cloud_switch_rejected_with_no_provider_configured_leaves_state_untouched():
    from backend.core import routing_guard, provider_config

    def scenario(mm):
        mm.set_mode("local")
        mm.set_cloud_provider(None)

        original_resolve = provider_config.resolve_cloud_provider
        provider_config.resolve_cloud_provider = lambda requested: (None, "no providers configured (test)")
        try:
            result = routing_guard.attempt_mode_switch(mm, "cloud")
        finally:
            provider_config.resolve_cloud_provider = original_resolve

        assert result.ok is False
        assert result.error_code == "NO_CLOUD_PROVIDER", result.error_code
        # The whole point: a rejected switch must NOT touch ModeManager state.
        assert mm.get_mode() == "local", f"mode must be unchanged after rejection, got {mm.get_mode()!r}"
        assert mm.get_cloud_provider() is None

    _with_mode_manager(scenario)


def test_cloud_switch_with_valid_provider_persists_it():
    from backend.core import routing_guard, provider_config, model_selector

    def scenario(mm):
        mm.set_mode("local")

        original_resolve = provider_config.resolve_cloud_provider
        provider_config.resolve_cloud_provider = lambda requested: ("anthropic", None)
        original_select_cloud_model = model_selector.select_cloud_model
        fake_model_info = model_selector.ModelInfo(
            model_id="claude-3-opus", display_name="Claude 3 Opus", provider="anthropic",
            provider_display_name="Anthropic", location="cloud",
        )
        routing_guard.model_selector.select_cloud_model = lambda provider_name: fake_model_info
        try:
            result = routing_guard.attempt_mode_switch(mm, "cloud")
        finally:
            provider_config.resolve_cloud_provider = original_resolve
            routing_guard.model_selector.select_cloud_model = original_select_cloud_model

        assert result.ok is True
        assert result.cloud_provider == "anthropic"
        assert result.model_id == "claude-3-opus", result.model_id
        assert mm.get_mode() == "cloud"
        assert mm.get_cloud_provider() == "anthropic"

    _with_mode_manager(scenario)


def test_local_switch_clears_cloud_provider():
    from backend.core import routing_guard

    def scenario(mm):
        mm.set_mode("cloud")
        mm.set_cloud_provider("openai")

        result = routing_guard.attempt_mode_switch(mm, "local")

        assert result.ok is True
        assert mm.get_mode() == "local"
        assert mm.get_cloud_provider() is None

    _with_mode_manager(scenario)


def test_model_override_rejected_when_incompatible_with_current_mode():
    from backend.core import routing_guard

    def scenario(mm):
        mm.set_mode("cloud")
        mm.set_cloud_provider("openai")
        mm.set_explicit_model_override(None)

        from backend.core.model_registry import get_default_model_id
        local_id = get_default_model_id()
        assert local_id

        result = routing_guard.attempt_model_override_switch(mm, local_id)

        assert result.ok is False
        assert result.error_code == "MODEL_MODE_MISMATCH", result.error_code
        assert mm.get_explicit_model_override() is None, "rejected override must not be persisted"

    _with_mode_manager(scenario)


def test_model_override_accepted_when_compatible_with_current_mode():
    from backend.core import routing_guard
    from backend.core.model_registry import get_default_model_id

    def scenario(mm):
        mm.set_mode("local")
        local_id = get_default_model_id()
        assert local_id

        result = routing_guard.attempt_model_override_switch(mm, local_id)

        assert result.ok is True
        assert mm.get_explicit_model_override() == local_id

    _with_mode_manager(scenario)


# ============================================================
# PART 3 — ipc_router.mode_status_result is mode-aware
# ============================================================
def test_mode_status_result_cloud_mode_never_reports_local_model_id():
    from backend import ipc_router
    from backend.core.model_registry import get_default_model_id

    def scenario(mm):
        mm.set_explicit_model_override(None)
        mm.set_mode("cloud")
        mm.set_cloud_provider("anthropic")
        return ipc_router.dispatch({"type": "mode_status_request", "payload": {}})

    result = _with_mode_manager(scenario)
    payload = result["payload"]
    local_default = get_default_model_id()

    assert payload["active_model_id"] != local_default, (
        f"mode_status_result reported the LOCAL default model_id "
        f"({local_default!r}) while routing_mode='cloud' — the exact "
        f"'Cloud Mode + {local_default}' contradiction this fix addresses"
    )
    assert payload["cloud_provider"] == "anthropic"
    assert payload["location"] == "cloud", payload


def test_diagnostics_models_result_cloud_mode_never_reports_local_model_id():
    """
    Same class of bug as mode_status_result, found at the same time in
    ipc_router._handle_diagnostics_models() and backend/rest/router.py's
    get_diagnostics_models() — both used
    ModeManager().get_explicit_model_override() or get_active_model_id()
    for "active_chat_model_id", which is mode-unaware.
    """
    from backend import ipc_router
    from backend.core.model_registry import get_default_model_id

    def scenario(mm):
        mm.set_explicit_model_override(None)
        mm.set_mode("cloud")
        mm.set_cloud_provider("openai")
        return ipc_router.dispatch({"type": "diagnostics_models_request", "payload": {}})

    result = _with_mode_manager(scenario)
    local_default = get_default_model_id()
    assert result["payload"]["active_chat_model_id"] != local_default, result["payload"]


def test_mode_status_result_local_mode_reports_location_local():
    from backend import ipc_router

    def scenario(mm):
        mm.set_explicit_model_override(None)
        mm.set_mode("local")
        return ipc_router.dispatch({"type": "mode_status_request", "payload": {}})

    result = _with_mode_manager(scenario)
    assert result["payload"]["location"] == "local", result["payload"]


# ============================================================
# PART 4 — packet_validation
# ============================================================
def test_active_model_changed_validation_rejects_null_model_id():
    from backend.core import packet_validation

    error = packet_validation.validate_active_model_changed_payload({
        "modelId": None, "displayName": "OpenAI",
    })
    assert error is not None


def test_active_model_changed_validation_accepts_synthesized_cloud_id():
    from backend.core import packet_validation

    error = packet_validation.validate_active_model_changed_payload({
        "modelId": "cloud:openai", "displayName": "OpenAI",
    })
    assert error is None, error


def test_mode_status_payload_validation_allows_null_cloud_provider():
    """
    routing_mode="cloud" + cloud_provider=None is a real, valid,
    already-gracefully-handled state to REPORT (the user is in Cloud
    Mode but hasn't configured a provider yet — see
    backend/websocket/handlers.py's structured safety_warning for it) —
    only the switch-time gate (routing_guard.attempt_mode_switch)
    rejects it; mode_status_result must still be able to report it.
    """
    from backend.core import packet_validation

    error = packet_validation.validate_mode_status_payload({
        "routing_mode": "cloud", "cloud_provider": None, "active_model_id": None, "location": "cloud",
    })
    assert error is None, error


def test_mode_status_payload_validation_catches_local_model_id_in_cloud_mode():
    from backend.core import packet_validation
    from backend.core.model_registry import get_default_model_id

    local_id = get_default_model_id()
    assert local_id, "test requires at least one registered local model"

    error = packet_validation.validate_mode_status_payload({
        "routing_mode": "cloud", "cloud_provider": "openai", "active_model_id": local_id, "location": "cloud",
    })
    assert error is not None


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
