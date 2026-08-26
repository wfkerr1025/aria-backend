# backend/core/compatibility_checker.py

"""
Steam-style system requirement compatibility checker.

Compares a live ResourceSnapshot (RAM/VRAM) plus real, cross-platform
CPU/storage detection (backend.core.hardware_detector) against a
model's "requirements" block from models.json — falling back to
backend.core.model_size_requirements's canonical per-size-tier table
when a model has no requirements block of its own — and reports
pass/fail per category plus overall meets_minimum / meets_recommended /
exceeds_recommended verdicts.
"""

import os

from .resource_monitor import ResourceSnapshot
from . import hardware_detector
from . import hardware_snapshot_cache
from . import perf_profiler
from .model_metadata import get_model_params
from .model_size_requirements import get_requirements_for_params, load_cache, should_recompute

from logger import get_logger

logger = get_logger(__name__)


def _requirements_for(model_cfg: dict) -> dict:
    """
    Prefer a model's own hand-authored requirements block (so curated
    notes/difficulty/speed targets for specific models keep working);
    otherwise reuse whatever the startup pipeline
    (backend/core/init_pipeline.py) already cached for this model, and
    only fall back to computing from the canonical size-tiered table
    when neither is available — instead of silently defaulting to "runs
    on anything" (minCpuCores=1/minRamGB=0).
    """
    requirements = model_cfg.get("requirements")
    if requirements:
        return requirements

    model_id = model_cfg.get("id")
    file_path = model_cfg.get("path")
    if model_id and file_path:
        cache = load_cache()
        cached_entry = cache.get("models", {}).get(model_id)
        if not should_recompute(file_path, cached_entry):
            logger.debug(f"_requirements_for() → cache hit for model_id={model_id}")
            return cached_entry.get("requirements", {})

    params = get_model_params(model_cfg)
    logger.debug(f"_requirements_for() → no requirements block/cache hit, deriving from params={params}")
    return get_requirements_for_params(params, model_id=model_id, file_path=file_path)


def _storage_meets(detected_type: str, detected_tier: str, required_type: str | None, required_tier: str | None) -> bool:
    """
    A storage requirement is satisfied when the detected type matches
    (e.g. "NVMe") and, if a tier is also required (e.g. "NVMe Ultra"),
    the detected tier is at least that good.
    """
    if required_type and detected_type != required_type:
        return False
    if required_tier:
        detected_ordinal = {"Low": 1, "Medium": 2, "High": 3, "Ultra": 4}.get(detected_tier, 0)
        required_ordinal = {"Low": 1, "Medium": 2, "High": 3, "Ultra": 4}.get(required_tier, 0)
        if detected_ordinal < required_ordinal:
            return False
    return True


def get_effective_requirements(model_cfg: dict) -> dict:
    """
    Public wrapper around _requirements_for() — the same explicit-block >
    warmed-cache > size-table fallback resolution check_requirements()
    uses internally, exposed for callers (backend.core.model_info) that
    need "what requirements apply to this model" without also running a
    live hardware comparison against them.
    """
    return _requirements_for(model_cfg)


# -----------------------------------------------------------
# PUBLIC API
# -----------------------------------------------------------

