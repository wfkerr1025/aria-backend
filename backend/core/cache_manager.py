# backend/core/cache_manager.py

"""
Versioning, integrity, and migration for the on-disk requirements cache
(~/.aria-lite/cache/model_requirements_cache.json — see
backend.core.model_size_requirements and backend.core.init_pipeline).

backend.core.model_size_requirements.load_cache()/save_cache() are the
only code that actually reads/writes the file; this module wraps that
raw dict with:
  - a schema version stamp, so a future incompatible cache shape change
    doesn't get silently misread as valid data;
  - migration from an unversioned (pre-this-feature) cache file rather
    than discarding it;
  - integrity verification (every entry has the fields every reader
    actually expects);
  - a warm-up-verification summary for /v1/diagnostics/cache.

Kept separate from model_size_requirements.py on purpose: that module
owns the *content* (what a model's requirements are); this one owns the
*container* (is the file itself trustworthy, current, and complete).
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from logger import get_logger

logger = get_logger(__name__)

# Bump this when the cache entry SHAPE changes in a way old readers
# can't safely interpret (e.g. a renamed/removed field an existing
# reader indexes into directly). Purely additive fields don't need a
# bump — see migrate_cache()'s docstring.
CACHE_SCHEMA_VERSION = 1

# Fields every consumer (compatibility_checker, model_loader,
# init_pipeline, the REST/IPC surfaces) expects a model entry to have.
# Used by verify_integrity() to flag a corrupt/partial entry rather than
# let a KeyError surface deep in some other module later.
REQUIRED_ENTRY_FIELDS = ("hash", "fingerprint", "requirements", "timestamp")


def ensure_versioned(cache: Dict[str, Any]) -> Dict[str, Any]:
    """
    Stamp a cache dict with the current schema version, migrating it
    first if it's carrying an older (or absent — pre-this-feature)
    version. Idempotent: calling this on an already-current cache is a
    no-op read, not a rewrite.
    """
    version = cache.get("version")

    if version == CACHE_SCHEMA_VERSION:
        return cache

    if version is None:
        # Pre-existing cache written before versioning existed (or a
        # brand-new empty {"models": {}} from load_cache()'s own
        # missing-file fallback) — nothing shape-incompatible to
        # migrate, just stamp it. Real model entries are untouched.
        logger.debug("ensure_versioned() → unversioned cache, stamping as v%d", CACHE_SCHEMA_VERSION)
        cache = dict(cache)
        cache["version"] = CACHE_SCHEMA_VERSION
        cache.setdefault("models", {})
        return cache

    if version > CACHE_SCHEMA_VERSION:
        # A newer binary wrote this cache than the one currently
        # running (e.g. after a rollback) — safest move is to rebuild
        # rather than guess at an unknown-future shape.
        logger.warning("ensure_versioned() → cache version %s is newer than this build supports (%d); rebuilding", version, CACHE_SCHEMA_VERSION)
        return {"version": CACHE_SCHEMA_VERSION, "models": {}}

    return migrate_cache(cache, version, CACHE_SCHEMA_VERSION)


def migrate_cache(cache: Dict[str, Any], from_version: int, to_version: int) -> Dict[str, Any]:
    """
    Step-wise migration, one version at a time, so a future v1->v3 jump
    is "apply the v1->v2 step, then the v2->v3 step" rather than one
    combinatorial special case per (from, to) pair.

    There are no real migrations registered yet (CACHE_SCHEMA_VERSION
    has only ever been 1) — this exists so the next actual schema change
    has a tested place to add one instead of improvising cache-shape
    surgery inline wherever it's noticed.
    """
    migrated = dict(cache)
    version = from_version

    while version < to_version:
        step = _MIGRATIONS.get(version)
        if step is None:
            logger.warning("migrate_cache() → no migration registered for v%d, rebuilding instead", version)
            return {"version": to_version, "models": {}}
        migrated = step(migrated)
        version += 1

    migrated["version"] = to_version
    return migrated


# Registry of (version -> migration function producing version+1).
# Empty today; see migrate_cache()'s docstring.
_MIGRATIONS: Dict[int, Any] = {}


def verify_integrity(cache: Dict[str, Any]) -> Dict[str, Any]:
    """
    Structural sanity check — not a hash/fingerprint re-verification
    (that's should_recompute()'s job, and re-hashing every entry here
    would reintroduce the exact per-request cost this whole cache exists
    to avoid). Reports which entries are missing fields a reader would
    otherwise KeyError on.
    """
    models = cache.get("models", {})
    if not isinstance(models, dict):
        return {"ok": False, "reason": "'models' is not an object", "entry_count": 0, "corrupt_entries": []}

    corrupt = []
    for model_id, entry in models.items():
        if not isinstance(entry, dict):
            corrupt.append({"model_id": model_id, "reason": "entry is not an object"})
            continue
        missing = [f for f in REQUIRED_ENTRY_FIELDS if f not in entry]
        if missing:
            corrupt.append({"model_id": model_id, "reason": f"missing fields: {missing}"})

    return {
        "ok": len(corrupt) == 0,
        "entry_count": len(models),
        "corrupt_entries": corrupt,
        "schema_version": cache.get("version"),
    }


def warm_up_summary(cache: Dict[str, Any]) -> Dict[str, Any]:
    """
    /v1/diagnostics/cache payload — is the cache warm, how many models
    does it cover, and how stale is the oldest entry. "Stale" here just
    means "old", not "wrong" — should_recompute() is what actually
    decides whether a given entry still matches its file.
    """
    integrity = verify_integrity(cache)
    models: Dict[str, Any] = cache.get("models", {}) if isinstance(cache.get("models"), dict) else {}

    now = time.time()
    ages = [now - entry.get("timestamp", now) for entry in models.values() if isinstance(entry, dict)]

    return {
        "schema_version": cache.get("version"),
        "entry_count": len(models),
        "integrity_ok": integrity["ok"],
        "corrupt_entries": integrity["corrupt_entries"],
        "oldest_entry_age_seconds": round(max(ages), 1) if ages else None,
        "newest_entry_age_seconds": round(min(ages), 1) if ages else None,
        "model_ids": sorted(models.keys()),
    }
