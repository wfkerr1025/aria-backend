# backend/core/local_model_selector.py

import json
import os
from pathlib import Path

from backend.core.model_registry import get_all_models
from backend.logger import log as unified_log

STATE_PATH = Path("config/local_model_state.json")


from logger import get_logger

logger = get_logger(__name__)


def _load_state() -> dict:
    logger.debug("Loading local model state")

    if STATE_PATH.exists():
        try:
            with STATE_PATH.open("r", encoding="utf-8") as f:
                state = json.load(f)
                logger.debug(f"State loaded: {state}")
                return state
        except Exception as e:
            logger.debug(f"Failed to load state: {e}")
            return {}

    logger.debug("State file does not exist, returning empty state")
    return {}


def _save_state(state: dict) -> None:
    logger.debug(f"Saving state: {state}")

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    logger.debug("State saved successfully")


def get_active_local_model() -> str | None:
    state = _load_state()
    active = state.get("active_local_model")

    logger.debug(f"Active local model → {active}")
    return active


def set_active_local_model(model_id: str) -> None:
    logger.debug(f"Setting active local model → {model_id}")

    state = _load_state()
    state["active_local_model"] = model_id
    _save_state(state)

    logger.debug("Active model updated")


def list_local_models() -> list[str]:
    logger.debug("Listing local models")

    # Reads from backend.core.model_registry — the single source of truth
    # for the model config (backend/config/models.json, resolved relative
    # to that module's own __file__). This used to re-read a *different*,
    # cwd-relative "config/models.json" that never existed at the
    # process's working directory, so this always returned [] and
    # ensure_default_local_model() below always came back None.
    models = get_all_models()
    result = [m["id"] for m in models if m.get("provider") == "local" and "id" in m]

    logger.debug(f"Local models found: {result}")
    return result


def ensure_default_local_model() -> str | None:
    logger.debug("Ensuring default local model")

    active = get_active_local_model()
    if active:
        logger.debug(f"Active model already set → {active}")
        return active

    locals_ = list_local_models()
    if not locals_:
        logger.warning("No local models available in the registry")
        unified_log("local_model_selector", "ERROR", "No local models available in registry")
        return None

    active = locals_[0]
    set_active_local_model(active)

    logger.debug(f"Default local model set → {active}")
    unified_log("local_model_selector", "INFO", f"Default local model set: {active}", {"model_id": active})
    return active
