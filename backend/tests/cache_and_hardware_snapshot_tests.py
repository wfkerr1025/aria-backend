# backend/tests/cache_and_hardware_snapshot_tests.py
#
# Regression tests for the overnight backend-architecture pass:
#   - backend/core/cache_manager.py: schema versioning, migration,
#     integrity verification, warm-up summary.
#   - backend/core/hardware_snapshot_cache.py: TTL caching of CPU/RAM/
#     GPU/storage detection — the fix for hardware_detector being
#     re-probed (including a real disk benchmark) on every single
#     models_list_request, which was a real, measured contributor to
#     the "15-second model card stall".
#   - backend/core/hardware_detector.py's base_clock_ghz parsing bug
#     found while building the above: py-cpuinfo's hz_advertised is
#     [value, exponent] meaning value * 10**exponent Hz, not value /
#     10**exponent — the old code left a live machine's clock speed
#     as literal Hz (e.g. 3187000000.0 "GHz") instead of 3.187 GHz.
#
# Self-contained plain-assert tests, matching backend/tests/
# hardware_and_performance_tests.py. Run directly:
#
#   python backend/tests/cache_and_hardware_snapshot_tests.py

from __future__ import annotations

import os
import sys
import time
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ============================================================
# PART 1 — cache_manager.py
# ============================================================
def test_ensure_versioned_stamps_an_unversioned_cache():
    from backend.core.cache_manager import ensure_versioned, CACHE_SCHEMA_VERSION

    legacy = {"models": {"foo": {"hash": "abc"}}}
    result = ensure_versioned(legacy)

    assert result["version"] == CACHE_SCHEMA_VERSION
    assert result["models"] == {"foo": {"hash": "abc"}}, "existing entries must survive stamping untouched"


def test_ensure_versioned_is_a_noop_for_current_version():
    from backend.core.cache_manager import ensure_versioned, CACHE_SCHEMA_VERSION

    current = {"version": CACHE_SCHEMA_VERSION, "models": {"foo": {}}}
    result = ensure_versioned(current)
    assert result is current, "an already-current cache must be returned as-is, not rebuilt"


def test_ensure_versioned_rebuilds_a_future_version_it_cannot_understand():
    from backend.core.cache_manager import ensure_versioned, CACHE_SCHEMA_VERSION

    from_the_future = {"version": CACHE_SCHEMA_VERSION + 5, "models": {"foo": {"hash": "abc"}}}
    result = ensure_versioned(from_the_future)

    assert result["version"] == CACHE_SCHEMA_VERSION
    assert result["models"] == {}, "a cache from a newer, unknown schema must be rebuilt clean, not misread"


def test_migrate_cache_rebuilds_when_no_migration_step_is_registered():
    from backend.core.cache_manager import migrate_cache, CACHE_SCHEMA_VERSION

    old = {"version": 0, "models": {"foo": {"hash": "abc"}}}
    result = migrate_cache(old, from_version=0, to_version=CACHE_SCHEMA_VERSION)

    assert result["version"] == CACHE_SCHEMA_VERSION
    assert result["models"] == {}


def test_verify_integrity_flags_missing_required_fields():
    from backend.core.cache_manager import verify_integrity

    cache = {
        "version": 1,
        "models": {
            "good-model": {"hash": "h", "fingerprint": "f", "requirements": {}, "timestamp": 1},
            "bad-model": {"hash": "h"},  # missing fingerprint/requirements/timestamp
        },
    }
    result = verify_integrity(cache)

    assert result["ok"] is False
    assert result["entry_count"] == 2
    bad_entry = next(e for e in result["corrupt_entries"] if e["model_id"] == "bad-model")
    assert "fingerprint" in bad_entry["reason"]
    assert "requirements" in bad_entry["reason"]
    assert "timestamp" in bad_entry["reason"]


