# backend/core/routing_history.py

"""
Tiny "last time this happened" timestamp store for two routing events
named in the dual-context diagnostics spec:

  - "Last Local Escalation": the most recent time complexity_router.py
    chose its heaviest local tier (DIFFICULT_MODEL_ID) for a prompt.
  - "Last Cloud Escalation": the most recent time Automatic Model
    Routing actually routed a message to a cloud provider rather than
    local (backend.core.provider_router.ProviderRouter.resolve()'s
    automatic branch).

Deliberately just two floats behind a lock, not a ring buffer like
auto_balancer.py's `_history` or complexity_router.py's own routing-
decision log (backend.core.complexity_router.routing_diagnostics_snapshot())
-- nothing here needs more than "how long ago" for a diagnostics
readout, and accumulating a growing history of every escalation ever
would be pure overhead for that.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

_lock = threading.Lock()
_last_local_escalation: Optional[float] = None
_last_cloud_escalation: Optional[float] = None


def record_local_escalation() -> None:
    global _last_local_escalation
    with _lock:
        _last_local_escalation = time.time()


def record_cloud_escalation() -> None:
    global _last_cloud_escalation
    with _lock:
        _last_cloud_escalation = time.time()


def get_last_local_escalation() -> Optional[float]:
    with _lock:
        return _last_local_escalation


def get_last_cloud_escalation() -> Optional[float]:
    with _lock:
        return _last_cloud_escalation
