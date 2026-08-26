# backend/core/model_info.py

"""
Unified ModelInfo — one object describing everything the rest of the
backend needs to know about a single catalog entry, instead of every
caller (model_manager.list_models(), the REST /v1/models* routes, the
IPC model_requirements_request/model_performance_request handlers,
init_pipeline.py) separately re-deriving params/quant/difficulty/
context/requirements from a raw model_cfg dict.

This does NOT change any external wire format — model_manager.list_models()
still returns exactly the dict shape it always has (webui and the existing
test suite depend on that shape). ModelInfo is the internal representation
those call sites build FROM and can serialize back to that same shape; see
ModelInfo.to_dict().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .model_metadata import get_model_params, get_quant_bytes, get_quant_difficulty
from .compatibility_checker import get_effective_requirements
from .model_size_requirements import file_fingerprint
from .safety_profiles import select_profile, PerformanceProfile
from .resource_monitor import ResourceSnapshot

from logger import get_logger

logger = get_logger(__name__)


@dataclass
class ModelInfo:
    model_id: str
    model_cfg: Dict[str, Any]
    params: int
    quant: str
    quant_bytes: float
    quant_difficulty: float
    context: int
    requirements: Dict[str, Any]
    cache_fingerprint: Optional[str]
    safety_profile: Optional[Dict[str, Any]] = field(default=None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "params": self.params,
            "quant": self.quant,
            "quant_bytes": self.quant_bytes,
            "quant_difficulty": self.quant_difficulty,
            "context": self.context,
            "requirements": self.requirements,
            "cache_fingerprint": self.cache_fingerprint,
            "safety_profile": self.safety_profile,
        }


def _profile_to_dict(profile: PerformanceProfile) -> Dict[str, Any]:
    return {
        "name": profile.name,
        "n_threads": profile.n_threads,
        "n_gpu_layers": profile.n_gpu_layers,
        "max_ctx": profile.max_ctx,
    }


def build_model_info(model_cfg: Dict[str, Any], snapshot: Optional[ResourceSnapshot] = None) -> ModelInfo:
    """
    Assemble a ModelInfo from a raw model_cfg (as returned by
    model_registry.get_model()/get_all_models()).

    snapshot is optional: pass a live ResourceSnapshot to also compute an
    adaptive safety_profile (thread count/GPU layers/context for THIS
    machine right now); omit it (e.g. from init_pipeline.py's startup
    pass, which has no "current" snapshot concept) and safety_profile is
    left None — every other field is a static property of the model
    file itself and doesn't need one.
    """
    model_id = model_cfg.get("id", "unknown")

    params = get_model_params(model_cfg)
    quant_bytes = get_quant_bytes(model_cfg)
    quant_difficulty = get_quant_difficulty(model_cfg)
    requirements = get_effective_requirements(model_cfg)
    file_path = model_cfg.get("path")

    safety_profile = None
    if snapshot is not None:
        try:
            safety_profile = _profile_to_dict(select_profile(snapshot, model_cfg))
        except Exception as e:
            logger.debug(f"build_model_info() → select_profile failed for {model_id}: {e}")

    return ModelInfo(
        model_id=model_id,
        model_cfg=model_cfg,
        params=params,
        quant=(model_cfg.get("quant") or "").upper(),
        quant_bytes=quant_bytes,
        quant_difficulty=quant_difficulty,
        context=int(model_cfg.get("maxContext") or requirements.get("minContext") or 0),
        requirements=requirements,
        cache_fingerprint=file_fingerprint(file_path) if file_path else None,
        safety_profile=safety_profile,
    )
