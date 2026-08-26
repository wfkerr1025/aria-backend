# backend/logger.py
#
# Client for the unified cross-runtime logging server
# (backend/logging_server.py, http://127.0.0.1:5001/log).
#
# Distinct from the repo-root `logger.py` (imported as `from logger import
# get_logger`) — that one drives this process's own local Python `logging`
# output. This module instead ships every call to the ONE shared run-log
# file that every runtime in the stack (Python, the C# launcher, Electron,
# the webui) writes into, interleaved in one timeline.
#
# log() must never be able to break the caller: it queues the entry and
# returns immediately, and the background sender swallows every failure
# (log server not started yet, down, network hiccup, ...).

from __future__ import annotations

import json
import queue
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

LOG_SERVER_URL = "http://127.0.0.1:5001/log"
_REQUEST_TIMEOUT_SECONDS = 1.5
_MAX_QUEUED_ENTRIES = 5000

_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=_MAX_QUEUED_ENTRIES)
_worker_lock = threading.Lock()
_worker_started = False


def _worker() -> None:
    while True:
        payload = _queue.get()
        try:
            data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                LOG_SERVER_URL,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS).close()
        except Exception:
            # Logging must never break runtime — the log server may not be
            # up yet, may have exited, or the network hiccuped. Drop it.
            pass
        finally:
            _queue.task_done()


def _ensure_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_worker, daemon=True, name="aria-unified-logger").start()
            _worker_started = True


def log(subsystem: str, level: str, message: str, context: Optional[Dict[str, Any]] = None) -> None:
    """
    Fire-and-forget log call to the unified run-log server. Returns
    immediately (queues onto a background thread) so a slow or unreachable
    log server can never stall the caller — safe to call from hot paths
    like per-token streaming callbacks.
    """
    try:
        _ensure_worker()
        _queue.put_nowait({
            "subsystem": subsystem,
            "level": level,
            "message": message,
            "context": context or {},
        })
    except Exception:
        pass
