"""A turn that never ends must not take the session with it.

THE FAILURE THIS EXISTS FOR
---------------------------
A turn holds WebSocketHandler._turn_lock for its whole life. One that
never finishes therefore holds it forever: every later message queues
behind it for the life of the connection, and the client sits on an
open bubble waiting for a stream_end that is never coming. Restarting
the backend is the only way out.

That is not hypothetical. Before the Ludo layer capped its own poll,
a queued generation would have polled for 900 seconds inside a turn.

ONE PATH, TWO REASONS
---------------------
Stop and timeout differ in what triggers them and in what the user is
told. Everything after the trigger -- set the flag, cancel the task,
close the bubble, release the lock -- is _end_turn, once, because two
code paths that must stay in step will not. These tests pin the shared
path and the two things that differ.

NO TEST HERE SLEEPS FOR A BUDGET
--------------------------------
Budgets are monkeypatched to fractions of a second, and long turns are
simulated with an asyncio.Event that is never set. A suite that waited
out a real deadline would pay 120 seconds per case.
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from backend.core import turn_budget
from backend.websocket import handlers
from backend.websocket.handlers import WebSocketHandler


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        try:
            self.sent.append(json.loads(message)
                             if isinstance(message, str) else message)
        except ValueError:
            self.sent.append({"type": "raw", "message": message})

    async def close(self):
        pass


@pytest.fixture
def handler():
    return WebSocketHandler(FakeWebSocket())


def _run(coro):
    return asyncio.run(coro)


def _kinds(handler):
    return [m.get("type") for m in handler.websocket.sent]


def _hanging(started):
    async def never_ends(_packet):
        started.set()
        await asyncio.sleep(60)
    return never_ends


# ======================================================
# The budgets themselves
# ======================================================

def test_budgets_exceed_the_work_they_contain():
    """A turn budget shorter than its own inner limit is not a safety
    net, it is a guaranteed failure that kills correct work -- and it
    is invisible until somebody's Blender render dies at six minutes.

    This caught exactly that during the build: cli_program was 360
    while cli_programs.PROGRAMS["blender"] carries 900, because a
    typed `blender --background ...` IS Blender.
    """
    too_short = []
    for kind in turn_budget.kinds():
        inner = turn_budget.inner_limit_for(kind)
        if inner is not None and turn_budget.BUDGETS[kind] <= inner:
            too_short.append((kind, turn_budget.BUDGETS[kind], inner))

    assert too_short == [], f"budget shorter than its work: {too_short}"


def test_a_ludo_budget_covers_the_whole_chain():
    """NOT simply JOB_TOTAL_SECONDS. A generate_model is two calls --
    Ludo has no text-to-3D -- plus a download."""
    from backend.ludo import ludo_client

    one_call = max(ludo_client.REQUEST_TIMEOUT_SECONDS,
                   ludo_client.JOB_TOTAL_SECONDS)
    chain = (2 * one_call) + ludo_client.DOWNLOAD_TIMEOUT_SECONDS

    assert turn_budget.inner_limit_for("ludo") == chain
    assert turn_budget.BUDGETS["ludo"] > chain
    assert turn_budget.BUDGETS["ludo"] > ludo_client.JOB_TOTAL_SECONDS * 2


def test_the_budget_resolution_order():
    """Per-turn, then per-kind, then the backstop -- the same order as
    every other path in this codebase."""
    assert turn_budget.budget_for("chat", requested=45) == 45
    assert turn_budget.budget_for("chat") == turn_budget.BUDGETS["chat"]
    assert turn_budget.budget_for("nothing-like-this") == turn_budget.TURN_MAX_SECONDS


def test_an_absurd_request_is_capped_not_refused():
    """A caller asking for an hour means "as long as possible"."""
    assert turn_budget.budget_for("chat", requested=99_999) == \
        turn_budget.SCRIPT_MAX_SECONDS


def test_a_nonsense_request_falls_back(monkeypatch):
    assert turn_budget.budget_for("chat", requested="soon") == \
        turn_budget.BUDGETS["chat"]
    assert turn_budget.budget_for("chat", requested=-5) == \
        turn_budget.BUDGETS["chat"]


def test_the_environment_can_override(monkeypatch):
    monkeypatch.setenv(turn_budget.ENV_TURN_MAX, "7")

    assert turn_budget.budget_for("blender") == 7.0


# ======================================================
# The deadline fires
# ======================================================

def test_a_turn_past_its_budget_is_ended(handler, monkeypatch):
    monkeypatch.setitem(turn_budget.BUDGETS, "chat", 0.05)
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
        await handler._dispatch({"type": "chat_request", "turnKind": "chat"})
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(handler.wait_for_turns(), timeout=5)

    _run(scenario())

    assert cancelled == [True]


def test_a_timeout_releases_the_lock(handler, monkeypatch):
    """The one that matters most. A lock held by a wedged turn is a
    permanently dead session -- every later message queues behind it
    for the life of the connection."""
    monkeypatch.setitem(turn_budget.BUDGETS, "chat", 0.05)
    started = asyncio.Event()
    ran = []

    async def quick(_packet):
        ran.append("second")

    async def scenario():
        monkeypatch.setattr(handler, "_handle_chat_request", _hanging(started))
        await handler._dispatch({"type": "chat_request", "turnKind": "chat"})
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(handler.wait_for_turns(), timeout=5)

        monkeypatch.setattr(handler, "_handle_chat_request", quick)
        await handler._dispatch({"type": "chat_request", "turnKind": "chat"})
        await asyncio.wait_for(handler.wait_for_turns(), timeout=5)

    _run(scenario())

    assert ran == ["second"]
    assert not handler._turn_lock.locked()


def test_a_timeout_closes_the_bubble(handler, monkeypatch):
    """The ended turn never sends its own stream_end, and chat.js waits
    for one -- without this the UI spins forever on a turn that is
    already over."""
    monkeypatch.setitem(turn_budget.BUDGETS, "chat", 0.05)
    started = asyncio.Event()

    async def never_ends(_packet):
        handler._turn_stream_id = "req-42"
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(handler, "_handle_chat_request", never_ends)

    async def scenario():
        await handler._dispatch({"type": "chat_request", "turnKind": "chat"})
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(handler.wait_for_turns(), timeout=5)

    _run(scenario())

    ends = [m for m in handler.websocket.sent if m.get("type") == "stream_end"]
    assert len(ends) == 1
    assert ends[0].get("requestId") == "req-42"


def test_a_timeout_stops_the_worker_thread(handler, monkeypatch):
    """Cancelling the task stops the coroutine awaiting the result. The
    executor thread would go on sending regardless, which is what
    delivery_stopped() is read for -- and the flag has to be set
    BEFORE the cancel or tokens escape in between."""
    monkeypatch.setitem(turn_budget.BUDGETS, "chat", 0.05)
    started = asyncio.Event()
    seen = {}

    async def never_ends(_packet):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            seen["stopped_when_cancelled"] = handler.delivery_stopped()
            raise

    monkeypatch.setattr(handler, "_handle_chat_request", never_ends)

    async def scenario():
        await handler._dispatch({"type": "chat_request", "turnKind": "chat"})
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(handler.wait_for_turns(), timeout=5)

    _run(scenario())

    assert seen["stopped_when_cancelled"] is True


def test_a_timeout_says_the_limit_and_the_cost(handler, monkeypatch):
    """The honesty clause is shared with stop because the FACTS are
    shared: Python cannot kill the worker thread, no provider takes a
    cancellation signal, and a Ludo credit is spent at request time."""
    monkeypatch.setitem(turn_budget.BUDGETS, "chat", 0.05)
    started = asyncio.Event()
    monkeypatch.setattr(handler, "_handle_chat_request", _hanging(started))

    async def scenario():
        await handler._dispatch({"type": "chat_request", "turnKind": "chat"})
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(handler.wait_for_turns(), timeout=5)

    _run(scenario())

    told = [m for m in handler.websocket.sent if m.get("type") == "turn_timeout"]
    assert told, _kinds(handler)
    message = told[0]["message"]
    assert "ran past its" in message
    assert "not refunded" in message
    assert "finishes on its own" in message
    assert told[0]["budgetSeconds"] == pytest.approx(0.05)


def test_a_normal_turn_never_fires_the_timer(handler, monkeypatch):
    monkeypatch.setitem(turn_budget.BUDGETS, "chat", 5.0)

    async def quick(_packet):
        return None

    monkeypatch.setattr(handler, "_handle_chat_request", quick)

    async def scenario():
        await handler._dispatch({"type": "chat_request", "turnKind": "chat"})
        await handler.wait_for_turns()
        await asyncio.sleep(0.05)

    _run(scenario())

    assert "turn_timeout" not in _kinds(handler)
    assert handler._turn_deadline is None


def test_the_timer_is_disarmed_even_when_the_turn_raises(handler, monkeypatch):
    """A timer left armed by a crashed turn fires against whatever is
    running next."""
    monkeypatch.setitem(turn_budget.BUDGETS, "chat", 5.0)

    async def explode(_packet):
        raise RuntimeError("boom")

    monkeypatch.setattr(handler, "_handle_chat_request", explode)

    async def scenario():
        await handler._dispatch({"type": "chat_request", "turnKind": "chat"})
        await handler.wait_for_turns()

    _run(scenario())

    assert handler._turn_deadline is None


# ======================================================
# Stop and timeout together
# ======================================================

def test_a_stop_during_a_timeout_does_not_end_the_turn_twice(handler,
                                                             monkeypatch):
    """A user clicks Stop at 119.8s and the deadline fires at 120.0s.
    Without the guard the client gets two stream_end packets for one
    bubble and two contradictory explanations."""
    monkeypatch.setitem(turn_budget.BUDGETS, "chat", 0.05)
    started = asyncio.Event()
    monkeypatch.setattr(handler, "_handle_chat_request", _hanging(started))

    async def scenario():
        await handler._dispatch({"type": "chat_request", "turnKind": "chat"})
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.sleep(0.1)                       # let the deadline fire
        await handler._dispatch({"type": handlers.STOP_TYPE})
        await asyncio.wait_for(handler.wait_for_turns(), timeout=5)

    _run(scenario())

    assert _kinds(handler).count("stream_end") == 1
    assert _kinds(handler).count("stream_cancelled") == 1


def test_a_timeout_after_a_stop_adds_nothing(handler, monkeypatch):
    """The reverse order. The first reason wins and the second is a
    no-op, rather than reopening a bubble that is already closed."""
    monkeypatch.setitem(turn_budget.BUDGETS, "chat", 0.15)
    started = asyncio.Event()
    monkeypatch.setattr(handler, "_handle_chat_request", _hanging(started))

    async def scenario():
        await handler._dispatch({"type": "chat_request", "turnKind": "chat"})
        await asyncio.wait_for(started.wait(), timeout=2)
        await handler._dispatch({"type": handlers.STOP_TYPE})
        await asyncio.sleep(0.25)                      # deadline would fire here
        await asyncio.wait_for(handler.wait_for_turns(), timeout=5)

    _run(scenario())

    assert _kinds(handler).count("stream_end") == 1
    assert "turn_timeout" not in _kinds(handler)


def test_the_two_endings_share_one_path():
    """Pinned in the source. Two code paths that must stay in step
    will not, so there is one and it takes a reason."""
    stop = inspect.getsource(WebSocketHandler._handle_chat_stop)
    deadline = inspect.getsource(WebSocketHandler._on_turn_deadline)

    assert "_end_turn(" in stop
    assert "_end_turn(" in deadline
    # Neither may cancel on its own.
    assert "task.cancel()" not in stop
    assert "task.cancel()" not in deadline


def test_both_endings_carry_the_same_caveat(handler):
    """The trigger differs; the tier facts do not."""
    stopped = handler._ending_message("stopped", 0.0)
    timed_out = handler._ending_message("timeout", 120.0)

    assert WebSocketHandler.ENDING_CAVEAT in stopped
    assert WebSocketHandler.ENDING_CAVEAT in timed_out
    assert stopped != timed_out


# ======================================================
# The session stays alive while it happens
# ======================================================

def test_heartbeats_flow_during_the_timeout_window(handler, monkeypatch):
    """The dispatch change is what makes a timeout observable at all:
    while the loop was parked awaiting the turn, a client could not
    even be told the backend was still there."""
    monkeypatch.setitem(turn_budget.BUDGETS, "chat", 0.4)
    started = asyncio.Event()
    monkeypatch.setattr(handler, "_handle_chat_request", _hanging(started))
    monkeypatch.setattr(handler, "_dispatch_off_the_loop",
                        lambda packet: _ack())
    monkeypatch.setattr(handlers, "IPC_HANDLED_TYPES", frozenset({"heartbeat"}))

    async def _ack():
        return {"type": "heartbeat_ack"}

    async def scenario():
        await handler._dispatch({"type": "chat_request", "turnKind": "chat"})
        await asyncio.wait_for(started.wait(), timeout=2)

        await handler._dispatch({"type": "heartbeat"})
        assert "heartbeat_ack" in _kinds(handler), "acked before the timeout"

        await asyncio.wait_for(handler.wait_for_turns(), timeout=5)

        await handler._dispatch({"type": "heartbeat"})

    _run(scenario())

    assert _kinds(handler).count("heartbeat_ack") == 2, \
        "acked before and after the timeout"


# ======================================================
# Tier 3: a paid job outliving its turn
# ======================================================

def test_a_ludo_timeout_keeps_the_job_id(handler, monkeypatch):
    """The credit is already spent and the job may still finish.
    Dropping the id turns a recoverable delay into a paid-for asset
    nobody can ever reach."""
    monkeypatch.setitem(turn_budget.BUDGETS, "ludo", 0.05)
    started = asyncio.Event()

    async def never_ends(_packet):
        handler._turn_job_id = "job-abc123"
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(handler, "_handle_chat_request", never_ends)

    async def scenario():
        await handler._dispatch({"type": "chat_request", "turnKind": "ludo"})
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(handler.wait_for_turns(), timeout=5)

    _run(scenario())

    told = [m for m in handler.websocket.sent if m.get("type") == "turn_timeout"][0]
    assert told["jobId"] == "job-abc123"
    assert "collect job-abc123 in Ludo" in told["message"]
    assert "costs nothing" in told["message"]


def test_a_turn_with_no_job_says_nothing_about_collecting(handler, monkeypatch):
    """Offering to collect a job that does not exist would be an
    instruction that fails."""
    monkeypatch.setitem(turn_budget.BUDGETS, "chat", 0.05)
    started = asyncio.Event()
    monkeypatch.setattr(handler, "_handle_chat_request", _hanging(started))

    async def scenario():
        await handler._dispatch({"type": "chat_request", "turnKind": "chat"})
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(handler.wait_for_turns(), timeout=5)

    _run(scenario())

    told = [m for m in handler.websocket.sent if m.get("type") == "turn_timeout"][0]
    assert "jobId" not in told
    assert "collect" not in told["message"]


# ======================================================
# Predicting the kind
# ======================================================

@pytest.mark.parametrize("text,kind", [
    ("make a car in Blender", "blender"),
    ("in Blender export it as fbx", "blender"),
    ("make a car in Ludo", "ludo"),
    ("collect abc123def in Ludo", "ludo"),
    ("unity env", "unity_cli"),
    ("blender --version", "cli_program"),
    ("what is the weather in Paris?", "chat"),
    ("make a car", "chat"),
    ("", "chat"),
])
def test_the_kind_is_predicted_from_the_message(text, kind):
    """The deadline is armed before the orchestrator has decided
    anything, so the budget has to predict the route rather than wait
    for it."""
    assert turn_budget.classify(text) == kind


def test_budgeting_and_routing_ask_the_same_question():
    """A budget that decided differently from the router would give a
    Blender render the chat budget and kill it at two minutes. Both
    ask the same public predicates, which is why those are public."""
    from backend.blender import blender_nl_mapping
    from backend.ludo import ludo_nl_mapping

    for text in ("make a car in Blender", "make a car in Ludo",
                 "make a car", "make a car in Ludo and Blender"):
        kind = turn_budget.classify(text)
        if kind == "blender":
            assert blender_nl_mapping.names_blender(text)
            assert not blender_nl_mapping.names_another_tool(text)
        if kind == "ludo":
            assert ludo_nl_mapping.names_ludo(text)
            assert not ludo_nl_mapping.names_another_tool(text)


def test_an_ambiguous_message_gets_the_short_budget():
    """Naming two tools routes to neither, so it is a chat turn and
    must not inherit either tool's long budget."""
    assert turn_budget.classify("make a car in Ludo and Blender") == "chat"


