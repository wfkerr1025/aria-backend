# backend/tests/logging_server_tests.py
#
# Regression tests for backend/logging_server.py's rotation/compression,
# severity-channel, and per-module-stream additions.
#
# backend.logging_server has real import-time side effects (it creates
# this run's ARIA_Run_*.log / *_errors.log under the real logs/
# directory, same as it always has — every other test file that
# transitively imports backend.logger already lives with this). Tests
# that exercise rotation specifically monkeypatch LOGS_DIR/MODULES_DIR
# to a temp directory first so they never touch real run logs.
#
# Self-contained plain-assert tests. Run directly:
#
#   python backend/tests/logging_server_tests.py

from __future__ import annotations

import gzip
import os
import sys
import tempfile
import time
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _client():
    from fastapi.testclient import TestClient
    import backend.logging_server as ls
    return TestClient(ls.app), ls


# ============================================================
# PART 1 — /log routes to the main file, the error channel, and a
# per-module stream.
# ============================================================
def test_error_level_entry_appears_in_both_main_and_error_channel():
    client, ls = _client()
    marker = f"error-marker-{time.time()}"

    resp = client.post("/log", json={"subsystem": "test_subsystem", "level": "ERROR", "message": marker})
    assert resp.status_code == 200

    with open(ls._log_file_path, encoding="utf-8") as f:
        assert marker in f.read()
    with open(ls._error_log_file_path, encoding="utf-8") as f:
        assert marker in f.read()


def test_info_level_entry_does_not_appear_in_error_channel():
    client, ls = _client()
    marker = f"info-marker-{time.time()}"

    client.post("/log", json={"subsystem": "test_subsystem", "level": "INFO", "message": marker})

    with open(ls._log_file_path, encoding="utf-8") as f:
        assert marker in f.read()
    with open(ls._error_log_file_path, encoding="utf-8") as f:
        assert marker not in f.read()


def test_entry_appears_in_its_own_per_module_stream():
    client, ls = _client()
    marker = f"module-marker-{time.time()}"

    client.post("/log", json={"subsystem": "my_special_module", "level": "INFO", "message": marker})

    module_path = ls._module_log_path("my_special_module")
    assert os.path.exists(module_path)
    with open(module_path, encoding="utf-8") as f:
        assert marker in f.read()


def test_module_log_path_slugifies_noisy_subsystem_labels_consistently():
    from backend.logging_server import _module_log_path

    # "Backend STDOUT" and "Backend ERROR" (real labels from
    # AriaLauncher/BackendManager.cs) must not explode into a separate
    # file per exact string — slugification collapses non-alnum runs.
    assert _module_log_path("Backend STDOUT") == _module_log_path("backend_stdout")
    assert "backend" in os.path.basename(_module_log_path("Backend STDOUT"))


def test_health_reports_all_three_log_paths():
    client, ls = _client()
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["log_file"] == ls._log_file_path
    assert body["error_log_file"] == ls._error_log_file_path
    assert body["modules_dir"] == ls.MODULES_DIR


# ============================================================
# PART 2 — rotation + compression (isolated to a temp directory)
# ============================================================
def test_rotate_log_files_compresses_beyond_max_log_files_instead_of_deleting():
    import backend.logging_server as ls

    original_logs_dir = ls.LOGS_DIR
    original_max = ls.MAX_LOG_FILES
    with tempfile.TemporaryDirectory() as tmp:
        ls.LOGS_DIR = tmp
        ls.MAX_LOG_FILES = 2
        try:
            paths = []
            for i in range(4):
                p = os.path.join(tmp, f"ARIA_Run_test-{i}.log")
                with open(p, "w", encoding="utf-8") as f:
                    f.write(f"run {i}\n")
                os.utime(p, (time.time() + i, time.time() + i))  # distinct, increasing mtimes
                paths.append(p)

            ls._rotate_log_files()

            remaining_plain = sorted(os.path.basename(p) for p in __import__("glob").glob(os.path.join(tmp, "ARIA_Run_*.log")))
            remaining_gz = sorted(os.path.basename(p) for p in __import__("glob").glob(os.path.join(tmp, "ARIA_Run_*.log.gz")))

            assert len(remaining_plain) == 2, f"expected 2 plain .log files kept, got {remaining_plain}"
            assert len(remaining_gz) == 2, f"expected the 2 oldest compressed, got {remaining_gz}"

            # The two newest (highest index) must be the ones left plain.
            assert "ARIA_Run_test-3.log" in remaining_plain
            assert "ARIA_Run_test-2.log" in remaining_plain
            # And the compressed files must actually be valid gzip with the original content.
            oldest_gz = os.path.join(tmp, "ARIA_Run_test-0.log.gz")
            assert os.path.exists(oldest_gz)
            with gzip.open(oldest_gz, "rt", encoding="utf-8") as f:
                assert f.read() == "run 0\n"
        finally:
            ls.LOGS_DIR = original_logs_dir
            ls.MAX_LOG_FILES = original_max


def test_rotate_log_files_caps_compressed_file_count_too():
    import backend.logging_server as ls

    original_logs_dir = ls.LOGS_DIR
    original_max_gz = ls.MAX_COMPRESSED_LOG_FILES
    with tempfile.TemporaryDirectory() as tmp:
        ls.LOGS_DIR = tmp
        ls.MAX_COMPRESSED_LOG_FILES = 1
        try:
            for i in range(3):
                p = os.path.join(tmp, f"ARIA_Run_old-{i}.log.gz")
                with gzip.open(p, "wt", encoding="utf-8") as f:
                    f.write("x")
                os.utime(p, (time.time() + i, time.time() + i))

            ls._rotate_log_files()

            remaining = __import__("glob").glob(os.path.join(tmp, "ARIA_Run_*.log.gz"))
            assert len(remaining) == 1, f"expected only MAX_COMPRESSED_LOG_FILES=1 kept, got {remaining}"
            assert os.path.basename(remaining[0]) == "ARIA_Run_old-2.log.gz", "the newest compressed file must survive"
        finally:
            ls.LOGS_DIR = original_logs_dir
            ls.MAX_COMPRESSED_LOG_FILES = original_max_gz


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
