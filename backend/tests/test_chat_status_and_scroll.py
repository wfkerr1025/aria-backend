# backend/tests/test_chat_status_and_scroll.py
#
# The two halves of the chat UX change that live outside the filter:
# what the backend tells the UI it is doing, and when the UI is allowed to
# move the reader.
#
# Both are transport/UI concerns by construction. Nothing here touches the
# orchestrator, the reasoning core, or the tool registry -- the status
# packets are derived from a TurnResult the orchestrator already returns,
# and the scroll rule is a pure function of three numbers.

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import subprocess

import pytest

from backend.core import turn_status
from backend.core.turn_status import IDLE, THINKING, WRITING


REPO = pathlib.Path(__file__).resolve().parents[2]


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw) if isinstance(raw, str) else raw)


@pytest.fixture
def search_tool():
    from backend.core import tool_registry as core_reg

    core_reg.register_tool(
        core_reg.ToolSchema(
            name="web_search", description="test double",
            parameters={"query": {"type": "string", "required": True}},
            permission=core_reg.PERMISSION_NETWORK,
        ),
        lambda query: {"raw": {}, "reply": "MSFT is trading at $412.30 (source: example.com)"},
    )
    try:
        yield
    finally:
        core_reg.register_builtin_tools()


def run_turn(message: str, reply_tokens, monkeypatch):
    """One WebSocket turn with the provider replaced by fixed tokens.

    The streaming engine is stubbed rather than the model, so everything
    between the packet arriving and the tokens going out -- orchestration,
    the answer filter, the status emitter -- is the real code.
    """
    from backend.websocket import handlers as handlers_mod

    ws = FakeWebSocket()
    handler = handlers_mod.WebSocketHandler(ws)

    def fake_stream(request, send_packet):
        send_packet({"type": "stream_start", "modelId": "m", "requestId": 1})
        for token in reply_tokens:
            send_packet({"type": "stream_token", "modelId": "m", "requestId": 1, "token": token})
        send_packet({"type": "stream_end", "modelId": "m", "requestId": 1})

    monkeypatch.setattr(handler.streamer, "stream", fake_stream)

    async def scenario():
        await handler._dispatch({
            "type": "chat_request",
            "payload": {
                "messages": [{"role": "user", "content": message}],
                "conversationId": "status-test",
                "multiTurn": True,
                "skipSafetyCheck": True,
            },
        })
        await handler.wait_for_turns()
        # run_coroutine_threadsafe hands the sends back to this loop from
        # the executor thread; yield once so they land before we look.
        await asyncio.sleep(0.05)
        return ws.sent

    return asyncio.run(scenario())


def statuses(sent):
    return [p["value"] for p in sent if p.get("type") == turn_status.STATUS_PACKET_TYPE]


def streamed_text(sent):
    return "".join(p.get("token", "") for p in sent if p.get("type") == "stream_token")


# ======================================================
# Status sequence
# ======================================================
def test_a_reasoning_turn_reports_thinking_planning_writing_idle(search_tool, monkeypatch):
    """`planning` joined the sequence when the orchestrator gained a callback.

    It sits between thinking and writing because that is when it happens:
    the orchestrator fires it immediately before handing the turn to Engine
    B, whose first act is to build the plan.
    """
    from backend.core.turn_status import PLANNING

    sent = run_turn("how much is MSFT trading at",
                    ["Microsoft is at $412.30, per example.com."], monkeypatch)
    assert statuses(sent) == [THINKING, PLANNING, WRITING, IDLE]


def test_a_turn_with_no_reasoning_never_claims_to_plan(search_tool, monkeypatch):
    from backend.core.turn_status import PLANNING

    sent = run_turn("what model are you running", [], monkeypatch)
    assert PLANNING not in statuses(sent)


def test_writing_carries_what_the_turn_actually_ran(search_tool, monkeypatch):
    """The UI says "searched the web" only when a search really happened.

    tool_runs comes off TurnResult.metadata -- the orchestrator's own
    record -- rather than being guessed from the intent, so the indicator
    cannot claim a lookup that did not occur.
    """
    sent = run_turn("how much is MSFT trading at", ["$412.30"], monkeypatch)
    writing = next(p for p in sent if p.get("value") == WRITING)
    assert "web_search" in writing["tool_runs"]


def test_a_turn_with_no_tools_says_so(monkeypatch):
    sent = run_turn("explain the build pipeline", ["It compiles shaders first."], monkeypatch)
    writing = next(p for p in sent if p.get("value") == WRITING)
    assert "web_search" not in writing["tool_runs"]


def test_the_status_returns_to_idle_after_stream_end(search_tool, monkeypatch):
    sent = run_turn("how much is MSFT trading at", ["$412.30"], monkeypatch)

    kinds = [p.get("type") for p in sent]
    assert statuses(sent)[-1] == IDLE
    assert kinds.index("stream_end") < len(kinds) - 1, "idle must follow stream_end"
    assert kinds[-1] == turn_status.STATUS_PACKET_TYPE


