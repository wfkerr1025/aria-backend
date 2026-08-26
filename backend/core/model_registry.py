import json
import os

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "config",
    "models.json"
)

from logger import get_logger

logger = get_logger(__name__)

# Model Discovery Engine — imported here (not the other way around) to
# avoid a circular import: model_discovery.py is deliberately
# self-contained and knows nothing about this registry.
from . import model_discovery


# Load config
logger.debug(f"Loading model registry from {CONFIG_PATH}")
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    _config = json.load(f)
logger.debug("Model registry loaded successfully")

# Extract models + default. _registry is a COPY of _config["models"], not
# a reference to it — _merge_discovered_models() below adds auto-
# discovered entries straight into _registry, and if this were the same
# dict object as _config["models"], those entries would silently ride
# along into models.json the next time any setter (set_default_model(),
# set_fallback_model_id(), ...) does json.dump(_config, ...), even though
# nothing here ever intends to persist a discovered entry.
_registry = dict(_config.get("models", {}))
_default_id = _config.get("default")
_fallback_id = _config.get("fallback")
_emergency_id = _config.get("emergency")

logger.debug(f"Loaded {len(_registry)} models; default={_default_id}")


# ============================================================
# MODEL DISCOVERY — merge auto-discovered *.gguf files into the
# in-memory registry alongside the hand-curated entries above.
#
# Discovered entries are NOT written back to models.json — only
# set_default_model()/set_fallback_model_id()/set_emergency_model_id()
# persist anything, and only the id they were pointed at, not a whole
# synthesized model_cfg. That keeps the hand-curated file exactly as
# the user left it; discovery re-runs (cheaply, from the filesystem)
# every startup instead of being frozen into a config file that could
# drift from what's actually on disk.
# ============================================================
def _merge_discovered_models() -> None:
    try:
        discovered = model_discovery.discover_models()
    except Exception as e:
        # Discovery must never take the whole registry (and therefore
        # the backend) down with it — fall back to whatever was already
        # loaded from models.json and log clearly.
        logger.error(f"_merge_discovered_models() — discovery failed, continuing with curated models only: {e}")
        return

    if not discovered:
        logger.warning("_merge_discovered_models() — no *.gguf files found; only curated models.json entries are available")
        return

    existing_paths = {
        os.path.normcase(os.path.abspath(cfg["path"]))
        for cfg in _registry.values()
        if cfg.get("path")
    }

    added = 0
    for model in discovered:
        resolved_path = os.path.normcase(os.path.abspath(model.path))

        if resolved_path in existing_paths:
            # Already registered under a hand-curated id (e.g. "mistral-
            # 7b-q4km" for mistral-7b-instruct-v0.2.Q4_K_M.gguf) — the
            # curated entry wins rather than duplicating the same file
            # under a second, filename-derived id.
            logger.debug(f"_merge_discovered_models() — {model.filename} already registered, skipping")
            continue

        if model.model_id in _registry:
            logger.debug(f"_merge_discovered_models() — id collision on '{model.model_id}', skipping discovered entry")
            continue

        _registry[model.model_id] = model_discovery.to_registry_entry(model)
        existing_paths.add(resolved_path)
        added += 1
        logger.info(f"_merge_discovered_models() — registered discovered model '{model.model_id}' ({model.filename})")

    logger.info(f"_merge_discovered_models() → {added} new model(s) merged, {len(_registry)} total in registry")


_merge_discovered_models()


def get_model(model_id: str) -> dict:
    logger.debug(f"get_model() → {model_id}")
    return _registry.get(model_id)


def model_violates_mode_separation(model_id: "str | None", mode: str) -> bool:
    """
    True if `model_id` must NOT be used while in `mode` — ARIA-Lite's
    absolute local/cloud mode separation (Section 1: "ARIA must NEVER
    select cloud models [in Local Mode]" / "NEVER select local models
    [in Cloud Mode]"). Single source of truth shared by
    backend.core.provider_router.ProviderRouter.resolve(),
    backend.core.self_knowledge.resolve_active_model_and_provider(), and
    backend.websocket.handlers.py's pre-flight model/safety resolution
    — all three used to independently (and inconsistently) decide this,
    which is exactly how a stale "switch to X" pin could survive a mode
    switch and produce a contradiction like "local provider, in Cloud
    Mode".

    - Automatic Model Routing: never violates — it's the only mode
      allowed to use either registry.
    - Cloud Mode: ANY concrete model_id violates. This registry has no
      individually-registered cloud models (a cloud "model" is chosen
      by provider name only, e.g. via mode_manager.get_cloud_provider()
      — see provider_router.py) — so a model_id naming a real local
      model, or an unrecognized id that would otherwise fall back to a
      local default, are both violations.
    - Local Mode: violates only if model_id resolves to a REAL registry
      entry whose provider isn't "local" — an unrecognized model_id is
      NOT a violation (existing callers already fall back to a real
      local model for it, which is itself compliant).

    Only Local vs. Cloud are checked here — Automatic Model Routing is
    exempt by design (it is the mode allowed to route to either).
    """
    if mode == "automatic" or not model_id:
        return False
    if mode == "cloud":
        return True
    if mode == "local":
        cfg = get_model(model_id)
        return cfg is not None and cfg.get("provider") != "local"
    return False


