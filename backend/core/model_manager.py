# backend/core/model_manager.py

"""
Model installation gate.

ARIA Lite does not yet have a download/install pipeline in this
repo — this module adds the compatibility gate that must run before
one exists: given a model_id, verify the local machine meets that
model's minimum requirements before any install/download is allowed
to proceed.
"""

import os

from .model_registry import get_model, get_all_models, get_default_model_id, set_default_model
from . import model_registry
from .model_path_resolver import ModelPathResolver
from .resource_monitor import get_resource_snapshot
from .compatibility_checker import check_requirements
from .safety_profiles import select_profile
from .performance_estimator import estimate_speed
from . import hardware_snapshot_cache
from . import perf_profiler
from .performance_tiers import estimate_performance_tier
from .model_info import build_model_info
from .lighter_model_engine import suggest_for_install

from backend.logger import log as unified_log

from logger import get_logger

logger = get_logger(__name__)


def install_model(model_id: str) -> dict:
    """
    Gate + (eventually) perform installation of a model.

    Returns a structured result keyed off compat["verdict"] (see
    compatibility_checker.check_requirements()):
      - "block": {"ok": False, "reason": str, "compat": ..., "suggestions": {...}}
        suggestions comes from lighter_model_engine.suggest_for_install() —
        an already-installed lighter local model when one exists, or a
        cloud-provider pointer when the machine's pc_capability_tier is
        too low for that to help (see that function's own docstring).
      - "warn": {"ok": True, "warning": str, "compat": ..., "model_id": ...}
        meets minimum but not recommended — install proceeds, caller
        surfaces the borderline warning inline rather than blocking.
      - "allow": {"ok": True, "compat": ..., "model_id": ...}

    NOTE: this repo has no download/network layer yet. When one is
    added, its logic belongs in _download_model(model_cfg) below —
    it will only ever run after meets_minimum has been confirmed.
    """
    logger.debug(f"install_model() called → model_id={model_id}")

    model_cfg = get_model(model_id)
    if model_cfg is None:
        logger.debug(f"install_model() → unknown model_id={model_id}")
        return {
            "ok": False,
            "reason": f"Unknown model ID: {model_id}",
            "compat": None,
        }

    snapshot = get_resource_snapshot()
    compat = check_requirements(snapshot, model_cfg)

    if compat["verdict"] == "block":
        logger.debug(
            f"install_model() → blocked, model_id={model_id}, "
            f"missing_minimum={compat['missing_minimum']}"
        )
        return {
            "ok": False,
            "reason": "Your system does not meet the minimum requirements.",
            "compat": compat,
            "suggestions": suggest_for_install(snapshot, model_cfg),
        }

    logger.debug(f"install_model() → compatibility check passed for {model_id} (verdict={compat['verdict']})")

    _download_model(model_cfg)

    result = {
        "ok": True,
        "compat": compat,
        "model_id": model_id,
    }
    if compat["verdict"] == "warn":
        result["warning"] = (
            "This model meets the minimum requirements but not the recommended ones — "
            "it may run slowly or feel underpowered on this machine."
        )
    return result


def _download_model(model_cfg: dict) -> None:
    """
    Placeholder for the actual model download/install step.

    No download pipeline exists in this codebase yet — this function
    is the intended integration point once one is added. It is only
    ever reached after install_model() has confirmed meets_minimum.
    """
    logger.debug(
        f"_download_model() → no-op stub, model_id={model_cfg.get('id')} "
        "(download pipeline not yet implemented)"
    )


def is_model_installed(model_cfg: dict) -> bool:
    """
    A model counts as installed if its GGUF file can be resolved on
    disk (either the path already recorded in models.json, or the
    default ~/.aria-lite/models/<filename> location).
    """
    resolver = ModelPathResolver()
    path = resolver.resolve_model_path(model_cfg["id"])
    installed = path is not None
    logger.debug(f"is_model_installed() → model_id={model_cfg.get('id')}, installed={installed}")
    return installed


def uninstall_model(model_id: str) -> dict:
    """
    Remove an installed model's GGUF file from disk.

    Returns:
      - {"ok": False, "reason": str} for an unknown model or a
        filesystem error
      - {"ok": True, "model_id": str, "removed": bool} on success
        (removed=False if the model wasn't installed — a no-op, not
        an error)
    """
    logger.debug(f"uninstall_model() called → model_id={model_id}")

    model_cfg = get_model(model_id)
    if model_cfg is None:
        logger.debug(f"uninstall_model() → unknown model_id={model_id}")
        return {"ok": False, "reason": f"Unknown model ID: {model_id}"}

    resolver = ModelPathResolver()
    path = resolver.resolve_model_path(model_id)

    if path is None:
        logger.debug(f"uninstall_model() → {model_id} is not installed, nothing to remove")
        return {"ok": True, "model_id": model_id, "removed": False}

    try:
        os.remove(path)
        logger.debug(f"uninstall_model() → removed file at {path}")
        return {"ok": True, "model_id": model_id, "removed": True}
    except OSError as e:
        logger.debug(f"uninstall_model() → failed to remove {path}: {e}")
        return {"ok": False, "reason": f"Failed to remove model file: {e}"}


