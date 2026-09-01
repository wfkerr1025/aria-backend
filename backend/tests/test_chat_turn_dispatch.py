"""A slow turn must not freeze the socket, and must not race the next one.

THE FAILURE THIS EXISTS FOR
---------------------------
The message loop read packets with

    async for raw in self.websocket:
        await self._dispatch(packet)

so for as long as a turn was awaited there, NOTHING ELSE WAS READ --
heartbeats included.

Measured twice on the developer's machine:

  * a commit request ran the pytest suite for three minutes; the last
    heartbeat went out at 15:18:44, none followed, and at 15:19:24 the
    client gave up and reported "reconnecting" while the commit was
    still working; and

  * a Ludo.ai image generation took 25 seconds and every heartbeat_ack
    stalled until it finished. A 3D generation took two minutes with
    the UI showing nothing at all.

THE SECOND HALF, WHICH IS NOT THE SAME PROBLEM
----------------------------------------------
Letting the loop run on means a packet arriving DURING a turn is now
handled during it. For a heartbeat that is the whole point. For a
context_reset or a model override it would be a race that could not
happen before -- they mutate the state a turn is reading.

So those take a lock, in arrival order, and these tests pin both
halves: the loop keeps reading, and nothing that mutates overlaps.
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from backend.websocket import handlers
from backend.websocket.handlers import WebSocketHandler


class FakeWebSocket:
    """A socket that records what was sent and never blocks.

    _send serialises to a JSON string before it reaches the socket, so
    the recorder decodes -- asserting on raw text would pass for the
    wrong reasons.
    """

    def __init__(self, incoming=None):
        self.sent = []
        self._incoming = list(incoming or [])

    async def send(self, message):
        try:
            self.sent.append(json.loads(message)
                             if isinstance(message, str) else message)
        except ValueError:
            self.sent.append({"type": "raw", "message": message})

    async def close(self):
        pass

    def __aiter__(self):
        async def gen():
            for message in self._incoming:
                yield message
        return gen()


@pytest.fixture
def handler():
    return WebSocketHandler(FakeWebSocket())


def _run(coro):
    return asyncio.run(coro)


# ======================================================
# The loop keeps reading
# ======================================================

def test_a_chat_request_does_not_block_the_dispatcher(handler, monkeypatch):
    """The point of the whole change. _dispatch must come back before
    the turn has finished, or the loop cannot read the next packet."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_turn(_packet):
        started.set()
        await release.wait()

    monkeypatch.setattr(handler, "_handle_chat_request", slow_turn)

    async def scenario():
        await handler._dispatch({"type": "chat_request", "messages": []})
        # If _dispatch had awaited the turn, this line is unreachable
        # until the turn ends -- and the turn is waiting on us.
        await asyncio.wait_for(started.wait(), timeout=2)
        assert any(not t.done() for t in handler._turns_in_flight)

        release.set()
        await handler.wait_for_turns()

    _run(scenario())


def test_a_heartbeat_is_answered_while_a_turn_runs(handler, monkeypatch):
    """Measured: heartbeat acks stopped for the whole of a 25-second
    generation, and the client reads that silence as a dead backend."""
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow_turn(_packet):
        started.set()
        await release.wait()

    async def fake_ipc(packet):
        return {"type": "heartbeat_ack"}

    monkeypatch.setattr(handler, "_handle_chat_request", slow_turn)
    monkeypatch.setattr(handler, "_dispatch_off_the_loop", fake_ipc)
    monkeypatch.setattr(handlers, "IPC_HANDLED_TYPES", frozenset({"heartbeat"}))

    async def scenario():
        await handler._dispatch({"type": "chat_request", "messages": []})
        await asyncio.wait_for(started.wait(), timeout=2)

        await handler._dispatch({"type": "heartbeat"})

        assert any(m.get("type") == "heartbeat_ack"
                   for m in handler.websocket.sent), \
            "the heartbeat was not answered while the turn ran"

        release.set()
        await handler.wait_for_turns()

    _run(scenario())