def check_requirements(snapshot: ResourceSnapshot, model_cfg: dict, storage_path: str | None = None) -> dict:
    """
    Compare a hardware snapshot against a model's requirements block.

    Returns a dict shaped like:
    {
        "model_id": str,
        "difficulty": str,
        "checks": {
            "ram":          {"value": float, "min": float, "rec": float, "pass_min": bool, "pass_rec": bool},
            "cpu_cores":    {"value": int,   "min": int,   "rec": int,   "pass_min": bool, "pass_rec": bool},
            "cpu_features": {"value": [str], "min": [str], "rec": [str], "pass_min": bool, "pass_rec": bool},
            "vram":         {"value": float, "min": float, "rec": float, "pass_min": bool, "pass_rec": bool},
            # present only when the requirements block specifies them:
            "avx2_tier":    {"value": str, "min": str|None, "rec": str|None, "pass_min": bool, "pass_rec": bool},
            "storage":      {"value": str, "min": str|None, "rec": str|None, "pass_min": bool, "pass_rec": bool},
        },
        "cpu_threads": int,  # informational only, not gated
        "meets_minimum": bool,
        "meets_recommended": bool,
        "exceeds_recommended": bool,
        "missing_minimum": [str],
        "missing_recommended": [str],
        "verdict": "block" | "warn" | "allow",
    }

    `verdict` is a single derived field for callers (model install
    gating, the Models page, compatibility-summary.js) that previously
    each re-derived their own three-way branch from meets_minimum/
    meets_recommended independently — "block" (fails minimum), "warn"
    (meets minimum but not recommended — install allowed, borderline),
    "allow" (meets recommended). It adds no new classification logic;
    it just names the same two booleans' three possible combinations
    once, in one place.
    """
    model_id = model_cfg.get("id", "unknown")
    logger.debug(f"check_requirements() called for model_id={model_id}")

    with perf_profiler.timed("compatibility_checker.check_requirements"):
        return _check_requirements_inner(snapshot, model_cfg, storage_path, model_id)


