# backend/core/init_pipeline.py

"""
Startup initialization pipeline, driven by the WinForms splash screen
(AriaLauncher/AriaLauncher/SplashForm.cs's RunInitializationPipeline())
so heavy model-catalog work happens up front, with real progress text,
instead of on first use inside the running app.

Each pipeline stage is invoked as its own short-lived `py -m
backend.core.init_pipeline --step <name>` process (see
AriaLauncher/AriaLauncher/BackendManager.cs's RunModelScanAndCache() /
RunGGUFMetadataPass() / RunRequirementDerivation() /
WriteRequirementCache()), one process per splash-screen status line.
Since each invocation is a fresh process, in-memory state from a prior
stage doesn't carry over — main() below re-runs every stage up to and
including the requested one so each step still has what it needs. This
duplicates cheap work (filename-based discovery, not real GGUF binary
parsing) rather than persisting intermediate state to disk, which would
add real complexity for a startup-progress display where the total cost
either way is a fraction of a second.

Reuses the existing model_discovery / model_metadata /
model_size_requirements pipeline rather than re-implementing GGUF
scanning or requirement derivation from scratch — this module's job is
only to run that existing work up front and cache the result.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from . import model_discovery
from .model_metadata import get_model_params, get_quant_bytes, get_quant_difficulty
from .model_size_requirements import (
    get_requirements_for_params, compute_hash, file_fingerprint, should_recompute,
    CACHE_DIR, CACHE_FILE, load_cache, save_cache,
)

from logger import get_logger

logger = get_logger(__name__)


# ============================================================
# In-memory pipeline state — populated by each stage in turn, scoped to
# a single process run (see module docstring on why state isn't
# persisted between the separate per-stage process invocations).
# ============================================================
_scanned_models: list = []
_metadata_by_id: dict = {}
_requirements_by_id: dict = {}


def run_model_scan_and_cache() -> list:
    """Enumerate all .gguf files in ~/.aria-lite/models and hash each one."""
    global _scanned_models
    logger.info("run_model_scan_and_cache() — scanning models directory")

    _scanned_models = model_discovery.discover_models()

    logger.info(f"run_model_scan_and_cache() → {len(_scanned_models)} model(s) found")
    return _scanned_models


def _persist_partial_metadata(model_id: str, file_hash: str | None, fingerprint: str | None) -> None:
    """
    Write just {hash, fingerprint} for one model to the on-disk cache
    right away, merged with whatever's already there (requirements from
    a previous full run, if any) rather than clobbering it. This is
    deliberately a per-model, immediate write (not batched until
    write_requirement_cache() at the very end of the pipeline) — see
    the call site's comment for why a later splash step's separate
    process needs to see this before it runs its own metadata pass.
    """
    cache = load_cache()
    prior = cache.setdefault("models", {}).get(model_id, {})
    cache["models"][model_id] = {
        **prior,
        "hash": file_hash,
        "fingerprint": fingerprint,
        "timestamp": prior.get("timestamp", int(time.time())),
    }
    save_cache(cache)


def run_gguf_metadata_pass() -> dict:
    """
    For each scanned model, extract architecture, quantization,
    parameter count, and context length — plus the derived quant-bytes/
    quant-difficulty multipliers used by the performance estimator.
    """
    global _metadata_by_id

    if not _scanned_models:
        run_model_scan_and_cache()

    # Reused below to skip re-hashing a file this run didn't actually
    # change — see the loop body's comment for why this matters (this
    # was the real cause of the "2-minute boot spike": every launch
    # re-computed a full SHA-256 of every .gguf on disk, including a
    # 70B-class ~40GB file, regardless of whether the previous run
    # already hashed that exact same file).
    existing_cache = load_cache().get("models", {})

    _metadata_by_id = {}
    for model in _scanned_models:
        model_cfg = model_discovery.to_registry_entry(model)
        existing_entry = existing_cache.get(model.model_id)

        # A full SHA-256 is only actually computed when the cheap size+
        # mtime fingerprint shows the file has no matching prior entry
        # (new model, or a genuinely replaced/updated one). An unchanged
        # file reuses last run's already-computed hash — see
        # model_size_requirements.py's compute_hash()/file_fingerprint()/
        # should_recompute() docstrings for the full rationale.
        if existing_entry and not should_recompute(model.path, existing_entry):
            file_hash = existing_entry.get("hash")
            fingerprint = existing_entry.get("fingerprint")
            logger.debug(f"run_gguf_metadata_pass() → {model.model_id} unchanged, reusing cached hash")
        else:
            file_hash = compute_hash(model.path)
            fingerprint = file_fingerprint(model.path)
            logger.debug(f"run_gguf_metadata_pass() → {model.model_id} new/changed, hashing")

            # Persisted immediately, not just held in this process's
            # _metadata_by_id — each of the 4 splash-screen steps
            # (scan/metadata/requirements/cache) is its OWN separate `py
            # -m ...` process (see AriaLauncher/BackendManager.cs), so
            # without this, a hash computed here would be invisible to
            # the NEXT step's process, which would find no cache entry
            # yet and hash the same multi-gigabyte file all over again —
            # the file would still get hashed up to three times in a
            # single boot (once per later step) even with the
            # cross-BOOT caching above, which was exactly the reported
            # "2-minute boot spike" (~3 x ~40s for a 70B-class file).
            _persist_partial_metadata(model.model_id, file_hash, fingerprint)

        _metadata_by_id[model.model_id] = {
            "model_cfg": model_cfg,
            "arch": model.arch,
            "params": get_model_params(model_cfg),
            "quant_bytes": get_quant_bytes(model_cfg),
            "quant_difficulty": get_quant_difficulty(model_cfg),
            "context": model.context_tokens,
            "hash": file_hash,
            "fingerprint": fingerprint,
        }

    logger.info(f"run_gguf_metadata_pass() → metadata extracted for {len(_metadata_by_id)} model(s)")
    return _metadata_by_id


def run_requirement_derivation() -> dict:
    """
    For each model: use its own requirements block if the registry
    entry has one, else derive from
    model_size_requirements.get_requirements_for_params() — the same
    fallback rule compatibility_checker.check_requirements() uses.
    """
    global _requirements_by_id

    if not _metadata_by_id:
        run_gguf_metadata_pass()

    _requirements_by_id = {}
    for model_id, meta in _metadata_by_id.items():
        model_cfg = meta["model_cfg"]
        requirements = model_cfg.get("requirements") or get_requirements_for_params(meta["params"])
        _requirements_by_id[model_id] = requirements

    logger.info(f"run_requirement_derivation() → requirements derived for {len(_requirements_by_id)} model(s)")
    return _requirements_by_id


def write_requirement_cache() -> None:
    """Persist this run's results to ~/.aria-lite/cache/model_requirements_cache.json."""
    if not _requirements_by_id:
        run_requirement_derivation()

    cache = load_cache()
    models = cache.setdefault("models", {})

    for model_id, meta in _metadata_by_id.items():
        models[model_id] = {
            "hash": meta["hash"],
            # Without this, should_recompute() would never find a
            # matching fingerprint on the NEXT run (load_cache() would
            # read back an entry with no "fingerprint" key at all), so
            # run_gguf_metadata_pass()'s skip-rehashing check above would
            # always treat every file as changed and re-hash it anyway —
            # exactly the "2-minute boot spike" this pass exists to fix.
            "fingerprint": meta["fingerprint"],
            "params": meta["params"],
            "quant_bytes": meta["quant_bytes"],
            "quant_difficulty": meta["quant_difficulty"],
            "context": meta["context"],
            "requirements": _requirements_by_id.get(model_id, {}),
            "timestamp": int(time.time()),
        }

    save_cache(cache)
    logger.info(f"write_requirement_cache() → wrote {len(models)} entries to {CACHE_FILE}")

    # Also warm the hardware snapshot cache (CPU/GPU/storage classification)
    # so the very first models_list_request after boot is a cache hit
    # there too, not just for requirements — see hardware_snapshot_cache.py.
    try:
        from . import hardware_snapshot_cache
        hardware_snapshot_cache.refresh()
        logger.info("write_requirement_cache() → hardware snapshot cache warmed")
    except Exception as e:
        logger.debug(f"write_requirement_cache() → hardware snapshot warm-up failed (non-fatal): {e}")


def run_all_steps() -> None:
    run_model_scan_and_cache()
    run_gguf_metadata_pass()
    run_requirement_derivation()
    write_requirement_cache()


_STEP_ORDER = ["scan", "metadata", "requirements", "cache"]
_STEP_FUNCS = {
    "scan": run_model_scan_and_cache,
    "metadata": run_gguf_metadata_pass,
    "requirements": run_requirement_derivation,
    "cache": write_requirement_cache,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="ARIA Lite startup initialization pipeline")
    parser.add_argument("--step", choices=_STEP_ORDER + ["all"], default="all")
    args = parser.parse_args()

    try:
        if args.step == "all":
            run_all_steps()
        else:
            for name in _STEP_ORDER:
                _STEP_FUNCS[name]()
                if name == args.step:
                    break
        return 0
    except Exception as e:
        logger.error(f"init_pipeline main() → step '{args.step}' failed: {e}")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
