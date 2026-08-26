# backend/core/mode_manager.py

import json
import os

from backend.logger import log as unified_log

from logger import get_logger

logger = get_logger(__name__)

_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "mode_state.json"
)


def _load_persisted_state() -> dict:
    if not os.path.exists(_STATE_PATH):
        return {}
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("mode_manager: failed to read persisted state, defaulting to automatic")
        return {}
    # Migration: older builds persisted the automatic-routing mode as
    # "auto" before it was renamed to "automatic". Normalize on load so
    # a pre-existing mode_state.json doesn't resurrect the retired name.
    if state.get("mode") == "auto":
        state["mode"] = "automatic"
    return state


def _save_persisted_state(mode: str, cloud_provider_name: str | None, explicit_model_override: str | None) -> None:
    os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
    with open(_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "mode": mode,
            "cloud_provider": cloud_provider_name,
            "explicit_model_override": explicit_model_override,
        }, f, indent=2)


class ModeManager:
    """
    Mode manager for the three-state mode system: Local, Cloud, and
    Automatic Model Routing (Copilot-style task-complexity-based
    selection across all models — see
    backend.core.provider_router.ProviderRouter and
    backend.core.auto_selector.AutoSelector), plus the explicit
    per-session model override ("switch to X" — see
    conversation_manager.resolve_model_switch_target() and
    backend/websocket/handlers.py's _handle_model_switch_directly()).
    Automatic Model Routing is the default whenever the user hasn't
    explicitly forced Local or Cloud Mode.

    Persisted to backend/config/mode_state.json rather than kept purely
    in-memory: a user saying "switch to Local Mode" (or "switch to
    mistral-7b") is expected to stick — permanently, across reconnects
    and restarts, the same way an explicit active-model switch already
    persists to models.json (see backend.core.model_manager.
    set_active_model) — not just for the rest of one WebSocket
    connection. Every WebSocketHandler constructs its own
    ProviderRouter/ModeManager per connection (see backend/websocket/
    handlers.py), so without this, reconnecting would silently reset
    the user back to "automatic" every time.
    """

    def __init__(self):
        state = _load_persisted_state()
        self._mode = state.get("mode") or "automatic"
        self._cloud_provider_name: str | None = state.get("cloud_provider")
        self._explicit_model_override: str | None = state.get("explicit_model_override")
        logger.debug(
            f"Initializing ModeManager (mode={self._mode}, cloud_provider={self._cloud_provider_name}, "
            f"explicit_model_override={self._explicit_model_override})"
        )
        self._self_heal_if_invalid()

    def _self_heal_if_invalid(self) -> None:
        """
        Batch 1 stability fix — "last-known-good routing state". A
        mode_state.json written before this fix (or hand-edited, or from
        a future build with a mode name this one doesn't know) can name
        a routing_mode that isn't one of the three real modes at all —
        every consumer of get_mode() assumes it's always "automatic",
        "local", or "cloud" (see e.g. provider_router.py's if/elif
        chain, which would otherwise silently fall through to the
        Automatic Model Routing branch anyway, just without ever having
        SAID so). Resets to Automatic Model Routing in that case,
        persisting the fix so it only has to happen once.

        Deliberately does NOT also reject routing_mode="cloud" paired
        with cloud_provider=None: that combination is a valid, already
        gracefully-handled state (see backend.core.provider_router.
        ProviderRouter.resolve()'s cloud branch, which returns
        (None, None) for it, and backend/websocket/handlers.py, which
        turns that into a structured safety_warning — "Cloud Mode, but
        no provider is configured" is a real, informative, non-crashing
        state, not a bug) — see backend.core.routing_guard for where
        cloud_provider IS required: at the moment of SWITCHING into
        Cloud Mode, which is the one place this codebase can actually
        choose a good provider or reject the switch, rather than
        silently discarding the user's mode choice on every unrelated
        internal ModeManager() read.
        """
        # Deferred import: routing_invariants -> model_registry would
        # otherwise be a circular import at module load time (it sits
        # below mode_manager in the dependency graph but is only needed
        # here, once, at construction).
        from backend.core import routing_invariants as inv

        if self._mode not in inv.VALID_MODES:
            logger.error(f"ModeManager: persisted mode {self._mode!r} is not a recognized routing_mode — resetting to automatic")
            unified_log("mode_manager", "ERROR", "Persisted routing_mode was invalid — self-healed to automatic", {
                "previous_mode": self._mode,
            })
            self._mode = "automatic"
            self._cloud_provider_name = None
            self._explicit_model_override = None
            self._save()

    def _save(self) -> None:
        _save_persisted_state(self._mode, self._cloud_provider_name, self._explicit_model_override)

    def reset_session(self) -> None:
        logger.debug("reset_session() called → mode=automatic, cloud_provider=None, override=None")
        self._mode = "automatic"
        self._cloud_provider_name = None
        self._explicit_model_override = None
        self._save()

    def get_mode(self) -> str:
        logger.debug(f"get_mode() → {self._mode}")
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in ("automatic", "local", "cloud"):
            logger.debug(f"set_mode() ignored → invalid mode '{mode}'")
            unified_log("mode_manager", "WARNING", f"Ignored invalid mode: {mode}")
            return
        logger.debug(f"set_mode() → {mode}")
        unified_log("mode_manager", "INFO", f"Mode changed: {self._mode} -> {mode}", {
            "previous_mode": self._mode, "new_mode": mode,
        })
        self._mode = mode
        # A mode switch ("switch to Local/Cloud Mode" or "go back to
        # automatic model selection") always clears
        # any explicit "switch to <model>" pin — set_mode() is only ever
        # called from that one user-facing action (see
        # _handle_model_switch_directly's "mode"/"provider" kinds and
        # server.py's _apply_model_switch), so this is exactly the
        # "when the user switches modes, clear any explicit model
        # override" requirement, enforced at the single real call site
        # rather than something every caller has to remember to do.
        if self._explicit_model_override is not None:
            logger.debug("set_mode() also clearing explicit_model_override")
            self._explicit_model_override = None
        self._save()

    def get_cloud_provider(self) -> str | None:
        logger.debug(f"get_cloud_provider() → {self._cloud_provider_name}")
        return self._cloud_provider_name

    def set_cloud_provider(self, provider_name: str | None) -> None:
        logger.debug(f"set_cloud_provider() → {provider_name}")
        self._cloud_provider_name = provider_name
        self._save()

    def get_explicit_model_override(self) -> str | None:
        logger.debug(f"get_explicit_model_override() → {self._explicit_model_override}")
        return self._explicit_model_override

    def set_explicit_model_override(self, model_id: str | None) -> None:
        """
        "switch to <model>" pins a specific model across every mode —
        it takes precedence over Auto/Cloud mode-based routing (though
        not over a packet naming its own modelId explicitly, e.g. the
        Models page or a suggested-lighter-model click) until the user
        explicitly switches modes again (set_mode() above clears this).
        """
        logger.debug(f"set_explicit_model_override() → {model_id}")
        unified_log("mode_manager", "INFO", "Explicit model override changed", {"model_id": model_id})
        self._explicit_model_override = model_id
        self._save()
