# backend/core/packet_validation.py

"""
Batch 1 stability fix — validate mode_status_result / active_model_changed
payloads against backend.core.routing_invariants right before they go out
on the wire, so a bug in whatever assembled the payload (or a corrupted
persisted state slipping through) surfaces as a logged, structured error
instead of a malformed/contradictory packet reaching the frontend and
potentially killing the WebSocket message loop.

Both entry points here are last-line defense-in-depth: the actual
sources of truth (backend.core.routing_guard for switches,
backend.core.mode_manager's last-known-good self-heal for persisted
state) are expected to make these payloads valid before they're ever
built. Neither function raises — callers decide what "invalid" means
for their transport (return an error packet instead, skip sending,
etc.).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from backend.core import routing_invariants as inv


def validate_mode_status_payload(payload: Dict[str, Any]) -> Optional[str]:
    """
    Returns an error message if `payload` (a mode_status_result payload)
    is genuinely malformed/contradictory, else None.

    require_cloud_provider=False: routing_mode="cloud" with
    cloud_provider=None is a real, valid, already-gracefully-handled
    state to REPORT (the user is in Cloud Mode but hasn't configured a
    provider yet) — the strict "reject the switch" version of this rule
    lives at backend.core.routing_guard.attempt_mode_switch(), the only
    place actually allowed to CHANGE this state. What this function
    guards against is the actual contradiction: an active_model_id from
    the wrong registry for the current mode (e.g. a local model_id
    alongside routing_mode="cloud" — the reported "Cloud Mode +
    nemo-12b-q5" bug).
    """
    try:
        inv.validate_routing_state(inv.RoutingState(
            routing_mode=payload.get("routing_mode"),
            cloud_provider=payload.get("cloud_provider"),
            active_model_id=payload.get("active_model_id"),
            location=payload.get("location"),
        ), require_cloud_provider=False)
    except inv.RoutingInvariantError as e:
        return e.message
    return None


def validate_active_model_changed_payload(payload: Dict[str, Any]) -> Optional[str]:
    """
    Returns an error message if `payload` (an active_model_changed
    packet) is malformed, else None. Per Batch 1 spec: modelId and
    displayName must both be non-null — backend.core.streaming_engine
    is responsible for synthesizing a real (non-null) value for both in
    every case, including a cloud provider with no specific per-model
    id, so a null here means something upstream regressed, not a
    legitimate "no model chosen yet" state.
    """
    if payload.get("modelId") is None:
        return "active_model_changed: modelId must not be null."
    if payload.get("displayName") is None:
        return "active_model_changed: displayName must not be null."
    return None