def test_the_kind_comes_from_the_latest_message_not_the_first(handler):
    """Multi-turn packets carry the whole conversation. Classifying by
    messages[0] would budget every turn by whatever was said at the
    start of the session."""
    packet = {"messages": [
        {"role": "user", "content": "make a car in Blender"},
        {"role": "assistant", "content": "Built Car in Blender."},
        {"role": "user", "content": "what is the weather in Paris?"},
    ]}

    assert handler._latest_user_text(packet) == "what is the weather in Paris?"
    assert turn_budget.classify(handler._latest_user_text(packet)) == "chat"


def test_a_client_cannot_grant_itself_a_longer_budget(handler, monkeypatch):
    """A client that names its own kind could name the longest one.
    The server predicts instead -- a supplied kind is only honoured
    where the caller is trusted, which the WebSocket client is not."""
    seen = {}

    def record(kind, requested=None):
        seen["kind"] = kind
        return 5.0

    monkeypatch.setattr(turn_budget, "budget_for", record)

    async def scenario():
        loop = asyncio.get_running_loop()
        handler._arm_deadline({"messages": [
            {"role": "user", "content": "what is the weather?"}]})
        handler._disarm_deadline()

    _run(scenario())

    assert seen["kind"] == "chat"


def test_a_blender_turn_gets_room_to_finish(handler):
    """The failure this prevents: a correct render killed at two
    minutes because the turn was budgeted as chat."""
    async def scenario():
        handler._arm_deadline({"messages": [
            {"role": "user", "content": "make a low-poly tree in Blender"}]})
        budget = handler._turn_budget
        handler._disarm_deadline()
        return budget

    budget = _run(scenario())

    from backend.blender import blender_actions
    assert budget > blender_actions.DEFAULT_TIMEOUT_SECONDS