# ======================================================
# ...but nothing that mutates overlaps
# ======================================================

def test_two_chat_turns_never_overlap(handler, monkeypatch):
    """Two turns in one conversation would interleave their tokens and
    race on the session state each one writes."""
    overlapping = []
    running = {"count": 0}

    async def turn(_packet):
        running["count"] += 1
        overlapping.append(running["count"])
        await asyncio.sleep(0.02)
        running["count"] -= 1

    monkeypatch.setattr(handler, "_handle_chat_request", turn)

    async def scenario():
        for _ in range(4):
            await handler._dispatch({"type": "chat_request", "messages": []})
        await handler.wait_for_turns()

    _run(scenario())

    assert overlapping == [1, 1, 1, 1], f"turns overlapped: {overlapping}"


def test_a_context_reset_cannot_land_mid_turn(handler, monkeypatch):
    """It clears the history the running turn is reading. Before this
    change the loop was blocked, so it could not happen; the lock is
    what keeps that true."""
    order = []

    async def turn(_packet):
        order.append("turn:start")
        await asyncio.sleep(0.02)
        order.append("turn:end")

    async def reset(_packet):
        order.append("reset")

    monkeypatch.setattr(handler, "_handle_chat_request", turn)
    monkeypatch.setattr(handler, "_handle_context_reset", reset)

    async def scenario():
        await handler._dispatch({"type": "chat_request", "messages": []})
        await handler._dispatch({"type": "context_reset"})
        await handler.wait_for_turns()

    _run(scenario())

    assert order == ["turn:start", "turn:end", "reset"]


def test_arrival_order_is_kept(handler, monkeypatch):
    """asyncio.Lock hands off in arrival order, so these packets keep
    exactly the ordering they had when the loop blocked on them."""
    order = []

    def record(name):
        async def handle(_packet):
            order.append(name)
            await asyncio.sleep(0.01)
        return handle

    monkeypatch.setattr(handler, "_handle_chat_request", record("chat"))
    monkeypatch.setattr(handler, "_handle_context_reset", record("reset"))
    monkeypatch.setattr(handler, "_handle_model_override", record("override"))
    monkeypatch.setattr(handler, "_handle_switch_to_lighter_model",
                        record("switch"))

    async def scenario():
        for kind in ("chat_request", "context_reset", "load_model_override",
                     "switch_to_lighter_model"):
            await handler._dispatch({"type": kind})
        await handler.wait_for_turns()

    _run(scenario())

    assert order == ["chat", "reset", "override", "switch"]


def test_every_state_mutating_packet_is_serialised():
    """A new packet that touches turn state and skips the lock is the
    race this change would otherwise introduce."""
    assert handlers.SERIALISED_TYPES == frozenset({
        "chat_request", "load_model_override", "switch_to_lighter_model",
        "context_reset"})


def test_a_read_only_packet_is_not_serialised(handler, monkeypatch):
    """Status reads and tool listings mutate nothing and must not
    queue behind a two-minute generation -- that was the old
    behaviour, and it is what made the app look dead."""
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow_turn(_packet):
        started.set()
        await release.wait()

    answered = []

    async def fake_ipc(packet):
        answered.append(packet["type"])
        return {"type": "ok"}

    monkeypatch.setattr(handler, "_handle_chat_request", slow_turn)
    monkeypatch.setattr(handler, "_dispatch_off_the_loop", fake_ipc)
    monkeypatch.setattr(handlers, "IPC_HANDLED_TYPES",
                        frozenset({"models_list_request"}))

    async def scenario():
        await handler._dispatch({"type": "chat_request", "messages": []})
        await asyncio.wait_for(started.wait(), timeout=2)
        await handler._dispatch({"type": "models_list_request"})

        assert answered == ["models_list_request"]

        release.set()
        await handler.wait_for_turns()

    _run(scenario())


# ======================================================
# Failures and teardown
# ======================================================