def test_a_turn_answered_without_a_model_still_returns_to_idle(monkeypatch):
    """An indicator left spinning is worse than no indicator."""
    sent = run_turn("what model are you running", [], monkeypatch)
    assert statuses(sent)[-1] == IDLE


def test_every_status_value_is_one_the_ui_knows(search_tool, monkeypatch):
    sent = run_turn("how much is MSFT trading at", ["$412.30"], monkeypatch)
    for value in statuses(sent):
        assert value in turn_status.TURN_STATUSES


def test_an_unknown_status_value_is_refused_at_the_source():
    with pytest.raises(ValueError):
        turn_status.status_packet("pondering")


# ======================================================
# stream_end ordering and the filter, end to end
# ======================================================
def test_stream_end_is_the_last_thing_after_the_answer(search_tool, monkeypatch):
    """No token may follow stream_end, and the answer must precede it."""
    sent = run_turn("how much is MSFT trading at",
                    ["Microsoft ", "is at ", "$412.30."], monkeypatch)

    kinds = [p.get("type") for p in sent]
    end = kinds.index("stream_end")
    assert "stream_token" not in kinds[end + 1:]
    assert "$412.30" in streamed_text(sent)


def test_the_reasoning_preamble_never_reaches_the_socket(search_tool, monkeypatch):
    """The whole point, asserted through the real transport.

    These are the tokens phi-3-mini actually produced; what the client
    receives should be the answer inside them and nothing else.
    """
    sent = run_turn(
        "how much is MSFT trading at",
        ["Solution:\n", "Microsoft is at $412.30 ", "(source: example.com).\n",
         "Instruction:\n", "Now answer this instead:\n"],
        monkeypatch,
    )
    text = streamed_text(sent)

    assert "$412.30" in text
    assert "example.com" in text
    assert "Solution:" not in text
    assert "Instruction:" not in text
    assert "Now answer this instead" not in text


def test_no_empty_token_packets_are_sent(search_tool, monkeypatch):
    """A held-back token must not go out as an empty stream_token.

    The client re-renders its bubble on every one, so an empty token is
    pure churn -- and a run of them is what filtering naively would
    produce.
    """
    sent = run_turn("how much is MSFT trading at",
                    ["Solution:\n", "Microsoft is at $412.30."], monkeypatch)
    tokens = [p for p in sent if p.get("type") == "stream_token"]
    assert tokens, "the answer was filtered away entirely"
    assert all(p["token"] for p in tokens)


# ======================================================
# Scroll policy (webui/components/chat/scroll-policy.js)
# ======================================================
POLICY = REPO / "webui" / "components" / "chat" / "scroll-policy.js"

# The cases travel in an environment variable rather than argv: `node -e`
# shifts positional arguments (the first lands at argv[1], not argv[2]),
# which is easy to get wrong and silently yields "undefined".
NODE_HARNESS = """
const policy = require(%s);
const cases = JSON.parse(process.env.ARIA_SCROLL_CASES);
const out = cases.map(c => policy.shouldFollow(c.metrics, c.state, c.reason));
console.log(JSON.stringify(out));
"""


