# Cache Manager

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

## Functions

### `ensure_versioned(cache: 'Dict[str, Any]') -> 'Dict[str, Any]'`

Stamp a cache dict with the current schema version, migrating it
first if it's carrying an older (or absent — pre-this-feature)
version. Idempotent: calling this on an already-current cache is a
no-op read, not a rewrite.

### `migrate_cache(cache: 'Dict[str, Any]', from_version: 'int', to_version: 'int') -> 'Dict[str, Any]'`

Step-wise migration, one version at a time, so a future v1->v3 jump
is "apply the v1->v2 step, then the v2->v3 step" rather than one
combinatorial special case per (from, to) pair.

There are no real migrations registered yet (CACHE_SCHEMA_VERSION
has only ever been 1) — this exists so the next actual schema change
has a tested place to add one instead of improvising cache-shape
surgery inline wherever it's noticed.

### `verify_integrity(cache: 'Dict[str, Any]') -> 'Dict[str, Any]'`

Structural sanity check — not a hash/fingerprint re-verification
(that's should_recompute()'s job, and re-hashing every entry here
would reintroduce the exact per-request cost this whole cache exists
to avoid). Reports which entries are missing fields a reader would
otherwise KeyError on.

### `warm_up_summary(cache: 'Dict[str, Any]') -> 'Dict[str, Any]'`

/v1/diagnostics/cache payload — is the cache warm, how many models
does it cover, and how stale is the oldest entry. "Stale" here just
means "old", not "wrong" — should_recompute() is what actually
decides whether a given entry still matches its file.
