# backend/core/lighter_model_engine.py

"""
Lighter Model Suggestion Engine
--------------------------------
Given:
- hardware snapshot
- model metadata
- projected resource usage

Produces:
- ranked list of lighter alternative models
"""

from dataclasses import dataclass
from typing import List, Dict

from .model_registry import get_all_models
from .model_metadata import get_model_params, get_quant_bytes
from .resource_monitor import ResourceSnapshot
from .compatibility_checker import check_requirements
from .safety_profiles import select_profile
from .performance_estimator import estimate_speed
from .pc_capability_tier import get_pc_tier

from logger import get_logger

logger = get_logger(__name__)


@dataclass
class ModelScore:
    model_id: str
    score: float
    reason: str


class LighterModelEngine:

    def __init__(self):
        logger.debug("Initializing LighterModelEngine")
        self.models = get_all_models()
        logger.debug(f"Loaded {len(self.models)} models from registry")

    # -------------------------------------------------------
    # MAIN ENTRY POINT
    # -------------------------------------------------------
    def suggest(self, snapshot: ResourceSnapshot, heavy_model_cfg: dict) -> List[ModelScore]:
        logger.debug(
            f"suggest() called with heavy model '{heavy_model_cfg.get('id')}' "
            f"(params={heavy_model_cfg.get('params')}, quant={heavy_model_cfg.get('quant')})"
        )

        heavy_params = get_model_params(heavy_model_cfg)
        heavy_quant = get_quant_bytes(heavy_model_cfg)

        heavy_profile = select_profile(snapshot, heavy_model_cfg)
        heavy_speed = estimate_speed(snapshot, heavy_model_cfg, heavy_profile)

        logger.debug(
            f"Heavy model params={heavy_params}, quant_bytes={heavy_quant}, "
            f"estimated_speed={heavy_speed:.2f} tok/sec"
        )

        suggestions = []

        for cfg in self.models:
            if cfg["id"] == heavy_model_cfg["id"]:
                continue

            score, reason = self._score_model(
                snapshot,
                cfg,
                heavy_model_cfg,
                heavy_params,
                heavy_quant,
                heavy_speed
            )

            if score > 0:
                logger.debug(f"Model '{cfg['id']}' scored {score} → {reason}")
                suggestions.append(ModelScore(
                    model_id=cfg["id"],
                    score=score,
                    reason=reason
                ))
            else:
                logger.debug(f"Model '{cfg['id']}' rejected (score={score})")

        suggestions.sort(key=lambda s: s.score, reverse=True)

        top = suggestions[:3]
        logger.debug(f"Top {len(top)} suggestions: {[s.model_id for s in top]}")

        return top

    # -------------------------------------------------------
    # MODEL SCORING LOGIC
    # -------------------------------------------------------
    def _score_model(
        self,
        snapshot: ResourceSnapshot,
        cfg: dict,
        heavy_model_cfg: dict,
        heavy_params: int,
        heavy_quant: int,
        heavy_speed: float = 0.0
    ):
        logger.debug(f"Scoring model '{cfg['id']}'")

        # -----------------------------------------------------
        # Reject any model that fails minimum system requirements
        # -----------------------------------------------------
        compat = check_requirements(snapshot, cfg)
        if not compat["meets_minimum"]:
            logger.debug(
                f"Model '{cfg['id']}' rejected → does not meet minimum "
                f"requirements (missing={compat['missing_minimum']})"
            )
            return 0, "Does not meet minimum requirements"

        params = get_model_params(cfg)
        quant = get_quant_bytes(cfg)

        # -----------------------------------------------------
        # NEW RULE: Reject any model heavier than the current one
        # -----------------------------------------------------
        if params >= heavy_params:
            logger.debug(
                f"Model '{cfg['id']}' rejected → heavier model "
                f"({params} >= {heavy_params})"
            )
            return 0, "Heavier model"

        # -----------------------------------------------------
        # Existing scoring logic
        # -----------------------------------------------------
        param_ratio = heavy_params / params if params > 0 else 1.0
        quant_ratio = heavy_quant / quant if quant > 0 else 1.0

        vram_fit = snapshot.vram_total_gb >= cfg.get("minVram", 0)
        ram_fit = snapshot.ram_total_gb >= cfg.get("minRam", 0)

        score = 0.0
        reason_parts = []

        if params < heavy_params:
            score += param_ratio * 2.0
            reason_parts.append(f"{params/1e9:.1f}B params (lighter)")

        if quant < heavy_quant:
            score += quant_ratio * 1.5
            reason_parts.append(f"{quant} bytes/param (lighter quant)")

        if vram_fit:
            score += 1.0
            reason_parts.append("Fits VRAM")

        if ram_fit:
            score += 1.0
            reason_parts.append("Fits RAM")

        default_layers = cfg.get("defaultGpuLayers", 24)
        heavy_layers = heavy_model_cfg.get("defaultGpuLayers", 24)

        if default_layers < heavy_layers:
            score += 1.0
            reason_parts.append(f"{default_layers} GPU layers (lighter)")

        # -----------------------------------------------------
        # Estimated speed: reward candidates that are actually
        # faster on this machine, not just smaller on paper
        # -----------------------------------------------------
        profile = select_profile(snapshot, cfg)
        speed = estimate_speed(snapshot, cfg, profile)

        if heavy_speed > 0 and speed > heavy_speed:
            speed_ratio = speed / heavy_speed
            score += min(speed_ratio, 3.0) * 1.0
            reason_parts.append(f"{speed:.1f} tok/sec (faster than {heavy_speed:.1f})")

        if score <= 0:
            logger.debug(f"Model '{cfg['id']}' → no improvements, score=0")
            return 0, ""

        reason = ", ".join(reason_parts)
        logger.debug(f"Model '{cfg['id']}' final score={score} → {reason}")

        return score, reason


# -------------------------------------------------------------------
# Install-time counterpart to the runtime safety_warning flow above --
# used by the model-installation safety system (backend.core.
# model_manager.install_model()) when a candidate model fails
# meets_minimum, to suggest either an already-installed lighter local
# model (reusing THIS class's own suggest()/scoring, not a second,
# possibly-diverging implementation) or, when nothing installed is
# actually lighter and the machine's pc_capability_tier is too low to
# expect that to change (Tier 0-2 -- below "+7B" per the tier table),
# pointing at a cloud provider instead.
# -------------------------------------------------------------------
CLOUD_SUGGESTION_MAX_TIER = 2


def suggest_for_install(snapshot: ResourceSnapshot, model_cfg: dict) -> Dict:
    engine = LighterModelEngine()
    lighter_local = engine.suggest(snapshot, model_cfg)
    pc_tier = get_pc_tier()
    suggest_cloud = not lighter_local and pc_tier["tier"] <= CLOUD_SUGGESTION_MAX_TIER

    logger.debug(
        f"suggest_for_install() → model_id={model_cfg.get('id')}, "
        f"lighter_local={[s.model_id for s in lighter_local]}, suggest_cloud={suggest_cloud}"
    )

    return {
        "lighter_local": [
            {"model_id": s.model_id, "score": s.score, "reason": s.reason} for s in lighter_local
        ],
        "suggest_cloud": suggest_cloud,
        "pc_tier": pc_tier,
    }