def test_a_turn_that_raises_is_reported_not_swallowed(handler, monkeypatch):
    """The message loop's try/except cannot catch this any more -- it
    is a different task. Without the catch the turn fails silently and
    asyncio prints "Task exception was never retrieved" to a log
    nobody reads."""
    async def explode(_packet):
        raise RuntimeError("the turn fell over")

    monkeypatch.setattr(handler, "_handle_chat_request", explode)

    async def scenario():
        await handler._dispatch({"type": "chat_request", "messages": []})
        await handler.wait_for_turns()

    _run(scenario())

    assert any(m.get("type") == "error" and "fell over" in str(m.get("message"))
               for m in handler.websocket.sent), handler.websocket.sent


def test_a_turn_that_raises_does_not_hold_the_lock(handler, monkeypatch):
    """A lock left held by a crashed turn would freeze every turn
    after it -- the same symptom, permanently."""
    calls = []

    async def explode(_packet):
        calls.append("first")
        raise RuntimeError("boom")

    async def fine(_packet):
        calls.append("second")

    async def scenario():
        monkeypatch.setattr(handler, "_handle_chat_request", explode)
        await handler._dispatch({"type": "chat_request"})
        await handler.wait_for_turns()

        monkeypatch.setattr(handler, "_handle_chat_request", fine)
        await handler._dispatch({"type": "chat_request"})
        await asyncio.wait_for(handler.wait_for_turns(), timeout=2)

    _run(scenario())

    assert calls == ["first", "second"]
    assert not handler._turn_lock.locked()


def test_a_finished_turn_is_not_held_forever(handler, monkeypatch):
    """The set exists to keep a task alive and cancellable, not to
    accumulate one entry per message for the life of the connection."""
    async def turn(_packet):
        return None

    monkeypatch.setattr(handler, "_handle_chat_request", turn)

    async def scenario():
        for _ in range(3):
            await handler._dispatch({"type": "chat_request"})
        await handler.wait_for_turns()
        await asyncio.sleep(0)

    _run(scenario())

    assert handler._turns_in_flight == set()


def test_a_running_turn_is_cancelled_when_the_connection_ends(handler,
                                                              monkeypatch):
    """Otherwise a two-minute generation goes on writing to a socket
    nobody is reading, and outlives the handler that owns its state."""
    started = asyncio.Event()
    finished = []

    async def never_ends(_packet):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            finished.append("cancelled")
            raise

    monkeypatch.setattr(handler, "_handle_chat_request", never_ends)

    async def scenario():
        await handler._dispatch({"type": "chat_request"})
        await asyncio.wait_for(started.wait(), timeout=2)

        await asyncio.wait_for(handler._cancel_turns_in_flight(), timeout=2)

    _run(scenario())

    assert finished == ["cancelled"]


def test_the_message_loop_cancels_turns_on_the_way_out():
    """Pinned in the source: the teardown must cancel before the
    heartbeat task, and before the connection is marked closed."""
    source = inspect.getsource(WebSocketHandler.handle)

    assert "_cancel_turns_in_flight" in source
    assert (source.index("_cancel_turns_in_flight")
            < source.index("heartbeat_task.cancel()"))


# ======================================================
# Stopping a turn
#
# Only possible at all because chat turns stopped blocking the message
# loop: a stop packet sent during a turn could never be READ while the
# loop was parked awaiting that same turn.
# ======================================================

def test_a_stop_is_not_serialised():
    """A stop that queued behind the turn it is meant to stop would
    arrive after that turn had finished. That is not a stop, it is a
    no-op with a misleading name."""
    assert handlers.STOP_TYPE not in handlers.SERIALISED_TYPES


def test_a_stop_reaches_a_running_turn(handler, monkeypatch):
    started = asyncio.Event()
    cancelled = []

    async def never_ends(_packet):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(handler, "_handle_chat_request", never_ends)

    async def scenario():
        await handler._dispatch({"type": "chat_request", "messages": []})
        await asyncio.wait_for(started.wait(), timeout=2)

        await asyncio.wait_for(
            handler._dispatch({"type": handlers.STOP_TYPE}), timeout=2)
        await handler.wait_for_turns()

    _run(scenario())

    assert cancelled == [True]