def test_verify_integrity_passes_a_well_formed_cache():
    from backend.core.cache_manager import verify_integrity

    cache = {
        "version": 1,
        "models": {"m": {"hash": "h", "fingerprint": "f", "requirements": {}, "timestamp": 1}},
    }
    result = verify_integrity(cache)
    assert result["ok"] is True
    assert result["corrupt_entries"] == []


def test_warm_up_summary_reports_entry_count_and_ages():
    from backend.core.cache_manager import warm_up_summary

    now = time.time()
    cache = {
        "version": 1,
        "models": {
            "old": {"hash": "h", "fingerprint": "f", "requirements": {}, "timestamp": now - 3600},
            "new": {"hash": "h", "fingerprint": "f", "requirements": {}, "timestamp": now - 5},
        },
    }
    summary = warm_up_summary(cache)

    assert summary["entry_count"] == 2
    assert summary["integrity_ok"] is True
    assert summary["oldest_entry_age_seconds"] > summary["newest_entry_age_seconds"]
    assert summary["model_ids"] == ["new", "old"]


def test_warm_up_summary_handles_an_empty_cache_without_crashing():
    from backend.core.cache_manager import warm_up_summary

    summary = warm_up_summary({"version": 1, "models": {}})
    assert summary["entry_count"] == 0
    assert summary["oldest_entry_age_seconds"] is None
    assert summary["newest_entry_age_seconds"] is None


# ============================================================
# PART 2 — model_size_requirements.py's load_cache()/save_cache()
# now routing through cache_manager (real file I/O, isolated via a
# monkeypatched CACHE_FILE so this never touches the real
# ~/.aria-lite/cache/model_requirements_cache.json).
# ============================================================
def _with_isolated_cache_file(fn):
    import tempfile
    from pathlib import Path
    from backend.core import model_size_requirements as msr

    original_file = msr.CACHE_FILE
    original_dir = msr.CACHE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        msr.CACHE_DIR = Path(tmp)
        msr.CACHE_FILE = Path(tmp) / "model_requirements_cache.json"
        try:
            return fn(msr)
        finally:
            msr.CACHE_DIR = original_dir
            msr.CACHE_FILE = original_file


def test_load_cache_on_missing_file_returns_versioned_empty_cache():
    def body(msr):
        from backend.core.cache_manager import CACHE_SCHEMA_VERSION
        cache = msr.load_cache()
        assert cache == {"version": CACHE_SCHEMA_VERSION, "models": {}}

    _with_isolated_cache_file(body)


def test_save_then_load_round_trips_and_stamps_version():
    def body(msr):
        from backend.core.cache_manager import CACHE_SCHEMA_VERSION
        msr.save_cache({"models": {"m": {"hash": "h", "fingerprint": "f", "requirements": {}, "timestamp": 1}}})
        reloaded = msr.load_cache()
        assert reloaded["version"] == CACHE_SCHEMA_VERSION
        assert reloaded["models"]["m"]["hash"] == "h"

    _with_isolated_cache_file(body)


def test_load_cache_migrates_a_legacy_unversioned_file_on_disk():
    def body(msr):
        import json
        from backend.core.cache_manager import CACHE_SCHEMA_VERSION

        # Simulate a cache file written before versioning existed.
        with open(msr.CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"models": {"legacy-model": {"hash": "abc123"}}}, f)

        reloaded = msr.load_cache()
        assert reloaded["version"] == CACHE_SCHEMA_VERSION
        assert reloaded["models"]["legacy-model"]["hash"] == "abc123", "legacy entries must survive migration"

    _with_isolated_cache_file(body)


