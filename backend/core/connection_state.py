# backend/core/connection_state.py

"""
Batch 3 — lightweight, process-wide tracker of WebSocket connection
health, backing diagnostics_weather_result / diagnostics_tools_result's
"is the backend connected" field ("from heartbeat", per the spec).

Updated exclusively by backend.websocket.handlers.WebSocketHandler:
connection_opened()/connection_closed() bracket its handle() loop, and
record_heartbeat_sent() fires from the SAME heartbeat loop Batch 1 added
(WebSocketHandler._heartbeat_loop()) right after a heartbeat actually
goes out over the wire — never a guess, always a real send that
succeeded.

Multiple connections can be open at once (a user could have more than
one window/tab); this deliberately reports "connected" as long as AT
LEAST ONE is alive, rather than trying to average/reconcile per-
connection health into one number.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

_active_connections = 0
_last_heartbeat_at: Optional[float] = None


def connection_opened() -> None:
    global _active_connections
    _active_connections += 1


def connection_closed() -> None:
    global _active_connections
    _active_connections = max(0, _active_connections - 1)


def record_heartbeat_sent() -> None:
    global _last_heartbeat_at
    _last_heartbeat_at = time.time()


@dataclass(frozen=True)
class ConnectionStatus:
    active_connections: int
    last_heartbeat_at: Optional[float]
    connected: bool


def get_status() -> ConnectionStatus:
    return ConnectionStatus(
        active_connections=_active_connections,
        last_heartbeat_at=_last_heartbeat_at,
        connected=_active_connections > 0,
    )