def test_a_stop_closes_the_bubble(handler, monkeypatch):
    """The cancelled turn never sends its own stream_end, and chat.js
    waits for one -- so a stop without this leaves the UI spinning
    forever on a turn that is already over."""
    started = asyncio.Event()

    async def never_ends(_packet):
        handler._turn_stream_id = "req-1"
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(handler, "_handle_chat_request", never_ends)

    async def scenario():
        await handler._dispatch({"type": "chat_request", "messages": []})
        await asyncio.wait_for(started.wait(), timeout=2)
        await handler._dispatch({"type": handlers.STOP_TYPE})
        await handler.wait_for_turns()

    _run(scenario())

    kinds = [m.get("type") for m in handler.websocket.sent]
    assert "stream_end" in kinds
    ends = [m for m in handler.websocket.sent if m.get("type") == "stream_end"]
    assert ends[0].get("requestId") == "req-1", "closed the wrong bubble"


def test_a_stop_sets_the_flag_before_cancelling(handler, monkeypatch):
    """The worker thread is what actually stops sending. If the cancel
    landed first there would be a window in which tokens from a
    cancelled turn still reached the client."""
    started = asyncio.Event()
    seen = {}

    async def never_ends(_packet):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            seen["flag_set_when_cancelled"] = (
                handler._turn_stop is not None and handler._turn_stop.is_set())
            raise

    monkeypatch.setattr(handler, "_handle_chat_request", never_ends)

    async def scenario():
        await handler._dispatch({"type": "chat_request", "messages": []})
        await asyncio.wait_for(started.wait(), timeout=2)
        await handler._dispatch({"type": handlers.STOP_TYPE})
        await handler.wait_for_turns()

    _run(scenario())

    assert seen["flag_set_when_cancelled"] is True


def test_stopping_nothing_says_so(handler):
    """A stop with no turn running must answer, not fall silent -- a
    button that does nothing visible reads as broken."""
    async def scenario():
        await handler._dispatch({"type": handlers.STOP_TYPE})

    _run(scenario())

    results = [m for m in handler.websocket.sent
               if m.get("type") == handlers.STOP_TYPE + "_result"]
    assert results and results[0]["stopped"] is False


def test_a_stop_does_not_claim_to_have_saved_anything(handler, monkeypatch):
    """Python cannot kill the worker thread, no provider accepts a
    cancellation signal, and a Ludo credit is spent when the request
    is made. Saying "stopped" without saying that would be the kind of
    half-truth this codebase keeps paying for."""
    started = asyncio.Event()

    async def never_ends(_packet):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(handler, "_handle_chat_request", never_ends)

    async def scenario():
        await handler._dispatch({"type": "chat_request"})
        await asyncio.wait_for(started.wait(), timeout=2)
        await handler._dispatch({"type": handlers.STOP_TYPE})
        await handler.wait_for_turns()

    _run(scenario())

    said = [m for m in handler.websocket.sent
            if m.get("type") == handlers.STOP_TYPE + "_result"][0]["message"]
    assert "not refunded" in said
    assert "finishes on its own" in said


def test_the_next_turn_runs_after_a_stop(handler, monkeypatch):
    """A cancelled turn must release the lock, or the stop button
    freezes every turn after it -- the same symptom, permanently."""
    started = asyncio.Event()
    ran = []

    async def never_ends(_packet):
        started.set()
        await asyncio.sleep(60)

    async def quick(_packet):
        ran.append("second")

    async def scenario():
        monkeypatch.setattr(handler, "_handle_chat_request", never_ends)
        await handler._dispatch({"type": "chat_request"})
        await asyncio.wait_for(started.wait(), timeout=2)
        await handler._dispatch({"type": handlers.STOP_TYPE})
        await handler.wait_for_turns()

        monkeypatch.setattr(handler, "_handle_chat_request", quick)
        await handler._dispatch({"type": "chat_request"})
        await asyncio.wait_for(handler.wait_for_turns(), timeout=2)

    _run(scenario())

    assert ran == ["second"]
    assert not handler._turn_lock.locked()


