# backend/tests/model_info_and_init_pipeline_tests.py
#
# Regression tests for:
#   - backend/core/model_info.py: the unified ModelInfo object
#     (params/quant/quant_bytes/quant_difficulty/context/requirements/
#     cache_fingerprint/safety_profile) built from a raw model_cfg.
#   - backend/core/init_pipeline.py: the splash-screen startup pipeline,
#     specifically that run_gguf_metadata_pass() reuses a cached file
#     hash when the file's cheap fingerprint (size+mtime) hasn't
#     changed, instead of re-hashing every .gguf on every single boot —
#     the actual root cause of the reported "2-minute boot spike" (a
#     70B-class model's SHA-256 alone takes tens of seconds; the old
#     code recomputed it on every launch regardless of whether that
#     exact file had already been hashed in a previous run).
#
# Self-contained plain-assert tests. Run directly:
#
#   python backend/tests/model_info_and_init_pipeline_tests.py

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _model_cfg(model_id="test-model", params=7_000_000_000, quant="Q4_K_M",
                max_context=4096, path=None, requirements=None):
    cfg = {
        "id": model_id, "params": params, "quant": quant,
        "defaultFilename": f"{model_id}.{quant}.gguf",
        "maxContext": max_context,
    }
    if path:
        cfg["path"] = path
    if requirements is not None:
        cfg["requirements"] = requirements
    return cfg


# ============================================================
# PART 1 — model_info.py
# ============================================================
def test_build_model_info_populates_every_field_from_a_plain_model_cfg():
    from backend.core.model_info import build_model_info

    info = build_model_info(_model_cfg())

    assert info.model_id == "test-model"
    assert info.params == 7_000_000_000
    assert info.quant == "Q4_K_M"
    assert info.quant_bytes > 0
    assert info.quant_difficulty == 1.0  # Q4_K_M baseline
    assert info.context == 4096
    assert isinstance(info.requirements, dict) and info.requirements
    assert info.safety_profile is None, "no snapshot passed → no safety_profile"


def test_build_model_info_derives_requirements_from_size_table_when_absent():
    from backend.core.model_info import build_model_info

    info = build_model_info(_model_cfg(params=70_000_000_000))
    assert info.requirements["minRamGB"] == 48
    assert info.requirements["recRamGB"] == 64


def test_build_model_info_prefers_hand_authored_requirements_when_present():
    from backend.core.model_info import build_model_info

    curated = {"minRamGB": 1, "recRamGB": 2, "difficulty": "Custom"}
    info = build_model_info(_model_cfg(requirements=curated))
    assert info.requirements == curated


def test_build_model_info_includes_safety_profile_when_snapshot_given():
    from backend.core.model_info import build_model_info
    from backend.core.resource_monitor import ResourceSnapshot

    snapshot = ResourceSnapshot(cpu_usage=10, ram_used_gb=8, ram_total_gb=32,
                                 vram_used_gb=0, vram_total_gb=0, unity_running=False)
    info = build_model_info(_model_cfg(), snapshot)

    assert info.safety_profile is not None
    assert set(info.safety_profile.keys()) == {"name", "n_threads", "n_gpu_layers", "max_ctx"}
    assert info.safety_profile["n_threads"] > 0


def test_build_model_info_computes_a_real_cache_fingerprint_for_an_existing_file():
    from backend.core.model_info import build_model_info

    with tempfile.NamedTemporaryFile(suffix=".gguf", delete=False) as f:
        f.write(b"fake gguf bytes")
        tmp_path = f.name

    try:
        info = build_model_info(_model_cfg(path=tmp_path))
        assert info.cache_fingerprint is not None
        assert ":" in info.cache_fingerprint  # "size:mtime" shape
    finally:
        os.remove(tmp_path)


def test_build_model_info_fingerprint_is_none_without_a_path():
    from backend.core.model_info import build_model_info

    info = build_model_info(_model_cfg())  # no "path" key
    assert info.cache_fingerprint is None


def test_model_info_to_dict_round_trips_every_field():
    from backend.core.model_info import build_model_info

    info = build_model_info(_model_cfg())
    d = info.to_dict()
    assert d["model_id"] == info.model_id
    assert d["params"] == info.params
    assert d["quant_difficulty"] == info.quant_difficulty
    assert d["cache_fingerprint"] == info.cache_fingerprint
    assert set(d.keys()) == {
        "model_id", "params", "quant", "quant_bytes", "quant_difficulty",
        "context", "requirements", "cache_fingerprint", "safety_profile",
    }


