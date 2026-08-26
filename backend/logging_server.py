# backend/logging_server.py
#
# Unified cross-runtime logging server for ARIA-Lite.
#
# Every runtime in the stack (this Python backend, the C# AriaLauncher,
# the Electron main process, and the webui running inside it) POSTs log
# entries here over HTTP. This process owns exactly one log file for the
# lifetime of one ARIA run, so everything ends up interleaved in a single
# timeline instead of scattered across four separate log files.
#
# Must be started FIRST, before every other process in the stack, so the
# one-file-per-run guarantee holds. Prints LOG_SERVER_READY to stdout once
# listening, mirroring backend/ws_server.py's WS_SERVER_READY convention
# so AriaLauncher can watch for it the same way.

from __future__ import annotations

import gzip
import glob
import os
import shutil
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Resolve paths from this file's location, not from cwd — the launcher may
# invoke this script with any working directory.
ROOT = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(ROOT)  # backend/ -> repo root
LOGS_DIR = os.path.join(ROOT, "logs")
MODULES_DIR = os.path.join(LOGS_DIR, "modules")

# Uncompressed, immediately-previous runs kept as plain .log (fast to
# tail/grep without decompressing); older than that gets gzipped instead
# of deleted outright, and only past MAX_COMPRESSED_LOG_FILES of those
# are actually removed — a real "rotation" that preserves history
# instead of just capping it.
MAX_LOG_FILES = 10
MAX_COMPRESSED_LOG_FILES = 40
LOG_PORT = 5001

# Severities routed to a dedicated ARIA_Run_*_errors.log alongside the
# full-detail main log, so an operator can tail just the lines that
# actually need attention instead of the whole interleaved run.
_ERROR_CHANNEL_LEVELS = {"ERROR", "CRITICAL"}

_write_lock = threading.Lock()
_log_file_path: Optional[str] = None
_error_log_file_path: Optional[str] = None


def format_run_timestamp(dt: datetime) -> str:
    """
    ARIA's custom run-timestamp format: <Month>-<Day>-<Year> -<Hour>.<Minute>
    e.g. 8-21-2026 -01.31

    Spec called for a ':' between hour and minute (8-21-2026 -01:31), but
    ':' is illegal in Windows filenames — it's reserved for drive letters
    and NTFS alternate data streams, so "...-01:31.log" silently splits
    into an empty file named "...-01" with the real content hidden in an
    invisible ADS named "31.log". Using '.' instead keeps the HH:MM look
    without hitting that. Confirmed with the user (2026-08-21).

    Month/day are NOT zero-padded; hour/minute ARE (matches "01" and "31").
    Built manually rather than via %-m/%-d strftime flags — those are a
    glibc-only extension and raise on Windows.
    """
    return f"{dt.month}-{dt.day}-{dt.year} -{dt.hour:02d}.{dt.minute:02d}"


def _compress_log_file(path: str) -> None:
    gz_path = path + ".gz"
    try:
        with open(path, "rb") as src, gzip.open(gz_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.remove(path)
    except OSError:
        # Compression is a space-saving nicety, not a correctness
        # requirement — leaving the plain .log behind on failure is a
        # safe fallback, not data loss.
        pass


def _rotate_log_files() -> None:
    """
    Keep the MAX_LOG_FILES most-recently-modified *.log files (both the
    main run log and its *_errors.log sibling) uncompressed for fast
    tailing; anything older than that gets gzip-compressed instead of
    deleted outright. Compressed (.log.gz) files beyond
    MAX_COMPRESSED_LOG_FILES are then actually removed — real rotation
    (bounded disk usage, preserved history) rather than the previous
    "keep N, delete everything else" behavior.
    """
    plain_pattern = os.path.join(LOGS_DIR, "ARIA_Run_*.log")
    plain_files = sorted(glob.glob(plain_pattern), key=os.path.getmtime, reverse=True)
    for stale_path in plain_files[MAX_LOG_FILES:]:
        _compress_log_file(stale_path)

    gz_pattern = os.path.join(LOGS_DIR, "ARIA_Run_*.log.gz")
    gz_files = sorted(glob.glob(gz_pattern), key=os.path.getmtime, reverse=True)
    for stale_gz_path in gz_files[MAX_COMPRESSED_LOG_FILES:]:
        try:
            os.remove(stale_gz_path)
        except OSError:
            pass


def _init_log_file() -> tuple[str, str]:
    os.makedirs(LOGS_DIR, exist_ok=True)
    os.makedirs(MODULES_DIR, exist_ok=True)

    timestamp = format_run_timestamp(datetime.now())
    filename = f"ARIA_Run_{timestamp}.log"
    path = os.path.join(LOGS_DIR, filename)
    error_filename = f"ARIA_Run_{timestamp}_errors.log"
    error_path = os.path.join(LOGS_DIR, error_filename)

    with open(path, "a", encoding="utf-8") as f:
        f.write(f"=== ARIA-Lite unified run log started {datetime.now().isoformat()} ===\n")
    with open(error_path, "a", encoding="utf-8") as f:
        f.write(f"=== ARIA-Lite error/critical channel started {datetime.now().isoformat()} ===\n")

    _rotate_log_files()
    return path, error_path


_log_file_path, _error_log_file_path = _init_log_file()


class LogEntry(BaseModel):
    subsystem: str
    level: str = "INFO"
    message: str
    context: Optional[Dict[str, Any]] = None


def _module_log_path(subsystem: str) -> str:
    # Subsystem names in practice include things like "Backend STDOUT"
    # and "LogServer WARN" (see AriaLauncher/BackendManager.cs) — slugify
    # so every variant of one subsystem's noisy sub-labels still lands in
    # one importable filename instead of exploding into one file per
    # exact string.
    slug = "".join(c if c.isalnum() else "_" for c in subsystem.lower()).strip("_") or "unknown"
    slug = "_".join(part for part in slug.split("_") if part)  # collapse repeats
    return os.path.join(MODULES_DIR, f"{slug}.log")


def _write_line(entry: LogEntry) -> None:
    level = entry.level.upper()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    line = f"[{ts}] [{level:<7}] [{entry.subsystem}] {entry.message}"
    if entry.context:
        line += f" | context={entry.context}"
    line += "\n"

    with _write_lock:
        with open(_log_file_path, "a", encoding="utf-8") as f:
            f.write(line)

        # Severity channel — ERROR/CRITICAL only, so an operator can tail
        # just what needs attention instead of the whole interleaved run.
        if level in _ERROR_CHANNEL_LEVELS:
            with open(_error_log_file_path, "a", encoding="utf-8") as f:
                f.write(line)

        # Per-module stream — every entry, split by subsystem, so
        # e.g. logs/modules/streaming_engine.log can be tailed in
        # isolation without grepping the interleaved main log.
        try:
            with open(_module_log_path(entry.subsystem), "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("LOG_SERVER_READY", flush=True)
    yield


app = FastAPI(title="ARIA Lite Unified Logging Server", version="1.0.0", lifespan=lifespan)

# Every client is a local process (Python, C#, Electron main, and the
# browser/webui renderer) — CORS is opened wide since this never leaves
# 127.0.0.1 and browser fetch() would otherwise be blocked cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/log")
async def receive_log(entry: LogEntry) -> Dict[str, Any]:
    _write_line(entry)
    return {"status": "ok"}


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "log_file": _log_file_path,
        "error_log_file": _error_log_file_path,
        "modules_dir": MODULES_DIR,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=LOG_PORT, log_level="warning")
