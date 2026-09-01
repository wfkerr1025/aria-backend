"""A bubble that outlives the turn that opened it.

WHAT THIS FEATURE IS, AND THE TWO THINGS IT IS NOT
--------------------------------------------------
"Multi-turn streaming" has three readings and only two are reachable:

  A. One bubble surviving a tool call. Reachable -- this machinery --
     but it has no producer yet: the orchestrator answers in one pass,
     with no multi-step tool loop for a stream to survive. Building a
     caller for it would be a road to nowhere, so the tests below
     exercise the mechanism, not an imaginary user of it.

  B. A follow-up turn appending to the same bubble. Reachable, real,
     and the first caller: today "continue" opens a second reply with
     no relationship to the first.

  C. KV-cache reuse across turns. NOT reachable. No provider in
     backend.llm.providers exposes a resumable session handle, so an
     API that assumed it would have nothing behind it. The prompt is
     still rebuilt from `messages` every turn.

NOT BUILT ON streaming_engine_v2
--------------------------------
The plan said it had to be. It did not: suppressing a second
stream_start, holding stream_end and stopping delivery all live at the
HANDLER boundary, where _turn_stream_id and delivery_stopped() already
are. v2 also takes a resolved ProviderAdapter where v1 resolves
internally, so migrating means moving model resolution -- a separate
change with its own risk, and coupling them would have made both
harder to trust.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from backend.core import stream_session
from backend.core.stream_session import SessionRegistry
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


@pytest.fixture
def registry():
    return SessionRegistry()


def _run(coro):
    return asyncio.run(coro)


# ======================================================
# What counts as "continue"
# ======================================================

@pytest.mark.parametrize("said", [
    "continue", "Continue.", "continue!", "keep going", "go on",
    "carry on", "more", "next", "  Continue  ",
])
def test_a_nudge_is_a_continuation(said):
    assert WebSocketHandler._is_continuation(said) is True


@pytest.mark.parametrize("said", [
    "make a car in Blender",
    "what is the weather in Paris?",
    "continue the analysis of the third quarter results and then summarise it",
    "tell me more about how the router picks a model and why",
    "",
    "   ",
])
def test_anything_substantial_is_a_new_request(said):
    """A continuation is a nudge, not a paragraph. A long message that
    happens to start with "more" is asking for something new, and
    appending it to the previous bubble would hide it."""
    assert WebSocketHandler._is_continuation(said) is False


def test_a_long_message_starting_with_a_nudge_word_is_not_one():
    """The length guard, specifically -- it is what stops "continue"
    matching the start of a real request."""
    said = "continue " + ("x" * 60)

    assert WebSocketHandler._is_continuation(said) is False


# ======================================================
# The registry
# ======================================================

def test_a_session_is_attachable_after_it_opens(registry):
    registry.open("conv-1", model_id="nemo-12b-q5", stream_id="req-1")

    session = registry.attachable("conv-1", "nemo-12b-q5")
    assert session is not None
    assert session.stream_id == "req-1"


def test_a_session_belongs_to_one_conversation(registry):
    """The ghost-stream guard. A session adopted across a conversation
    boundary would stream tokens into a bubble the client is no longer
    showing."""
    registry.open("conv-1", model_id="m", stream_id="req-1")

    assert registry.attachable("conv-2", "m") is None
    assert registry.get("conv-2") is None


def test_a_different_model_is_not_attachable(registry):
    """A session pins one model. Attributing one model's tokens to
    another would be a lie told in the transcript."""
    registry.open("conv-1", model_id="nemo-12b-q5", stream_id="req-1")

    assert registry.attachable("conv-1", "phi-3-mini-4k-instruct-q4") is None
    assert registry.attachable("conv-1", "nemo-12b-q5") is not None


def test_opening_replaces_rather_than_stacks(registry):
    first = registry.open("conv-1", model_id="m", stream_id="req-1")
    second = registry.open("conv-1", model_id="m", stream_id="req-2")

    assert first.open is False
    assert second.open is True
    assert len(registry) == 1


def test_a_stale_session_is_not_attachable(registry, monkeypatch):
    """A session left from an hour ago is a bubble that has scrolled
    out of the world."""
    monkeypatch.setattr(stream_session, "IDLE_SECONDS", 0.01)
    registry.open("conv-1", model_id="m", stream_id="req-1")
    time.sleep(0.03)

    assert registry.attachable("conv-1", "m") is None
    assert len(registry) == 0, "the stale session was not cleaned up"


def test_a_closed_session_is_not_attachable(registry):
    registry.open("conv-1", model_id="m", stream_id="req-1")
    registry.close("conv-1", "stopped")

    assert registry.attachable("conv-1", "m") is None


def test_closing_all_leaves_nothing(registry):
    for name in ("a", "b", "c"):
        registry.open(name, model_id="m", stream_id=name)

    assert registry.close_all("disconnected") == 3
    assert len(registry) == 0


def test_a_session_records_its_turns(registry):
    session = registry.open("conv-1", model_id="m", stream_id="req-1")
    session.begin_turn("continue")
    session.begin_turn("continue")

    assert len(session.turns) == 2
    assert session.turns[0].user_text == "continue"


# ======================================================
# Attaching, in the handler
# ======================================================

def test_a_continuation_attaches_to_the_open_session(handler):
    handler._sessions.open(handler.conversation_id, model_id="m",
                           stream_id="req-1")

    handler._attach_session({"messages": [
        {"role": "user", "content": "continue"}]})

    assert handler._turn_session is not None
    assert handler._turn_session.stream_id == "req-1"


def test_an_ordinary_message_does_not_attach(handler):
    handler._sessions.open(handler.conversation_id, model_id="m",
                           stream_id="req-1")

    handler._attach_session({"messages": [
        {"role": "user", "content": "make a car in Blender"}]})

    assert handler._turn_session is None


def test_a_continuation_with_no_session_attaches_to_nothing(handler):
    """"continue" out of nowhere is an ordinary turn, not an error."""
    handler._attach_session({"messages": [
        {"role": "user", "content": "continue"}]})

    assert handler._turn_session is None


def test_remembering_needs_a_bubble(handler):
    """A turn that streamed nothing has no bubble to remember, and a
    session pointing at no requestId would send tokens the client
    drops."""
    handler._turn_stream_id = None
    handler._remember_session()

    assert len(handler._sessions) == 0


def test_a_streamed_turn_becomes_attachable(handler):
    handler._turn_stream_id = "req-9"
    handler._remember_session()

    assert handler._sessions.attachable(handler.conversation_id, None) is not None


def test_an_attached_turn_does_not_replace_the_session(handler):
    """Otherwise every continuation would reopen the bubble it just
    joined, and the turn list would never grow."""
    opened = handler._sessions.open(handler.conversation_id, model_id="m",
                                    stream_id="req-1")
    handler._turn_session = opened
    handler._turn_stream_id = "req-1"

    handler._remember_session()

    assert handler._sessions.attachable(handler.conversation_id, None) is opened


# ======================================================
# Ending it
# ======================================================

def test_a_stop_closes_the_session(handler, monkeypatch):
    """A session surviving a stop would glue the next message onto a
    bubble the user has already abandoned."""
    started = asyncio.Event()

    async def never_ends(_packet):
        handler._turn_stream_id = "req-1"
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(handler, "_handle_chat_request", never_ends)

    async def scenario():
        handler._sessions.open(handler.conversation_id, model_id="m",
                               stream_id="req-1")
        await handler._dispatch({"type": "chat_request"})
        await asyncio.wait_for(started.wait(), timeout=2)
        await handler._end_turn("stopped")
        await handler.wait_for_turns()

    _run(scenario())

    assert handler._sessions.attachable(handler.conversation_id, None) is None


def test_a_timeout_closes_the_session(handler, monkeypatch):
    from backend.core import turn_budget

    monkeypatch.setitem(turn_budget.BUDGETS, "chat", 0.05)
    started = asyncio.Event()

    async def never_ends(_packet):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(handler, "_handle_chat_request", never_ends)

    async def scenario():
        handler._sessions.open(handler.conversation_id, model_id="m",
                               stream_id="req-1")
        await handler._dispatch({"type": "chat_request", "turnKind": "chat"})
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(handler.wait_for_turns(), timeout=5)

    _run(scenario())

    assert handler._sessions.attachable(handler.conversation_id, None) is None


def test_context_reset_closes_the_session(handler, monkeypatch):
    """The conversation is gone; a bubble belonging to it is a ghost."""
    async def reset(_packet):
        return None

    monkeypatch.setattr(handler, "_handle_context_reset", reset)

    async def scenario():
        handler._sessions.open(handler.conversation_id, model_id="m",
                               stream_id="req-1")
        await handler._dispatch({"type": "context_reset"})
        await handler.wait_for_turns()

    _run(scenario())

    assert len(handler._sessions) == 0


def test_no_session_outlives_its_connection(handler):
    handler._sessions.open("a", model_id="m", stream_id="1")
    handler._sessions.open("b", model_id="m", stream_id="2")

    handler._sessions.close_all("disconnected")

    assert len(handler._sessions) == 0


def test_the_teardown_closes_sessions():
    import inspect
    source = inspect.getsource(WebSocketHandler.handle)

    assert "_sessions.close_all(" in source


# ======================================================
# The turn state stays per-turn
# ======================================================

def test_the_attachment_is_cleared_between_turns(handler, monkeypatch):
    """_turn_session is PER-TURN. Left set, the next unrelated answer
    would be written into the previous bubble."""
    async def quick(_packet):
        return None

    monkeypatch.setattr(handler, "_handle_chat_request", quick)

    async def scenario():
        handler._sessions.open(handler.conversation_id, model_id="m",
                               stream_id="req-1")
        await handler._dispatch({"type": "chat_request", "messages": [
            {"role": "user", "content": "continue"}]})
        await handler.wait_for_turns()

    _run(scenario())

    assert handler._turn_session is None


def test_sessions_are_opt_in_for_the_common_path(handler, monkeypatch):
    """A one-shot question that streams nothing opens no session. The
    common path must not pay for machinery it does not use."""
    async def quick(_packet):
        return None

    monkeypatch.setattr(handler, "_handle_chat_request", quick)

    async def scenario():
        await handler._dispatch({"type": "chat_request", "messages": [
            {"role": "user", "content": "what is the weather in Paris?"}]})
        await handler.wait_for_turns()

    _run(scenario())

    assert len(handler._sessions) == 0
    assert handler._turn_session is None


# ======================================================
# What this is honestly not
# ======================================================

def test_no_provider_offers_a_resumable_session():
    """Reading C, pinned as unreachable. If a provider ever grows a
    session handle this test fails and the docstring above needs
    rewriting -- which is the point of it."""
    import backend.llm.providers as providers

    names = [n for n in dir(providers) if "session" in n.lower()]
    assert names == [], f"a provider now exposes {names}; reading C may be live"


def test_the_session_layer_does_not_depend_on_v2():
    """The plan said it had to. It did not, and coupling them would
    have dragged model resolution into this change."""
    # Anchored on what the module IMPORTS, not on the word appearing
    # anywhere: its own docstring explains at length why it does not
    # use v2, and a text search matched that explanation. The same
    # trap this codebase has fallen into before -- an assertion that
    # passes or fails on a comment about itself.
    import sys

    module = sys.modules[stream_session.__name__]
    imported = {name for name in dir(module)
                if not name.startswith("__")}

    assert "StreamHandle" not in imported
    assert "StreamingEngineV2" not in imported
    assert not any("streaming_engine" in str(getattr(module, name, ""))
                   for name in imported)
