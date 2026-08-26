# backend/core/routing_invariants.py

"""
Batch 1 stability fix — hard routing invariants.

Single source of truth for "is this routing_mode/cloud_provider/
active_model_id/location combination even possible". Before this, the
combination was never checked as a whole anywhere: ModeManager
(mode_manager.py) is a plain state store with no validation of its own,
and callers (backend/server.py, backend/websocket/handlers.py) set mode
and cloud_provider as two separate, uncoordinated writes. That's how
backend/config/mode_state.json was found on disk as
{"mode": "cloud", "cloud_provider": null} — a state that then made
ipc_router.py's mode_status_result report the LOCAL default model_id
(get_active_model_id() is mode-unaware) while routing_mode said
"cloud": the exact "Cloud Mode + nemo-12b-q5" contradiction reported.

validate_routing_state() below is deliberately pure (no I/O, no
mutation) so it can be called both BEFORE a switch is applied
(backend.core.routing_guard — reject the switch, keep the previous
state) and on ANY already-persisted state (backend.core.mode_manager's
last-known-good self-heal on load, ipc_router's pre-send check) without
caring which caller it is.

Only "local" and "cloud" are constrained. "automatic" is exempt by
design across this codebase (see model_registry.model_violates_mode_
separation()'s identical exemption) — it re-resolves per message via
ProviderRouter/AutoSelector, so there is no single persisted
active_model_id/location for it to be consistent with.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from backend.core import model_registry

VALID_MODES = ("automatic", "local", "cloud")
VALID_LOCATIONS = ("local", "cloud")


@dataclass(frozen=True)
class RoutingState:
    routing_mode: str
    cloud_provider: Optional[str] = None
    active_model_id: Optional[str] = None
    location: Optional[str] = None


class RoutingInvariantError(Exception):
    """
    Raised by validate_routing_state() when a RoutingState combination is
    structurally impossible. `code` is one of the constants below —
    callers surface it via backend.ipc_errors.ROUTING_INVARIANT_VIOLATION
    (or NO_CLOUD_PROVIDER, the one case with its own dedicated code
    because it's the specific bug this batch was written to fix) so the
    UI can distinguish "no provider configured" from other violations
    without string-matching the message.
    """

    INVALID_MODE = "INVALID_MODE"
    CLOUD_PROVIDER_REQUIRED = "CLOUD_PROVIDER_REQUIRED"
    LOCATION_MISMATCH = "LOCATION_MISMATCH"
    UNKNOWN_MODEL = "UNKNOWN_MODEL"
    MODEL_MODE_MISMATCH = "MODEL_MODE_MISMATCH"

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __repr__(self) -> str:
        return f"RoutingInvariantError({self.code!r}, {self.message!r})"


def _is_real_local_model(model_id: str) -> bool:
    """True if `model_id` is a REAL entry in the local file-backed registry."""
    cfg = model_registry.get_model(model_id)
    return cfg is not None and cfg.get("provider") == "local"


def validate_routing_state(state: RoutingState, require_cloud_provider: bool = True) -> None:
    """
    Raise RoutingInvariantError if `state` violates a hard invariant.
    Returns None (does not raise) for a valid state — including any
    "automatic" state, which is unconstrained.

    `require_cloud_provider` defaults to True — the strict rule
    backend.core.routing_guard.attempt_mode_switch() enforces at the
    moment of SWITCHING into Cloud Mode: a switch that can't resolve a
    real, configured provider is rejected outright, which is what
    actually stops a null cloud_provider from ever being created again.

    Callers validating an ALREADY-persisted or already-broadcast state
    (backend.core.packet_validation, for mode_status_result /
    active_model_changed sanity) pass require_cloud_provider=False:
    routing_mode="cloud" with cloud_provider=None is a real, valid,
    already-gracefully-handled state in this codebase (the user is in
    Cloud Mode but hasn't configured a provider yet — see
    backend.core.provider_router.ProviderRouter.resolve()'s cloud
    branch and the structured safety_warning backend/websocket/
    handlers.py sends for it), not a malformed packet — only an
    active_model_id from the WRONG registry for the current mode is an
    actual contradiction worth blocking a packet over.
    """
    mode = state.routing_mode

    if mode not in VALID_MODES:
        raise RoutingInvariantError(
            RoutingInvariantError.INVALID_MODE, f"Unknown routing_mode: {mode!r}"
        )

    if mode == "automatic":
        return

    if mode == "cloud":
        if require_cloud_provider and not state.cloud_provider:
            raise RoutingInvariantError(
                RoutingInvariantError.CLOUD_PROVIDER_REQUIRED,
                "Cloud Mode requires a non-null cloud_provider.",
            )
        if state.location is not None and state.location != "cloud":
            raise RoutingInvariantError(
                RoutingInvariantError.LOCATION_MISMATCH,
                f"Cloud Mode requires location='cloud', got {state.location!r}.",
            )
        if state.active_model_id is not None and _is_real_local_model(state.active_model_id):
            # Batch 2 note: this is deliberately NOT
            # model_registry.model_violates_mode_separation() (which
            # treats ANY non-null model_id as a violation in Cloud
            # Mode) — Batch 2 gives Cloud Mode real model ids of its
            # own (backend.core.model_selector.select_cloud_model(),
            # e.g. "gpt-4"), which are unknown to model_registry (a
            # LOCAL, file-backed registry) and so must NOT be flagged
            # here. Only an id that resolves to a REAL local registry
            # entry is the actual contradiction ("nemo-12b-q5" showing
            # up in Cloud Mode).
            raise RoutingInvariantError(
                RoutingInvariantError.MODEL_MODE_MISMATCH,
                f"active_model_id {state.active_model_id!r} is a local model and cannot be "
                f"active while routing_mode='cloud'.",
            )
        return

    # mode == "local"
    if state.location is not None and state.location != "local":
        raise RoutingInvariantError(
            RoutingInvariantError.LOCATION_MISMATCH,
            f"Local Mode requires location='local', got {state.location!r}.",
        )
    if state.active_model_id is not None:
        if model_registry.model_violates_mode_separation(state.active_model_id, "local"):
            raise RoutingInvariantError(
                RoutingInvariantError.MODEL_MODE_MISMATCH,
                f"active_model_id {state.active_model_id!r} is not a local model and cannot be "
                f"active while routing_mode='local'.",
            )
        # Symmetric case: a known CLOUD model id (e.g. "gpt-4" from
        # model_selector.CLOUD_DEFAULT_MODELS) surviving into
        # routing_mode="local" — model_violates_mode_separation() above
        # can't catch this because these ids were never in
        # model_registry (a local-only registry) to begin with.
        from backend.core import model_selector
        if model_selector.is_cloud_model_id(state.active_model_id):
            raise RoutingInvariantError(
                RoutingInvariantError.MODEL_MODE_MISMATCH,
                f"active_model_id {state.active_model_id!r} is a cloud model and cannot be "
                f"active while routing_mode='local'.",
            )


def is_valid_routing_state(state: RoutingState) -> bool:
    """Non-raising convenience wrapper — True if `state` satisfies every invariant."""
    try:
        validate_routing_state(state)
        return True
    except RoutingInvariantError:
        return False