def run_policy(cases):
    """Exercise the real scroll-policy.js, in node, from the python suite.

    The rule is pure arithmetic on three numbers, so it needs no DOM --
    but it does need to be the actual file the browser loads rather than a
    python re-implementation of it, or the test would be pinning a copy.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")

    env = {**os.environ, "ARIA_SCROLL_CASES": json.dumps(cases)}
    result = subprocess.run(
        [node, "-e", NODE_HARNESS % json.dumps(str(POLICY))],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def at_bottom():
    return {"scrollTop": 900, "scrollHeight": 1000, "clientHeight": 100}


def scrolled_up():
    return {"scrollTop": 200, "scrollHeight": 1000, "clientHeight": 100}


def test_the_policy_file_is_loaded_by_the_page():
    """The test and the browser have to be exercising the same file."""
    index = (REPO / "webui" / "index.html").read_text(encoding="utf-8")
    assert "components/chat/scroll-policy.js" in index


def test_streaming_does_not_scroll_a_reader_who_has_moved_up():
    """The reported behaviour: scrolling up during a long answer, and
    being dragged back down on the next token."""
    [result] = run_policy([
        {"metrics": scrolled_up(), "state": {"pinned": True}, "reason": "token"},
    ])
    assert result["follow"] is False
    assert result["pinned"] is False


def test_streaming_never_scrolls():
    """Changed contract, and the point of the change.

    Following the tail meant the reader always looked at the answer's
    last line and had to scroll UP to read it from the start -- which is
    what "I have to scroll every message" was describing. It was not
    that nothing scrolled; the scroll landed at the wrong end.

    The view is positioned once, at the top of the new message
    (chat.js's scrollToMessageTop), and left alone while the text
    arrives underneath.
    """
    results = run_policy([
        {"metrics": at_bottom(), "state": {"pinned": True}, "reason": "token"},
        {"metrics": scrolled_up(), "state": {"pinned": False}, "reason": "token"},
    ])
    assert [r["follow"] for r in results] == [False, False]


def test_streaming_still_tracks_whether_the_reader_stayed():
    """follow is gone; pinned is not.

    A new message still has to know whether the reader is at the bottom,
    and that is recomputed from the live position on every token -- so
    someone who scrolls back down is picked up by the next message
    without waiting for a new turn.
    """
    results = run_policy([
        {"metrics": scrolled_up(), "state": {"pinned": True}, "reason": "token"},
        {"metrics": at_bottom(), "state": {"pinned": False}, "reason": "token"},
    ])
    assert [r["pinned"] for r in results] == [False, True]


def test_an_answer_arriving_scrolls_only_for_a_reader_at_the_bottom():
    """The new message positions the view; a reader who left keeps it."""
    results = run_policy([
        {"metrics": at_bottom(), "state": {"pinned": True}, "reason": "assistant_message"},
        {"metrics": scrolled_up(), "state": {"pinned": False}, "reason": "assistant_message"},
    ])
    assert [r["follow"] for r in results] == [True, False]


def test_stream_end_does_not_yank_a_reader_back():
    """Now for a reader at the bottom too.

    It used to scroll someone still at the bottom down to the end of the
    finished answer. They are reading it from the top; arriving at its
    last line the moment it finishes is exactly the jump this removes.
    """
    results = run_policy([
        {"metrics": scrolled_up(), "state": {"pinned": False}, "reason": "stream_end"},
        {"metrics": at_bottom(), "state": {"pinned": True}, "reason": "stream_end"},
    ])
    assert [r["follow"] for r in results] == [False, False]


def test_sending_a_message_always_scrolls():
    """The one unambiguous request to be at the bottom."""
    [result] = run_policy([
        {"metrics": scrolled_up(), "state": {"pinned": False}, "reason": "new_message"},
    ])
    assert result["follow"] is True


def test_the_decision_is_stable_across_a_long_response():
    """A growing transcript must not drift back into following.

    scrollHeight climbs with every token while scrollTop stays put, so the
    gap only widens -- the answer has to stay "do not follow" for all of
    it, not flip once the numbers get large.
    """
    cases = [
        {"metrics": {"scrollTop": 200, "scrollHeight": h, "clientHeight": 100},
         "state": {"pinned": False}, "reason": "token"}
        for h in (400, 1000, 5000, 50000)
    ]
    assert all(r["follow"] is False for r in run_policy(cases))


def test_a_hair_from_the_bottom_still_counts_as_the_bottom():
    """Fractional layout values must not read as "the reader scrolled up".

    Asserted on `pinned` rather than `follow`: a token no longer scrolls
    at all, so the thing this protects is the flag the NEXT message
    reads. The property is unchanged -- a reader who has not moved must
    not be treated as one who has.
    """
    [result] = run_policy([
        {"metrics": {"scrollTop": 899.6, "scrollHeight": 1000, "clientHeight": 100},
         "state": {"pinned": True}, "reason": "token"},
    ])
    assert result["pinned"] is True

# ======================================================
# The legacy typing indicator is gone
# ======================================================
# showTyping() set a fixed "ARIA is typing..." the instant the user pressed
# send. It was the only indicator available before the backend described
# what it was doing, and it flashed for a moment before the first status
# packet replaced it with something true. Status packets are the only
# writer now, so what the indicator says always matches the turn.
CHAT_JS = REPO / "webui" / "components" / "chat" / "chat.js"


def _chat_js_code() -> str:
    """chat.js with comments stripped, so prose about the old behaviour
    does not read as the old behaviour."""
    import re

    source = CHAT_JS.read_text(encoding="utf-8")
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("//")
    )


def test_nothing_calls_the_legacy_typing_indicator():
    assert "showTyping(" not in _chat_js_code()


def test_the_legacy_typing_string_is_never_assigned():
    code = _chat_js_code()
    assert "ARIA is typing" not in code


def test_status_packets_are_the_only_writer_of_the_indicator():
    """One writer, so the indicator cannot disagree with the turn."""
    import re

    code = _chat_js_code()
    writers = re.findall(r"typingIndicator\.textContent\s*=", code)
    # _handleStatus sets it; hideTyping clears it on the paths that end a
    # turn without a status packet (an IPC error, a dropped connection).
    assert len(writers) == 2, f"expected 2 writers, found {len(writers)}"
    assert "_handleStatus(packet)" in code
