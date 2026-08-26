# backend/core/routing_guard.py

"""
Batch 1 stability fix — the ONLY path allowed to apply a routing_mode /
cloud_provider / explicit-model-override switch.

Before this module, backend/server.py's _apply_model_switch() and
backend/websocket/handlers.py's _handle_model_switch_directly() each
called ModeManager.set_mode() / set_cloud_provider() /
set_explicit_model_override() directly and unconditionally — a bare
"switch to cloud mode" (no provider named) landed the backend in
routing_mode="cloud", cloud_provider=None with nothing to stop it, and
"switch to <a local model>" while already in Cloud Mode pinned an
explicit_model_override incompatible with the mode with no rejection at
all (it was silently ignored later, at USE time, by ProviderRouter/
self_knowledge — functionally harmless, but mode_status_result still
displayed the contradictory pin).

Every function here resolves the FULL resulting state, validates it
against backend.core.routing_invariants before touching ModeManager,
and — on failure — returns a SwitchResult with ok=False and leaves
ModeManager completely untouched (the "keep the previous stable state"
requirement). Callers must check `.ok` and send the caller's own
structured error response instead of any success packet when it's
False; nothing here sends packets itself, so this module has zero
transport-layer dependencies and is usable from both the WebSocket
handler and the REST path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from backend.core.mode_manager import ModeManager
from backend.core import model_registry
from backend.core import model_selector
from backend.core import provider_config
from backend.core import routing_invariants as inv
from backend.logger import log as unified_log

from logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class SwitchResult:
    ok: bool
    mode: Optional[str] = None
    cloud_provider: Optional[str] = None
    model_id: Optional[str] = None
    display_name: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


def _reject(code: str, message: str, **extra) -> SwitchResult:
    logger.warning(f"routing_guard rejected switch: {code}: {message}")
    unified_log("routing_guard", "WARNING", f"Routing switch rejected: {code}", {
        "message": message, **extra,
    })
    return SwitchResult(ok=False, error_code=code, error_message=message)


def attempt_mode_switch(mode_manager: ModeManager, mode: str, provider: Optional[str] = None) -> SwitchResult:
    """
    Validate and, only if valid, apply a routing_mode switch (optionally
    naming a cloud provider in the same call — the "switch to <provider>"
    phrasing resolves to kind="provider", which also lands here with
    mode="cloud").

    Batch 2: beyond Batch 1's provider-only check, this now also runs the
    switch through backend.core.model_selector — a switch into Cloud
    Mode isn't accepted unless select_cloud_model() can name a real
    model for the resolved provider (CLOUD_MODEL_RESOLUTION_FAILED
    otherwise), and a switch into Local Mode isn't accepted unless
    select_local_model(None) can name a real default local model
    (LOCAL_MODEL_INVALID otherwise, e.g. an empty registry). Either way,
    a rejected switch leaves ModeManager completely untouched.

    mode_manager.set_mode() already clears any explicit_model_override on
    every real mode change (see mode_manager.py) — a stale override can
    therefore never survive a mode switch, so this function does not
    need to separately validate active_model_id against the target mode.
    """
    if mode not in inv.VALID_MODES:
        return _reject(inv.RoutingInvariantError.INVALID_MODE, f"Unknown mode: {mode!r}", requested_mode=mode)

    resolved_provider: Optional[str] = None
    location: Optional[str] = None
    model_info: Optional[model_selector.ModelInfo] = None

    if mode == "cloud":
        candidate = provider or mode_manager.get_cloud_provider()
        resolved_provider, err = provider_config.resolve_cloud_provider(candidate)
        if resolved_provider is None:
            return _reject(
                "NO_CLOUD_PROVIDER",
                err or "No configured cloud provider is available.",
                requested_provider=candidate,
            )

        selection = model_selector.select_cloud_model(resolved_provider)
        if isinstance(selection, model_selector.SelectionError):
            return _reject(selection.code, selection.message, requested_provider=resolved_provider)
        model_info = selection
        location = "cloud"

    elif mode == "local":
        selection = model_selector.select_local_model(None)
        if isinstance(selection, model_selector.SelectionError):
            return _reject(selection.code, selection.message)
        model_info = selection
        location = "local"

    try:
        inv.validate_routing_state(inv.RoutingState(
            routing_mode=mode,
            cloud_provider=resolved_provider,
            active_model_id=None,  # override is cleared by set_mode() regardless
            location=location,
        ))
    except inv.RoutingInvariantError as e:
        return _reject(e.code, e.message, requested_mode=mode, requested_provider=provider)

    mode_manager.set_mode(mode)
    if mode == "cloud":
        mode_manager.set_cloud_provider(resolved_provider)
    else:
        mode_manager.set_cloud_provider(None)

    unified_log("routing_guard", "INFO", "Routing switch applied", {
        "mode": mode, "cloud_provider": resolved_provider,
        "model_id": model_info.model_id if model_info else None,
    })
    return SwitchResult(
        ok=True, mode=mode, cloud_provider=resolved_provider,
        model_id=model_info.model_id if model_info else None,
        display_name=model_info.display_name if model_info else None,
    )


def attempt_model_override_switch(mode_manager: ModeManager, model_id: str) -> SwitchResult:
    """
    Validate and, only if valid, pin an explicit "switch to <model>"
    override for the CURRENT routing_mode. Rejects a model_id that
    belongs to the wrong registry for the current mode (e.g. a local
    model while routing_mode="cloud") instead of silently pinning an
    override that downstream resolution would have ignored anyway —
    mode_status_result must never display a pin that isn't actually
    honored. When the current mode allows a local override (local or
    automatic), the model_id is additionally validated through
    backend.core.model_selector.select_local_model() — an unknown
    model_id is rejected with LOCAL_MODEL_INVALID rather than silently
    pinned.
    """
    mode = mode_manager.get_mode()

    if mode != "automatic" and model_registry.model_violates_mode_separation(model_id, mode):
        return _reject(
            inv.RoutingInvariantError.MODEL_MODE_MISMATCH,
            f"Model '{model_id}' is not valid in the current '{mode}' mode.",
            requested_model_id=model_id,
        )

    selection = model_selector.select_local_model(model_id)
    if isinstance(selection, model_selector.SelectionError):
        return _reject(selection.code, selection.message, requested_model_id=model_id)

    mode_manager.set_explicit_model_override(model_id)
    unified_log("routing_guard", "INFO", "Explicit model override applied", {"model_id": model_id})
    return SwitchResult(ok=True, mode=mode, model_id=selection.model_id, display_name=selection.display_name)
