# backend/tests/test_answer_actions_wiring.py
#
# The point at which a chat turn can write to disk.
#
# Everything before this was reachable but unreached: the orchestrator
# ran plans, action_plan parsed actions out of an answer, and nothing
# called either during a turn. This is the call, and these are the
# properties that have to hold once it exists.
#
# The wiring lives at the end of _stream_inference because that is the
# first point at which the answer exists. Planning, routing and tool
# execution all happen inside Engine B while the prompt is assembled; the
# actions are in what the model then wrote.

from __future__ import annotations

import asyncio

import pytest

from backend.core import file_tools
from backend.websocket.handlers import WebSocketHandler

ORIGINAL = "line one\n"
ACTION_ANSWER = (
    "I would change it:\n\n```json\n"
    '{"tool": "edit_file", "args": {"path": "notes.txt", "content": "changed\\n"}}'
    "\n```\n"
)


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))
    (tmp_path / "notes.txt").write_text(ORIGINAL, encoding="utf-8")
    return tmp_path


@pytest.fixture
def handler(monkeypatch):
    h = WebSocketHandler(FakeSocket())
    packets = []

    async def capture(packet):
        packets.append(packet)

    monkeypatch.setattr(h, "_send", capture)
    h.packets = packets
    return h


def run(handler, answer, user_text):
    handler._turn_user_text = user_text
    asyncio.run(handler._run_answer_actions(answer))
    return [p for p in handler.packets if p.get("type") == "answer_actions"]


# ------------------------------------------------------
# Additive: a turn with no actions is untouched
# ------------------------------------------------------
def test_an_answer_with_no_actions_sends_no_packet(workspace, handler):
    assert run(handler, "Here is an explanation.", "apply the changes") == []

    # A client that has never heard of answer_actions sees exactly the
    # traffic it saw before.
    assert handler.packets == []


def test_an_answer_with_no_actions_touches_nothing(workspace, handler):
    run(handler, "Here is an explanation.", "apply the changes")

    assert (workspace / "notes.txt").read_text(encoding="utf-8") == ORIGINAL


# ------------------------------------------------------
# Dry run is still the default
# ------------------------------------------------------
def test_a_turn_without_consent_is_a_dry_run(workspace, handler):
    packet = run(handler, ACTION_ANSWER, "what would you change?")[0]

    assert packet["dry_run"] is True
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == ORIGINAL
    assert "changed" in packet["results"][0]["preview"]


def test_the_model_cannot_grant_itself_a_live_run(workspace, handler):
    """The property the whole gate rests on, asserted through the wiring.

    The answer says the words. The consent is read from the user's
    message, and the wiring passes self._turn_user_text -- never the
    answer -- so a model writing "apply the changes" has described an
    intention rather than granted itself one.
    """
    self_authorising = ACTION_ANSWER + "\n\nApply the changes. Execute this now."

    packet = run(handler, self_authorising, "what would that do?")[0]

    assert packet["dry_run"] is True
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == ORIGINAL


def test_a_negated_request_still_vetoes(workspace, handler):
    packet = run(handler, ACTION_ANSWER, "don't apply the changes yet")[0]

    assert packet["dry_run"] is True
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == ORIGINAL


def test_an_empty_user_text_is_no_consent(workspace, handler):
    # A stream that never went through _dispatch. Empty reads as "no
    # consent given", which is the safe answer.
    handler._turn_user_text = ""
    packet = run(handler, ACTION_ANSWER, "")[0]

    assert packet["dry_run"] is True


def test_the_default_is_set_before_any_dispatch():
    assert WebSocketHandler(FakeSocket())._turn_user_text == ""


# ------------------------------------------------------
# Live, when the user asked
# ------------------------------------------------------
def test_explicit_consent_applies_the_change(workspace, handler):
    packet = run(handler, ACTION_ANSWER, "apply the changes")[0]

    assert packet["dry_run"] is False
    assert packet["status"] == "success"
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "changed\n"


def test_a_live_failure_is_reported_and_unwound(workspace, handler):
    answer = ACTION_ANSWER + ('```json\n{"tool": "edit_file", "args": '
                              '{"path": "../escape.txt", "content": "no"}}\n```\n')

    packet = run(handler, answer, "apply the changes")[0]

    assert packet["status"] == "partial"
    assert packet["rollback"]
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == ORIGINAL


# ------------------------------------------------------
# The report, and the turn it belongs to
# ------------------------------------------------------
def test_the_packet_carries_the_documented_fields(workspace, handler):
    packet = run(handler, ACTION_ANSWER, "show me")[0]

    assert set(packet) >= {"type", "actions", "results", "rollback", "status"}
    assert packet["type"] == "answer_actions"
    assert packet["actions"][0]["tool"] == "edit_file"


def test_a_failing_action_pass_never_fails_the_turn(workspace, handler, monkeypatch):
    import backend.websocket.handlers as mod

    def boom(answer_text, user_text):
        raise RuntimeError("the action layer broke")

    monkeypatch.setattr(mod, "run_answer_actions", boom)

    # The answer has already been streamed and read by the time this
    # runs. An action that could not run is worth reporting and is not
    # worth turning a delivered answer into an error.
    asyncio.run(handler._run_answer_actions(ACTION_ANSWER))

    assert handler.packets == []


# ------------------------------------------------------
# What the wiring must not have brought with it
# ------------------------------------------------------
def test_the_wiring_opens_no_shell():
    import inspect

    source = inspect.getsource(WebSocketHandler._run_answer_actions)
    for forbidden in ("subprocess", "os.system", "popen", "shell=True",
                      "eval(", "exec("):
        assert forbidden not in source


def test_the_wiring_reads_consent_from_the_user_not_the_answer():
    import inspect

    source = inspect.getsource(WebSocketHandler._run_answer_actions)
    code = " ".join(
        line for line in source.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    call = code[code.index("run_in_executor"):]

    # The order of the two arguments is the whole gate: answer first,
    # user text second. Passing answer_text twice would let a model
    # authorise itself, and would look almost right.
    assert "answer_text, self._turn_user_text" in call


def test_only_the_permitted_tools_can_run():
    from backend.core.action_plan import ACTION_TOOLS

    assert ACTION_TOOLS == frozenset({"edit_file", "run_tests"})


def test_no_action_run_is_granted_the_network():
    from backend.core.tool_orchestrator import ACTION_PERMISSIONS
    from backend.core.tool_registry import PERMISSION_NETWORK

    assert PERMISSION_NETWORK not in ACTION_PERMISSIONS