def test_the_turn_is_forgotten_once_it_ends(handler, monkeypatch):
    """A stale reference would let a stop cancel a task that has
    already finished, or close a bubble belonging to nothing."""
    async def quick(_packet):
        assert handler._turn_stop is not None

    monkeypatch.setattr(handler, "_handle_chat_request", quick)

    async def scenario():
        await handler._dispatch({"type": "chat_request"})
        await handler.wait_for_turns()

    _run(scenario())

    assert handler._turn_stop is None
    assert handler._turn_task is None
    assert handler._turn_stream_id is None


def test_only_a_chat_turn_is_stoppable(handler, monkeypatch):
    """A context_reset is a millisecond of bookkeeping. Letting a stop
    cancel one halfway would corrupt the state it exists to set."""
    async def reset(_packet):
        assert handler._turn_task is None, \
            "a context_reset must not be claimed as the stoppable turn"

    monkeypatch.setattr(handler, "_handle_context_reset", reset)

    async def scenario():
        await handler._dispatch({"type": "context_reset"})
        await handler.wait_for_turns()

    _run(scenario())


def test_the_worker_thread_stops_delivering(handler):
    """The half of a stop that actually stops the tokens.

    Cancelling the task stops the coroutine awaiting the result;
    streamer.stream() runs on a worker thread and would go on handing
    packets back through run_coroutine_threadsafe regardless. This is
    what the thread reads.
    """
    import threading

    assert handler.delivery_stopped() is False, "nothing running, nothing to stop"

    handler._turn_stop = threading.Event()
    assert handler.delivery_stopped() is False, "a running turn still delivers"

    handler._turn_stop.set()
    assert handler.delivery_stopped() is True


def test_the_stop_flag_is_readable_from_another_thread(handler):
    """It is a threading.Event and not an asyncio one for exactly this
    reason -- an asyncio.Event read from the executor thread is not
    safe and would not be seen."""
    import threading

    handler._turn_stop = threading.Event()
    seen = []

    def worker():
        seen.append(handler.delivery_stopped())

    handler._turn_stop.set()
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=2)

    assert seen == [True]
    assert isinstance(handler._turn_stop, threading.Event)


def test_send_packet_sync_asks_before_sending():
    """Pinned in the source: the check must come before the packet is
    inspected or forwarded, or a token slips out after the stop."""
    source = inspect.getsource(WebSocketHandler._stream_inference)
    body = source[source.index("def send_packet_sync"):]

    assert "delivery_stopped()" in body
    assert body.index("delivery_stopped()") < body.index('"stream_start"')


def test_a_tool_answer_costs_no_model():
    """MEASURED, and the reason this ordering is pinned.

    "Create a 3D Model in Blender of an Anime Swordsman" was answered
    by the Blender layer in microseconds -- and the turn still took
    NINE SECONDS. A second identical message took more than SEVENTY-
    SEVEN, until the user gave up and sent context_reset.

    The cause was placement: search_activation.prime() spends a real
    inference, and it ran BEFORE the short-circuits. On a 12B that is
    a model load, paid for an answer no model was going to write.

    The old comment already claimed these went "first, ahead of intent
    detection". They were simply below the expensive part.
    """
    from backend.core import turn_orchestrator

    lines = inspect.getsource(turn_orchestrator.orchestrate_turn).splitlines()

    def call_line(needle):
        # The CALL, not a mention: the comment above the short-circuits
        # names prime() while explaining why it comes after them, and
        # a plain text search matched that instead of the code.
        return next(i for i, line in enumerate(lines)
                    if needle in line and not line.strip().startswith("#"))

    for short_circuit in ("_unity_cli_reply(", "_cli_program_reply(",
                          "_blender_reply(", "_ludo_reply("):
        assert call_line(short_circuit) < call_line("search_activation.prime("), \
            f"{short_circuit} pays for a classifier inference it never reads"
        assert call_line(short_circuit) < call_line("intent = detect_intent("), \
            f"{short_circuit} runs after intent detection"