def _set_model_role(role: str, model_id: str, setter) -> dict:
    """
    Shared body for set_active_model/set_fallback_model/
    set_emergency_model — same validation, same model_role_changed
    logging, different underlying registry setter.
    """
    logger.debug(f"set_{role}_model() called → model_id={model_id}")

    model_cfg = get_model(model_id)
    if model_cfg is None:
        logger.debug(f"set_{role}_model() → unknown model_id={model_id}")
        return {"ok": False, "reason": f"Unknown model ID: {model_id}"}

    if not is_model_installed(model_cfg):
        logger.debug(f"set_{role}_model() → {model_id} is not installed")
        return {"ok": False, "reason": "Model is not installed."}

    previous_id = {
        "active": get_default_model_id,
        "fallback": model_registry.get_fallback_model_id,
        "emergency": model_registry.get_emergency_model_id,
    }[role]()

    if not setter(model_id):
        logger.debug(f"set_{role}_model() → registry rejected {model_id}")
        return {"ok": False, "reason": f"Unknown model ID: {model_id}"}

    logger.debug(f"set_{role}_model() → {role} model is now {model_id}")
    unified_log("model_manager", "INFO", f"model_role_changed: {role}", {
        "role": role, "previous_model_id": previous_id, "new_model_id": model_id,
    })
    return {"ok": True, "model_id": model_id, "role": role}


def set_active_model(model_id: str) -> dict:
    """Switch the active/default model. Refuses to activate a model that isn't installed."""
    return _set_model_role("active", model_id, set_default_model)


def set_fallback_model(model_id: str) -> dict:
    """Switch the fallback model (used under RAM pressure — see backend/llm/providers/local_provider.py)."""
    return _set_model_role("fallback", model_id, model_registry.set_fallback_model_id)


def set_emergency_model(model_id: str) -> dict:
    """Switch the emergency model (used under critical RAM pressure)."""
    return _set_model_role("emergency", model_id, model_registry.set_emergency_model_id)


def list_models() -> list:
    """
    Combined view of every registered model for the Models page:
    install state, active/fallback/emergency role, compatibility, and a
    speed estimate — everything the Steam-style switcher/installer UI
    needs in one call instead of one round-trip per model.
    """
    with perf_profiler.timed("model_manager.list_models"):
        return _list_models_inner()


def _list_models_inner() -> list:
    logger.debug("list_models() called")

    snapshot = get_resource_snapshot()
    active_id = get_default_model_id()
    fallback_id = model_registry.get_fallback_model_id()
    emergency_id = model_registry.get_emergency_model_id()

    # TTL-cached (backend.core.hardware_snapshot_cache) rather than a
    # fresh hardware_detector probe on every list_models() call — CPU/
    # GPU/storage identity doesn't change between one Models-page load
    # and the next, so re-running py-cpuinfo/pynvml/a disk benchmark on
    # every single request (this WAS "once per call", but every call
    # still re-probed from scratch) was pure waste, and the storage
    # benchmark specifically was a real, measurable contributor to the
    # "15-second model card stall" on a spinning-disk system.
    cpu = hardware_snapshot_cache.get_cpu()
    ram = hardware_snapshot_cache.get_ram()
    gpu = hardware_snapshot_cache.get_gpu()
    storage = hardware_snapshot_cache.get_storage()

    results = []
    for model_cfg in get_all_models():
        compat = check_requirements(snapshot, model_cfg)
        profile = select_profile(snapshot, model_cfg)
        speed = estimate_speed(snapshot, model_cfg, profile)
        performance_tier = estimate_performance_tier(cpu, ram, gpu, storage, model_cfg)
        model_id = model_cfg.get("id")
        info = build_model_info(model_cfg, snapshot)

        results.append({
            "model_cfg": model_cfg,
            "installed": is_model_installed(model_cfg),
            "is_active": model_id == active_id,
            "is_fallback": model_id == fallback_id,
            "is_emergency": model_id == emergency_id,
            "compat": compat,
            "projected_speed_toksec": speed,
            "performance_tier": performance_tier,
            # Additive — the unified ModelInfo view (params/quant/
            # difficulty/context/requirements/safety_profile/cache
            # fingerprint) alongside the existing fields above, which
            # stay exactly as they were for every consumer that already
            # depends on this shape.
            "model_info": info.to_dict(),
        })

    logger.debug(f"list_models() → {len(results)} models")
    return results
