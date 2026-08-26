# backend/core/multi_tier_cache.py

"""
Advanced caching layer — Phase 2, item 5.

Expands Phase 1's backend.core.cache_manager (which owns versioning/
migration/integrity for the requirements-cache JSON file specifically)
into a general-purpose multi-tier cache any Phase 2 subsystem (providers,
tools, plugins, the execution pipeline) can use for its own keyed data:

  - Memory tier: a plain dict, checked first — near-zero cost.
  - Disk tier: one JSON file per namespace under
    ~/.aria-lite/cache/tiers/<namespace>.json, checked on a memory miss
    and populated back into memory (read-through).
  - Persistent tier: conceptually the same disk file (JSON survives
    process restarts, unlike the memory tier) — versioned/migrated via
    cache_manager.ensure_versioned() exactly like the Phase 1
    requirements cache, so this new layer inherits the same integrity/
    warm-up-verification machinery instead of re-deriving it.

This does NOT replace backend.core.model_size_requirements's own
load_cache()/save_cache() (that cache has its own, already-tested,
model-id-keyed shape) — it's a NEW, general-purpose facility for
anything else that wants tiered caching going forward.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import cache_manager

from logger import get_logger

logger = get_logger(__name__)

TIERS_DIR = Path.home() / ".aria-lite" / "cache" / "tiers"


class MultiTierCache:
    """One instance per namespace (e.g. "provider_metadata", "tool_results")
    — namespaces never share a file, so one cache's integrity issue can't
    corrupt another's."""

    def __init__(self, namespace: str, ttl_seconds: Optional[float] = None):
        self.namespace = namespace
        self.ttl_seconds = ttl_seconds
        self._memory: Dict[str, Dict[str, Any]] = {}  # key -> {"value", "expires_at", "stored_at"}
        self._lock = threading.Lock()
        self._disk_path = TIERS_DIR / f"{namespace}.json"

    # -----------------------------------------------------------
    # Disk tier (also the "persistent" tier — same file, survives restarts)
    # -----------------------------------------------------------
    def _load_disk(self) -> Dict[str, Any]:
        if not self._disk_path.exists():
            return {"version": cache_manager.CACHE_SCHEMA_VERSION, "entries": {}}
        try:
            with open(self._disk_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or "entries" not in data:
                return {"version": cache_manager.CACHE_SCHEMA_VERSION, "entries": {}}
            return cache_manager.ensure_versioned(data)
        except (OSError, json.JSONDecodeError) as e:
            logger.debug(f"MultiTierCache[{self.namespace}] → disk read failed: {e}")
            return {"version": cache_manager.CACHE_SCHEMA_VERSION, "entries": {}}

    def _save_disk(self, data: Dict[str, Any]) -> None:
        try:
            TIERS_DIR.mkdir(parents=True, exist_ok=True)
            with open(self._disk_path, "w", encoding="utf-8") as f:
                json.dump(cache_manager.ensure_versioned(data), f, indent=2)
        except OSError as e:
            logger.debug(f"MultiTierCache[{self.namespace}] → disk write failed: {e}")

    # -----------------------------------------------------------
    # Public API
    # -----------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        now = time.time()
        with self._lock:
            entry = self._memory.get(key)
            if entry is not None and (entry["expires_at"] is None or entry["expires_at"] > now):
                return entry["value"]

        # Memory miss (or expired) — fall through to disk, then promote
        # back into memory so the NEXT lookup for this key is a memory hit.
        disk = self._load_disk()
        disk_entry = disk.get("entries", {}).get(key)
        if disk_entry is None:
            return default
        if disk_entry.get("expires_at") is not None and disk_entry["expires_at"] <= now:
            return default

        with self._lock:
            self._memory[key] = {"value": disk_entry["value"], "expires_at": disk_entry.get("expires_at")}
        return disk_entry["value"]

    def set(self, key: str, value: Any, ttl_seconds: Optional[float] = None, persist: bool = True) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self.ttl_seconds
        expires_at = (time.time() + ttl) if ttl is not None else None

        with self._lock:
            self._memory[key] = {"value": value, "expires_at": expires_at}

        if persist:
            disk = self._load_disk()
            disk.setdefault("entries", {})[key] = {"value": value, "expires_at": expires_at, "stored_at": time.time()}
            self._save_disk(disk)

    def delete(self, key: str) -> None:
        with self._lock:
            self._memory.pop(key, None)
        disk = self._load_disk()
        if key in disk.get("entries", {}):
            del disk["entries"][key]
            self._save_disk(disk)

    def clear(self) -> None:
        with self._lock:
            self._memory.clear()
        self._save_disk({"version": cache_manager.CACHE_SCHEMA_VERSION, "entries": {}})

    def warm_up_verify(self) -> Dict[str, Any]:
        """
        Integrity + freshness summary. Uses this cache's own entry shape
        (value/expires_at/stored_at) rather than
        cache_manager.verify_integrity()'s REQUIRED_ENTRY_FIELDS — those
        are specific to the Phase 1 requirements-cache entry shape
        (hash/fingerprint/requirements/timestamp) and would flag every
        entry here as "corrupt" for missing fields that don't apply to
        this cache at all.
        """
        disk = self._load_disk()
        entries = disk.get("entries", {}) if isinstance(disk.get("entries"), dict) else {}
        corrupt = [k for k, v in entries.items() if not isinstance(v, dict) or "value" not in v]
        return {
            "namespace": self.namespace,
            "schema_version": disk.get("version"),
            "memory_entry_count": len(self._memory),
            "disk_entry_count": len(entries),
            "integrity_ok": len(corrupt) == 0,
            "corrupt_keys": corrupt,
            "disk_path": str(self._disk_path),
        }


_instances: Dict[str, MultiTierCache] = {}
_instances_lock = threading.Lock()


def get_cache(namespace: str, ttl_seconds: Optional[float] = None) -> MultiTierCache:
    """One shared MultiTierCache per namespace, so unrelated callers
    asking for the same namespace see the same memory tier instead of
    each keeping an independent, inconsistent copy."""
    with _instances_lock:
        if namespace not in _instances:
            _instances[namespace] = MultiTierCache(namespace, ttl_seconds=ttl_seconds)
        return _instances[namespace]