# ============================================================
# PART 2 — model_manager.list_models() surfaces model_info additively
# ============================================================
def test_list_models_includes_model_info_without_dropping_existing_fields():
    from backend.core.model_manager import list_models

    results = list_models()
    assert results, "expected at least one registered model"
    for entry in results:
        assert "model_info" in entry
        assert "model_cfg" in entry and "compat" in entry and "projected_speed_toksec" in entry
        assert entry["model_info"]["model_id"] == entry["model_cfg"]["id"]


# ============================================================
# PART 3 — init_pipeline.py: skip re-hashing an unchanged file
# ============================================================
def _isolated_models_dir_and_cache():
    """
    Returns a context manager-like tuple (models_dir, cache_dir) with
    everything monkeypatched to point at temp dirs, restored on exit —
    this must never touch the real ~/.aria-lite/models or
    ~/.aria-lite/cache.
    """
    import backend.core.init_pipeline as ip
    import backend.core.model_size_requirements as msr
    import backend.core.model_discovery as md

    tmp = tempfile.TemporaryDirectory()
    models_dir = Path(tmp.name) / "models"
    cache_dir = Path(tmp.name) / "cache"
    models_dir.mkdir()

    originals = {
        "get_default_model_dir": md.get_default_model_dir,
        "CACHE_DIR": msr.CACHE_DIR,
        "CACHE_FILE": msr.CACHE_FILE,
    }

    md.get_default_model_dir = lambda: models_dir
    msr.CACHE_DIR = cache_dir
    msr.CACHE_FILE = cache_dir / "model_requirements_cache.json"

    ip._scanned_models = []
    ip._metadata_by_id = {}
    ip._requirements_by_id = {}

    def restore():
        md.get_default_model_dir = originals["get_default_model_dir"]
        msr.CACHE_DIR = originals["CACHE_DIR"]
        msr.CACHE_FILE = originals["CACHE_FILE"]
        tmp.cleanup()

    return models_dir, restore


def test_run_gguf_metadata_pass_reuses_hash_for_an_unchanged_file():
    import backend.core.init_pipeline as ip

    models_dir, restore = _isolated_models_dir_and_cache()
    try:
        fake_model = models_dir / "test-7b.Q4_K_M.gguf"
        fake_model.write_bytes(b"x" * 1024)

        hash_calls = {"n": 0}
        real_compute_hash = ip.compute_hash

        def counting_compute_hash(path):
            hash_calls["n"] += 1
            return real_compute_hash(path)

        ip.compute_hash = counting_compute_hash
        try:
            # First run — cold, no cache entry yet — must hash once.
            ip.run_all_steps()
            assert hash_calls["n"] == 1, f"expected exactly 1 hash on first run, got {hash_calls['n']}"

            # Second run, SAME file, unchanged — must NOT re-hash. This
            # is the exact scenario that caused the 2-minute boot spike:
            # the splash pipeline re-running on every launch against
            # files that hadn't changed since the last one.
            ip._scanned_models = []
            ip._metadata_by_id = {}
            ip._requirements_by_id = {}
            ip.run_all_steps()
            assert hash_calls["n"] == 1, f"expected NO additional hash for an unchanged file, got {hash_calls['n']} total"
        finally:
            ip.compute_hash = real_compute_hash
    finally:
        restore()


def test_run_gguf_metadata_pass_rehashes_a_genuinely_changed_file():
    import backend.core.init_pipeline as ip

    models_dir, restore = _isolated_models_dir_and_cache()
    try:
        fake_model = models_dir / "test-7b.Q4_K_M.gguf"
        fake_model.write_bytes(b"x" * 1024)

        hash_calls = {"n": 0}
        real_compute_hash = ip.compute_hash

        def counting_compute_hash(path):
            hash_calls["n"] += 1
            return real_compute_hash(path)

        ip.compute_hash = counting_compute_hash
        try:
            ip.run_all_steps()
            assert hash_calls["n"] == 1

            # Mutate the file's content AND bump its mtime so the cheap
            # fingerprint (size+mtime) actually changes — writing the
            # same byte length back with a touched mtime is enough.
            time.sleep(0.05)
            fake_model.write_bytes(b"y" * 2048)

            ip._scanned_models = []
            ip._metadata_by_id = {}
            ip._requirements_by_id = {}
            ip.run_all_steps()
            assert hash_calls["n"] == 2, f"expected a re-hash after the file actually changed, got {hash_calls['n']} total"
        finally:
            ip.compute_hash = real_compute_hash
    finally:
        restore()


