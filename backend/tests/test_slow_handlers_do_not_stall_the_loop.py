# backend/tests/test_slow_handlers_do_not_stall_the_loop.py
#
# A commit stopped the heartbeat and the client reconnected.
#
# From the packet log:
#
#     15:18:44.222  heartbeat            (they were every ~10s)
#     15:18:46.999  workspace_commit_request
#     ...           nothing
#     15:19:24.174  reconnecting  attempt 1
#
# Commit-time verification runs the whole pytest suite -- 196 seconds on
# this project -- and ipc_dispatch ran inline on the event loop. So the
# loop could not run _heartbeat_loop, no heartbeat went out, and the
# client concluded the backend was gone. The commit itself was working
# fine; the connection died underneath it.
#
# The bug is not "commit is slow", it is "a blocking call ran on the
# event loop". That was true before commit-time verification too; the
# handlers were just fast enough that nobody could see it.

from __future__ import annotations

import asyncio
import json
import time

import pytest

from backend.websocket import handlers as handlers_mod


class RecordingSocket:
    def __init__(self):
        self.client_state = type("S", (), {"name": "CONNECTED"})()
        self.sent = []

    async def send(self, raw):
        self.sent.append((time.monotonic(), json.loads(raw)))

    async def accept(self):
        pass

    async def close(self, *a, **k):
        pass


SLOW_SECONDS = 0.4


def test_a_slow_handler_leaves_the_loop_free(monkeypatch):
    """The heartbeat task must keep running while a handler works.

    Proven by running something else on the loop AT THE SAME TIME. If
    the handler blocks, the other task cannot tick, which is exactly what
    happened to _heartbeat_loop.
    """
    def slow_dispatch(packet, on_progress=None):
        time.sleep(SLOW_SECONDS)
        return {"type": "workspace_details_result", "id": "x"}

    monkeypatch.setattr(handlers_mod, "ipc_dispatch", slow_dispatch)
    monkeypatch.setattr(handlers_mod, "IPC_HANDLED_TYPES", {"workspace_commit_request"})

    ticks = []

    async def scenario():
        handler = handlers_mod.WebSocketHandler(RecordingSocket())

        async def ticker():
            # Stands in for _heartbeat_loop: a separate task that must
            # keep getting scheduled while the handler runs.
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(SLOW_SECONDS / 8)

        beat = asyncio.create_task(ticker())
        try:
            await handler._dispatch({"type": "workspace_commit_request", "payload": {}})
        finally:
            beat.cancel()

    asyncio.run(scenario())

    # Inline, this would be 1 -- the tick before the block, and nothing
    # until it finished.
    assert len(ticks) > 3, (
        f"only {len(ticks)} ticks during a {SLOW_SECONDS}s handler; the event "
        f"loop was blocked, which is what stopped the heartbeat"
    )


def test_the_response_still_arrives_and_still_comes_first(monkeypatch):
    """Off the loop, not out of order. The await is what keeps this
    connection's packets in the sequence they were asked for."""
    def dispatch(packet, on_progress=None):
        return {"type": "workspace_details_result", "id": "after"}

    monkeypatch.setattr(handlers_mod, "ipc_dispatch", dispatch)
    monkeypatch.setattr(handlers_mod, "IPC_HANDLED_TYPES", {"workspace_commit_request"})

    socket = RecordingSocket()

    async def scenario():
        handler = handlers_mod.WebSocketHandler(socket)
        await handler._dispatch({"type": "workspace_commit_request", "payload": {}})
        await handler._send({"type": "sentinel"})

    asyncio.run(scenario())

    kinds = [packet["type"] for _, packet in socket.sent]
    assert kinds == ["workspace_details_result", "sentinel"]


def test_progress_from_a_slow_handler_reaches_the_client(monkeypatch):
    """Three minutes with no output is not a reason to believe anything
    is happening."""
    def dispatch(packet, on_progress=None):
        if on_progress:
            on_progress("running your full test suite")
        return {"type": "workspace_details_result", "id": "x"}

    monkeypatch.setattr(handlers_mod, "ipc_dispatch", dispatch)
    monkeypatch.setattr(handlers_mod, "IPC_HANDLED_TYPES", {"workspace_commit_request"})

    socket = RecordingSocket()

    async def scenario():
        handler = handlers_mod.WebSocketHandler(socket)
        await handler._dispatch({"type": "workspace_commit_request", "payload": {}})
        # The progress packet is scheduled from the executor thread, so
        # give the loop a turn to deliver it.
        await asyncio.sleep(0.05)

    asyncio.run(scenario())

    said = [p.get("label") or p.get("message")
            for _, p in socket.sent if p.get("type") == "progress"]
    assert "running your full test suite" in said


def test_the_commit_handler_accepts_a_progress_callback():
    """Threaded end to end, so the wiring cannot rot silently."""
    import inspect

    from backend import ipc_router

    signature = inspect.signature(ipc_router._handle_workspace_commit)
    assert "on_progress" in signature.parameters

    signature = inspect.signature(ipc_router.dispatch)
    assert "on_progress" in signature.parameters


@pytest.mark.parametrize("packet_type", ["workspace_discard_request",
                                         "workspace_rollback_request"])
def test_other_handlers_are_unaffected_by_the_extra_argument(packet_type):
    """dispatch passes on_progress only to the handler that takes it."""
    from backend import ipc_router

    response = ipc_router.dispatch(
        {"type": packet_type, "payload": {"id": "nope", "files": None,
                                          "user_text": "discard the changes"}},
        on_progress=lambda message: None,
    )

    # Whatever it answers, it must not have raised on the extra argument.
    assert isinstance(response, dict) and response.get("type")