# ============================================================
# PART 3 — hardware_detector.py's base_clock_ghz parsing bug
# ============================================================
def test_detect_cpu_reports_a_sane_clock_speed_not_raw_hz():
    from backend.core.hardware_detector import detect_cpu

    cpu = detect_cpu()
    # Any real desktop/laptop CPU clocks somewhere in this range — the
    # bug this guards against produced values like 3,187,000,000 (raw
    # Hz misread as GHz), which is many orders of magnitude outside it.
    assert 0.5 <= cpu["base_clock_ghz"] <= 10.0, f"base_clock_ghz={cpu['base_clock_ghz']} looks like raw Hz, not GHz"
    # A sane clock speed also means a sane GFLOPS estimate — the same
    # bug inflated this by ~1e9x, which would silently make every
    # system look "Excellent" regardless of anything else.
    assert 1.0 <= cpu["estimated_gflops"] <= 100_000.0, f"estimated_gflops={cpu['estimated_gflops']} implies the clock-speed bug is back"


# ============================================================
# PART 4 — hardware_snapshot_cache.py TTL caching
# ============================================================
def test_get_cpu_is_cached_across_calls_not_reprobed():
    from backend.core import hardware_snapshot_cache as hsc

    hsc.refresh()  # start from a known, empty state
    call_count = {"n": 0}
    original = hsc.hardware_detector.detect_cpu

    def counting_detect_cpu():
        call_count["n"] += 1
        return original()

    hsc.hardware_detector.detect_cpu = counting_detect_cpu
    try:
        hsc._cache.clear()
        first = hsc.get_cpu()
        second = hsc.get_cpu()
        third = hsc.get_cpu()
    finally:
        hsc.hardware_detector.detect_cpu = original

    assert call_count["n"] == 1, f"expected exactly one real probe, got {call_count['n']}"
    assert first == second == third


def test_refresh_forces_a_new_probe():
    from backend.core import hardware_snapshot_cache as hsc

    call_count = {"n": 0}
    original = hsc.hardware_detector.detect_cpu

    def counting_detect_cpu():
        call_count["n"] += 1
        return original()

    hsc.hardware_detector.detect_cpu = counting_detect_cpu
    try:
        hsc._cache.clear()
        hsc.get_cpu()
        hsc.refresh()
        hsc.get_cpu()
    finally:
        hsc.hardware_detector.detect_cpu = original

    assert call_count["n"] == 2, f"expected a probe before AND after refresh(), got {call_count['n']}"


def test_ram_has_a_much_shorter_ttl_than_cpu():
    from backend.core import hardware_snapshot_cache as hsc

    assert hsc._TTL_SECONDS["ram"] < hsc._TTL_SECONDS["cpu"], (
        "RAM availability/pressure genuinely changes between requests; "
        "CPU/GPU/storage identity does not — RAM must not be cached as long"
    )


def test_get_storage_caches_separately_per_path():
    from backend.core import hardware_snapshot_cache as hsc

    hsc._cache.clear()
    call_args = []
    original = hsc.hardware_detector.detect_storage

    def recording_detect_storage(path=None):
        call_args.append(path)
        return original(path)

    hsc.hardware_detector.detect_storage = recording_detect_storage
    try:
        hsc.get_storage("/tmp/a")
        hsc.get_storage("/tmp/a")  # cache hit, no new call
        hsc.get_storage("/tmp/b")  # different path, new call
    finally:
        hsc.hardware_detector.detect_storage = original

    assert call_args == ["/tmp/a", "/tmp/b"], f"expected one real probe per distinct path, got {call_args}"


def test_cache_state_reports_without_forcing_a_probe():
    from backend.core import hardware_snapshot_cache as hsc

    hsc.refresh()
    state = hsc.cache_state()
    assert "cpu" in state
    assert state["cpu"]["expires_in_seconds"] > 0


# ============================================================
# RUNNER
# ============================================================
def _all_tests():
    return [obj for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]


def main() -> int:
    failures = []
    for test in _all_tests():
        name = test.__name__
        try:
            test()
            print(f"PASS  {name}")
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            failures.append(name)
        except Exception as e:
            print(f"ERROR {name}: {e}")
            traceback.print_exc()
            failures.append(name)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1

    print("All tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