# ======================================================
# Naming a tool is not using one
# ======================================================

def test_naming_blender_without_a_buildable_request_is_a_chat_turn():
    """MEASURED. "Create a 3D Model in Blender of an Anime Swordsman"
    names Blender, so classify returned "blender" and granted the turn
    a 960-second budget. The Blender layer then declined it in
    microseconds -- there is no swordsman recipe -- and a model
    answered instead, holding a budget sized for a render that was
    never going to happen.

    The question is not "does this name Blender" but "would Blender
    actually be asked to do something"."""
    said = "Create a 3D Model in Blender of a Anime Swordsman holding a katana"

    assert turn_budget.classify(said) == "chat"
    assert turn_budget.budget_for(turn_budget.classify(said)) == \
        turn_budget.BUDGETS["chat"]


def test_a_buildable_blender_request_still_gets_the_long_budget():
    """The tightening must not take the budget from work that needs
    it -- that would kill a correct render at two minutes."""
    assert turn_budget.classify("make a low-poly tree in Blender") == "blender"


@pytest.mark.parametrize("said,kind", [
    ("in Blender, what is good topology?", "chat"),
    ("in Ludo what does a credit cost?", "chat"),
    ("make a car in Ludo", "ludo"),
    ("collect abc123def in Ludo", "ludo"),
])
def test_only_real_work_gets_a_tool_budget(said, kind):
    assert turn_budget.classify(said) == kind


def test_the_classifier_is_free():
    """It runs before the turn does, on the event loop. A classifier
    that spent an inference would be the very cost this whole change
    was made to avoid."""
    import backend.core.turn_budget as module
    source = __import__("inspect").getsource(module.classify)

    for spender in ("generate", "infer", "stream", "orchestrate", "prime"):
        assert spender not in source, f"classify() may not call {spender}"