def test_metadata_pass_persists_hash_immediately_for_a_later_process_to_reuse():
    """
    Regression guard for the real remaining bug found after the first
    fix: each of the 4 splash-screen steps (scan/metadata/requirements/
    cache) is its own SEPARATE process (see AriaLauncher/BackendManager.cs),
    so a hash computed in the "metadata" step's process used to be
    invisible to the "requirements"/"cache" steps' own processes — each
    would find no cache entry yet (nothing had been saved to disk until
    the very last step) and re-hash the same file itself, meaning one
    boot still hashed a 70B-class file up to 3 times before this fix.
    Verified here via real subprocess isolation — an in-process
    simulation wouldn't actually exercise the separate-process gap this
    guards against.
    """
    import subprocess
    import sys as _sys

    models_dir, restore = _isolated_models_dir_and_cache()
    try:
        import backend.core.model_discovery as md
        import backend.core.model_size_requirements as msr

        fake_model = models_dir / "test-7b.Q4_K_M.gguf"
        fake_model.write_bytes(b"x" * (2 * 1024 * 1024))  # a couple MB — real hashing, still fast

        msr.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        counter_file = Path(msr.CACHE_DIR) / "hash_call_count.txt"

        env = os.environ.copy()
        env["ARIA_TEST_MODELS_DIR"] = str(models_dir)
        env["ARIA_TEST_CACHE_DIR"] = str(msr.CACHE_DIR)
        env["ARIA_TEST_COUNTER_FILE"] = str(counter_file)

        # Each invocation is a genuinely separate process (matching
        # exactly how AriaLauncher/BackendManager.cs's RunGGUFMetadataPass()
        # /RunRequirementDerivation() each shell out to `py -m
        # backend.core.init_pipeline --step ...`), with compute_hash()
        # patched to append a marker to a shared file every time it's
        # actually called — the one thing an in-process test can't
        # observe, since state doesn't cross a real process boundary.
        # Written as a real temp script (not `python -c`) since a `def`
        # block doesn't survive `-c`'s semicolon-joined one-liner form.
        script_body = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(_ROOT)!r})\n"
            "from pathlib import Path\n"
            "import backend.core.model_discovery as md, backend.core.model_size_requirements as msr\n"
            "md.get_default_model_dir = lambda: Path(os.environ['ARIA_TEST_MODELS_DIR'])\n"
            "msr.CACHE_DIR = Path(os.environ['ARIA_TEST_CACHE_DIR'])\n"
            "msr.CACHE_FILE = msr.CACHE_DIR / 'model_requirements_cache.json'\n"
            "import backend.core.init_pipeline as ip\n"
            "_real_compute_hash = ip.compute_hash\n"
            "def _counting(path):\n"
            "    with open(os.environ['ARIA_TEST_COUNTER_FILE'], 'a') as f:\n"
            "        f.write('x')\n"
            "    return _real_compute_hash(path)\n"
            "ip.compute_hash = _counting\n"
            "if sys.argv[1] == 'metadata':\n"
            "    ip.run_gguf_metadata_pass()\n"
            "else:\n"
            "    ip.run_requirement_derivation()\n"
        )
        script_path = Path(msr.CACHE_DIR) / "_run_step.py"
        script_path.write_text(script_body, encoding="utf-8")

        for step in ("metadata", "requirements"):
            proc = subprocess.run([_sys.executable, str(script_path), step], env=env, capture_output=True, text=True, timeout=30)
            assert proc.returncode == 0, f"step {step} failed: {proc.stderr}"

        hash_call_count = len(counter_file.read_text()) if counter_file.exists() else 0
        assert hash_call_count == 1, (
            f"expected compute_hash() called exactly once total across both separate-process "
            f"steps, got {hash_call_count} — the second process re-hashed instead of reusing "
            f"the first process's persisted result"
        )

        cache = msr.load_cache()
        assert cache["models"], "expected the metadata step's process to have persisted an entry"
    finally:
        restore()


def test_write_requirement_cache_persists_fingerprint_field():
    """
    Regression guard for a real bug found while building this: the
    cache write used to omit "fingerprint" entirely, so should_recompute()
    on the NEXT run would always see None and treat every file as
    changed — silently defeating the whole skip-rehashing mechanism
    above despite it "working" within a single process.
    """
    import backend.core.init_pipeline as ip
    import backend.core.model_size_requirements as msr

    models_dir, restore = _isolated_models_dir_and_cache()
    try:
        fake_model = models_dir / "test-7b.Q4_K_M.gguf"
        fake_model.write_bytes(b"x" * 1024)

        ip.run_all_steps()

        cache = msr.load_cache()
        entry = next(iter(cache["models"].values()))
        assert entry.get("fingerprint") is not None, "cache entry must persist a real fingerprint, not omit the field"
        assert entry.get("hash") is not None
    finally:
        restore()


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
