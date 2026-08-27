# Multi-Tier Cache

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

## Classes

### `MultiTierCache`

One instance per namespace (e.g. "provider_metadata", "tool_results")
— namespaces never share a file, so one cache's integrity issue can't
corrupt another's.

- `__init__(self, namespace: 'str', ttl_seconds: 'Optional[float]' = None)`
  Initialize self.  See help(type(self)) for accurate signature.
- `clear(self) -> 'None'`
- `delete(self, key: 'str') -> 'None'`
- `get(self, key: 'str', default: 'Any' = None) -> 'Any'`
- `set(self, key: 'str', value: 'Any', ttl_seconds: 'Optional[float]' = None, persist: 'bool' = True) -> 'None'`
- `warm_up_verify(self) -> 'Dict[str, Any]'`
  Integrity + freshness summary. Uses this cache's own entry shape

## Functions

### `get_cache(namespace: 'str', ttl_seconds: 'Optional[float]' = None) -> 'MultiTierCache'`

One shared MultiTierCache per namespace, so unrelated callers
asking for the same namespace see the same memory tier instead of
each keeping an independent, inconsistent copy.