def _check_requirements_inner(snapshot: ResourceSnapshot, model_cfg: dict, storage_path: str | None, model_id: str) -> dict:
    requirements = _requirements_for(model_cfg)

    ram_gb = snapshot.ram_total_gb
    vram_gb = snapshot.vram_total_gb

    # Sourced from the TTL-cached hardware_snapshot_cache rather than
    # hardware_detector directly — this used to re-run py-cpuinfo
    # detection on every single check_requirements() call (once per
    # model, per models_list_request), which was a real, measurable
    # contributor to the "15-second model card stall".
    cpu = hardware_snapshot_cache.get_cpu()
    physical_cores = cpu["physical_cores"]
    logical_threads = cpu["logical_threads"]
    cpu_features = set()
    if cpu["avx2"]:
        cpu_features.add("AVX2")
    if cpu["avx512"]:
        cpu_features.add("AVX512")
    detected_avx2_tier = cpu["avx2_tier"]

    min_ram = float(requirements.get("minRamGB", 0))
    rec_ram = float(requirements.get("recRamGB", min_ram))

    min_cores = int(requirements.get("minCpuCores", 1))
    rec_cores = int(requirements.get("recCpuCores", min_cores))

    min_features = set(requirements.get("minCpuFeatures", []))
    rec_features = set(requirements.get("recCpuFeatures", min_features))

    min_vram = float(requirements.get("minVramGB", 0))
    rec_vram = float(requirements.get("recVramGB", min_vram))

    ram_pass_min = ram_gb >= min_ram
    ram_pass_rec = ram_gb >= rec_ram
    ram_exceeds = ram_gb > rec_ram

    cores_pass_min = physical_cores >= min_cores
    cores_pass_rec = physical_cores >= rec_cores
    cores_exceeds = physical_cores > rec_cores

    features_pass_min = min_features.issubset(cpu_features)
    features_pass_rec = rec_features.issubset(cpu_features)

    # A model with no VRAM requirement (min_vram == 0) is CPU-viable,
    # so the VRAM check always passes minimum in that case regardless
    # of whether a GPU is present.
    vram_pass_min = (min_vram <= 0) or (vram_gb >= min_vram)
    vram_pass_rec = (rec_vram <= 0) or (vram_gb >= rec_vram)
    vram_exceeds = (rec_vram > 0) and (vram_gb > rec_vram)

    checks = {
        "ram": {
            "value": round(ram_gb, 2),
            "min": min_ram,
            "rec": rec_ram,
            "pass_min": ram_pass_min,
            "pass_rec": ram_pass_rec,
        },
        "cpu_cores": {
            "value": physical_cores,
            "min": min_cores,
            "rec": rec_cores,
            "pass_min": cores_pass_min,
            "pass_rec": cores_pass_rec,
        },
        "cpu_features": {
            "value": sorted(cpu_features),
            "min": sorted(min_features),
            "rec": sorted(rec_features),
            "pass_min": features_pass_min,
            "pass_rec": features_pass_rec,
        },
        "vram": {
            "value": round(vram_gb, 2),
            "min": min_vram,
            "rec": rec_vram,
            "pass_min": vram_pass_min,
            "pass_rec": vram_pass_rec,
        },
    }

    # A numeric "exceeds recommended" only makes sense for the scalar
    # checks above — feature sets and (below) storage type/tier are
    # match/no-match, not a magnitude that can be "exceeded".
    exceeds_flags = [ram_exceeds, cores_exceeds]
    if rec_vram > 0:
        exceeds_flags.append(vram_exceeds)

    # Only gate on AVX2 tier / storage when the requirements block
    # actually specifies them — older/hand-authored entries that predate
    # these fields must keep passing exactly as before.
    min_avx2_tier = requirements.get("minAvx2Tier")
    rec_avx2_tier = requirements.get("recAvx2Tier")
    if min_avx2_tier or rec_avx2_tier:
        tier_order = hardware_detector.AVX2_TIER_ORDER
        detected_ordinal = tier_order.get(detected_avx2_tier, 0)
        min_ordinal = tier_order.get(min_avx2_tier, 0)
        rec_ordinal = tier_order.get(rec_avx2_tier, min_ordinal)

        checks["avx2_tier"] = {
            "value": detected_avx2_tier,
            "min": min_avx2_tier,
            "rec": rec_avx2_tier,
            "pass_min": detected_ordinal >= min_ordinal,
            "pass_rec": detected_ordinal >= rec_ordinal,
        }
        if rec_ordinal > 0:
            exceeds_flags.append(detected_ordinal > rec_ordinal)

    min_storage_type = requirements.get("minStorageType")
    rec_storage_type = requirements.get("recStorageType")
    if min_storage_type or rec_storage_type:
        storage = hardware_snapshot_cache.get_storage(storage_path)
        rec_type, _, rec_tier = (rec_storage_type or "").partition(" ")
        checks["storage"] = {
            "value": f"{storage['type']} ({storage['tier']})",
            "min": min_storage_type,
            "rec": rec_storage_type,
            "pass_min": _storage_meets(storage["type"], storage["tier"], min_storage_type, None),
            "pass_rec": _storage_meets(storage["type"], storage["tier"], rec_type or None, rec_tier or None),
        }

    missing_minimum = [name for name, c in checks.items() if not c["pass_min"]]
    missing_recommended = [name for name, c in checks.items() if not c["pass_rec"]]

    meets_minimum = len(missing_minimum) == 0
    meets_recommended = len(missing_recommended) == 0
    exceeds_recommended = meets_recommended and bool(exceeds_flags) and all(exceeds_flags)

    if not meets_minimum:
        verdict = "block"
    elif not meets_recommended:
        verdict = "warn"
    else:
        verdict = "allow"

    logger.debug(
        f"check_requirements() → model_id={model_id}, "
        f"meets_minimum={meets_minimum}, meets_recommended={meets_recommended}, "
        f"exceeds_recommended={exceeds_recommended}, missing_minimum={missing_minimum}, verdict={verdict}"
    )

    return {
        "model_id": model_id,
        "difficulty": requirements.get("difficulty", "Unknown"),
        "checks": checks,
        "cpu_threads": logical_threads,
        "meets_minimum": meets_minimum,
        "meets_recommended": meets_recommended,
        "exceeds_recommended": exceeds_recommended,
        "missing_minimum": missing_minimum,
        "missing_recommended": missing_recommended,
        "verdict": verdict,
    }
