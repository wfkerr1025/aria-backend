# backend/core/model_size_requirements.py

"""
Canonical, size-tiered model requirement thresholds.

Replaces the previous situation where every model's requirements block
had to be hand-authored in backend/config/models.json with no shared
source of truth — meaning a newly-added model with no requirements
block at all silently got minCpuCores=1/minRamGB=0 (i.e. "runs on
anything"), and existing entries could drift out of sync with what's
actually a reasonable modern threshold for that parameter count.

get_requirements_for_params() is the single source of truth for what
"minimum"/"recommended" means at each size class. backend/config/
models.json entries can still hand-author a "requirements" block to
override this (e.g. to add model-specific notes/difficulty text or a
tuned speed target) — compatibility_checker.check_requirements() only
falls back to this table when a model_cfg has no requirements block of
its own.

Recommended values that the spec expresses as a range (e.g. "32-48 GB
RAM") are stored as the range's LOWER bound in the scalar rec* field
(the threshold check.check_requirements() actually gates on — meeting
the low end of the documented range is enough to count as "meets
recommended"), with the full range preserved in a matching *Range field
purely for UI display.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from . import cache_manager

from logger import get_logger

logger = get_logger(__name__)

# Shared with backend/core/init_pipeline.py, which populates this same
# file from the splash-screen startup pipeline so the first real
# requirements lookup after launch is a cache hit instead of a cold
# compute.
CACHE_DIR = Path.home() / ".aria-lite" / "cache"
CACHE_FILE = CACHE_DIR / "model_requirements_cache.json"


def load_cache() -> dict:
    """
    Read the on-disk requirements cache. Never raises — a missing or
    corrupt cache file just means "nothing cached yet". Routed through
    cache_manager.ensure_versioned() so a pre-versioning cache file gets
    stamped/migrated rather than either breaking or being silently
    treated as already-current.
    """
    if not CACHE_FILE.exists():
        return {"version": cache_manager.CACHE_SCHEMA_VERSION, "models": {}}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "models" not in data:
            return {"version": cache_manager.CACHE_SCHEMA_VERSION, "models": {}}
        return cache_manager.ensure_versioned(data)
    except (OSError, json.JSONDecodeError) as e:
        logger.debug(f"load_cache() → failed to read {CACHE_FILE}: {e}")
        return {"version": cache_manager.CACHE_SCHEMA_VERSION, "models": {}}


def save_cache(cache: dict) -> None:
    cache = cache_manager.ensure_versioned(cache)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except OSError as e:
        logger.debug(f"save_cache() → failed to write {CACHE_FILE}: {e}")


def compute_hash(file_path: str) -> str | None:
    """
    SHA-256 of a model file's contents — the identity value actually
    stored in the cache (see write_requirement_cache() /
    backend/core/init_pipeline.py). GGUF files run into the tens of
    gigabytes (a 70B model is a real example already on disk in this
    project), so this is deliberately used only when writing a fresh
    cache entry, never as a per-lookup staleness check — see
    should_recompute()'s docstring for why re-reading the whole file on
    every hot-path lookup (compatibility_checker, model_loader) would be
    a serious performance regression, not a correctness improvement.
    """
    if not file_path or not os.path.exists(file_path):
        return None
    try:
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError as e:
        logger.debug(f"compute_hash() → failed to hash {file_path}: {e}")
        return None


def file_fingerprint(file_path: str) -> str | None:
    """
    Cheap (stat-only, no file content read) stand-in for "has this file
    changed" — file size + mtime. Used by should_recompute() so a
    multi-gigabyte GGUF isn't re-hashed in full on every single
    compatibility check or model load; a real SHA-256 is still computed
    and stored once whenever a cache entry is actually (re)written.
    """
    if not file_path:
        return None
    try:
        stat = os.stat(file_path)
        return f"{stat.st_size}:{int(stat.st_mtime)}"
    except OSError as e:
        logger.debug(f"file_fingerprint() → failed to stat {file_path}: {e}")
        return None


def should_recompute(model_path: str, cached_entry: dict | None) -> bool:
    """
    True if there's no usable cached entry, or the file looks like it
    might have changed since it was cached (different size/mtime).
    Deliberately does NOT re-hash the file's full contents on every call
    — see compute_hash()'s docstring — a size/mtime fingerprint is
    sufficient to catch a replaced/updated model file without paying an
    O(file size) cost on every lookup.
    """
    if not cached_entry:
        return True
    current_fingerprint = file_fingerprint(model_path)
    if current_fingerprint is None:
        return True
    return current_fingerprint != cached_entry.get("fingerprint")


def _compute_requirements_for_params(params: int) -> dict:
    """The original size-bucket lookup, unwrapped from caching — the
    part get_requirements_for_params() below actually falls back to."""
    billions = params / 1_000_000_000
    for _label, max_billions, requirements in _SIZE_TIERS:
        if billions <= max_billions:
            return dict(requirements)
    return dict(_SIZE_TIERS[-1][2])


def _tier(size_tier: str, requirements: dict) -> dict:
    result = dict(requirements)
    result["sizeTier"] = size_tier
    return result


# (label, inclusive upper bound in billions of params, requirements)
#
# 20B and 140B are explicit buckets (added alongside the universal
# Tier 0-6 PC-capability system in backend.core.pc_capability_tier) —
# previously a 20B model silently landed in the 30B bucket's
# requirements (30B's upper bound was 40) and 120B/140B fell into the
# 405B catch-all. Both cases "worked" (every model still got SOME
# requirements block), just not the specific named sizes the model-
# install-safety spec calls out; these two new entries interpolate
# between their neighbors rather than inventing unrelated numbers.
_SIZE_TIERS = [
    ("7B", 8, _tier("7B", {
        "minRamGB": 8, "recRamGB": 16,
        "minCpuCores": 4, "recCpuCores": 8,
        "minCpuFeatures": ["AVX2"], "recCpuFeatures": ["AVX2"],
        "minVramGB": 0, "recVramGB": 6,
        "minStorageType": None, "recStorageType": None,
        "difficulty": "Medium",
    })),
    ("12B", 16, _tier("12B", {
        "minRamGB": 12, "recRamGB": 24,
        "minCpuCores": 4, "recCpuCores": 8,
        "minCpuFeatures": ["AVX2"], "recCpuFeatures": ["AVX2"],
        "recAvx2Tier": "High",
        "minVramGB": 0, "recVramGB": 0,
        "minStorageType": None, "recStorageType": None,
        "difficulty": "Heavy",
    })),
    ("20B", 24, _tier("20B", {
        "minRamGB": 18, "recRamGB": 28, "recRamGBRange": [28, 36],
        "minCpuCores": 6, "recCpuCores": 10,
        "minCpuFeatures": ["AVX2"], "recCpuFeatures": ["AVX2"],
        "recAvx2Tier": "High",
        "minVramGB": 0, "recVramGB": 0,
        "minStorageType": None, "recStorageType": None,
        "difficulty": "Heavy",
    })),
    ("30B", 40, _tier("30B", {
        "minRamGB": 24, "recRamGB": 32, "recRamGBRange": [32, 48],
        "minCpuCores": 8, "recCpuCores": 12,
        "minCpuFeatures": ["AVX2"], "recCpuFeatures": ["AVX2"],
        "recAvx2Tier": "High",
        "minVramGB": 0, "recVramGB": 0,
        "minStorageType": None, "recStorageType": None,
        "difficulty": "Very Heavy",
    })),
    ("70B", 90, _tier("70B", {
        "minRamGB": 48, "recRamGB": 64,
        "minCpuCores": 12, "recCpuCores": 16, "recCpuCoresRange": [16, 24],
        "minCpuFeatures": ["AVX2"], "recCpuFeatures": ["AVX2"],
        "recAvx2Tier": "High",
        "minVramGB": 0, "recVramGB": 12,
        "minStorageType": None, "recStorageType": None,
        "difficulty": "Extreme",
    })),
    ("140B", 150, _tier("140B", {
        "minRamGB": 80, "recRamGB": 112, "recRamGBRange": [112, 144],
        "minCpuCores": 16, "recCpuCores": 20, "recCpuCoresRange": [20, 28],
        "minCpuFeatures": ["AVX2"], "recCpuFeatures": ["AVX2"],
        "recAvx2Tier": "High",
        "minVramGB": 0, "recVramGB": 24,
        "minStorageType": "NVMe", "recStorageType": "NVMe Ultra",
        "difficulty": "Extreme",
    })),
    ("405B", float("inf"), _tier("405B", {
        "minRamGB": 64, "recRamGB": 96, "recRamGBRange": [96, 128],
        "minCpuCores": 12, "recCpuCores": 16,
        "minCpuFeatures": ["AVX2"], "recCpuFeatures": ["AVX2"],
        "recAvx2Tier": "High",
        "minVramGB": 0, "recVramGB": 16,
        "minStorageType": "NVMe", "recStorageType": "NVMe Ultra",
        "difficulty": "Extreme",
    })),
]


def get_requirements_for_params(params: int, model_id: str | None = None, file_path: str | None = None) -> dict:
    """
    Return the canonical requirements block for a model with this many
    parameters.

    model_id/file_path are optional — every existing call site that
    only has a param count (e.g. compatibility_checker.py) keeps working
    unchanged and uncached. When both are supplied (the startup
    initialization pipeline, backend/core/init_pipeline.py, is the only
    caller that has a real file path up front), this checks the
    requirements cache first and reuses a hit rather than recomputing —
    the computation itself is cheap, but this is also where the
    written-by-the-splash-pipeline cache gets exercised/kept warm.
    """
    if model_id and file_path:
        cache = load_cache()
        cached_entry = cache.get("models", {}).get(model_id)
        if not should_recompute(file_path, cached_entry):
            logger.debug(f"get_requirements_for_params() → cache hit for {model_id}")
            return dict(cached_entry.get("requirements", {}))

        result = _compute_requirements_for_params(params)
        cache.setdefault("models", {})[model_id] = {
            "hash": compute_hash(file_path),
            "fingerprint": file_fingerprint(file_path),
            "requirements": result,
            "timestamp": int(time.time()),
        }
        save_cache(cache)
        return result

    return _compute_requirements_for_params(params)


def get_requirements_for_size_tier(size_tier: str) -> dict | None:
    """Look up a size tier's requirements directly by label (e.g. "70B")."""
    for label, _max_billions, requirements in _SIZE_TIERS:
        if label == size_tier:
            return dict(requirements)
    return None