def get_default_model() -> dict:
    logger.debug("get_default_model() called")
    if _default_id:
        logger.debug(f"Default model → {_default_id}")
        return _registry.get(_default_id)
    logger.debug("No default model set")
    return None


def get_default_model_id() -> str:
    logger.debug("get_default_model_id() called")
    return _default_id


def list_models() -> list:
    logger.debug("list_models() called")
    return list(_registry.values())


# REQUIRED BY lighter_model_engine.py
def get_all_models() -> list:
    logger.debug("get_all_models() called")
    return list(_registry.values())


def get_model_ids() -> list:
    logger.debug("get_model_ids() called")
    return list(_registry.keys())


def set_default_model(model_id: str) -> bool:
    """
    Switch the active/default model and persist it to models.json so
    the choice survives a restart. Returns False for an unknown id.
    """
    global _default_id

    logger.debug(f"set_default_model() called → {model_id}")

    if model_id not in _registry:
        logger.debug(f"set_default_model() → unknown model_id={model_id}")
        return False

    _default_id = model_id
    _config["default"] = model_id

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(_config, f, indent=4)

    logger.debug(f"set_default_model() → default model is now {model_id}")
    return True


# ============================================================
# MODEL ROLES — active / fallback / emergency
#
# "Active" is deliberately the same underlying state as "default"
# above (get_default_model_id/set_default_model) rather than a second,
# parallel concept — two independently-settable "which model is the
# main one" flags is exactly the kind of drift bug this registry has
# already had once (see backend/core/local_model_selector.py's history
# in earlier work). Fallback/emergency are genuinely new state.
# ============================================================
def get_active_model_id() -> str:
    return get_default_model_id()


def set_active_model_id(model_id: str) -> bool:
    return set_default_model(model_id)


def get_fallback_model_id() -> str:
    logger.debug("get_fallback_model_id() called")
    return _fallback_id


def set_fallback_model_id(model_id: str) -> bool:
    global _fallback_id

    logger.debug(f"set_fallback_model_id() called → {model_id}")

    if model_id not in _registry:
        logger.debug(f"set_fallback_model_id() → unknown model_id={model_id}")
        return False

    _fallback_id = model_id
    _config["fallback"] = model_id

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(_config, f, indent=4)

    logger.debug(f"set_fallback_model_id() → fallback model is now {model_id}")
    return True


def get_emergency_model_id() -> str:
    logger.debug("get_emergency_model_id() called")
    return _emergency_id


def set_emergency_model_id(model_id: str) -> bool:
    global _emergency_id

    logger.debug(f"set_emergency_model_id() called → {model_id}")

    if model_id not in _registry:
        logger.debug(f"set_emergency_model_id() → unknown model_id={model_id}")
        return False

    _emergency_id = model_id
    _config["emergency"] = model_id

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(_config, f, indent=4)

    logger.debug(f"set_emergency_model_id() → emergency model is now {model_id}")
    return True


# ============================================================
# DEFAULT ROLE ASSIGNMENT
#
# Runs once at import time, after discovery has merged in. Only sets a
# role if it isn't already configured (models.json's "fallback"/
# "emergency" keys, once a user has changed them via the Models page,
# take precedence over these built-in defaults) and only if the target
# id actually resolved to a real registry entry — never point a role at
# a model that doesn't exist.
# ============================================================
_DEFAULT_ACTIVE_ID = "mistral-7b-q4km"
_DEFAULT_FALLBACK_ID = "phi-3-mini-4k-instruct-q4"
_DEFAULT_EMERGENCY_ID = "qwen2.5-0.5b-instruct-q4_k_m"


def _apply_default_roles() -> None:
    global _default_id, _fallback_id, _emergency_id

    if not _default_id and _DEFAULT_ACTIVE_ID in _registry:
        _default_id = _DEFAULT_ACTIVE_ID
        logger.info(f"_apply_default_roles() — active model defaulted to '{_DEFAULT_ACTIVE_ID}'")

    if not _fallback_id:
        if _DEFAULT_FALLBACK_ID in _registry:
            _fallback_id = _DEFAULT_FALLBACK_ID
            logger.info(f"_apply_default_roles() — fallback model defaulted to '{_DEFAULT_FALLBACK_ID}'")
        else:
            logger.warning(f"_apply_default_roles() — default fallback model '{_DEFAULT_FALLBACK_ID}' not found in registry; fallback unset")

    if not _emergency_id:
        if _DEFAULT_EMERGENCY_ID in _registry:
            _emergency_id = _DEFAULT_EMERGENCY_ID
            logger.info(f"_apply_default_roles() — emergency model defaulted to '{_DEFAULT_EMERGENCY_ID}'")
        else:
            logger.warning(f"_apply_default_roles() — default emergency model '{_DEFAULT_EMERGENCY_ID}' not found in registry; emergency unset")


_apply_default_roles()
