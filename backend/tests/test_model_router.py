# backend/tests/test_model_router.py
#
# Which model takes a turn, decided from what the turn is going to do.
#
# Two things this file is really protecting, both of which were broken in
# the first version and both of which the tests found:
#
#   Absolute mode separation. The router read requested_model_id
#   directly, which meant a pin that _resolve_model_id had ALREADY
#   rejected for crossing the local/cloud boundary was put straight back
#   -- by the module whose docstring says it must never name a model in
#   the wrong registry.
#
#   The capability gate still fires. The router overrode a pin whose role
#   said it could not chat, so the 0.5B was moved aside here and
#   chat_capability_gate never ran: no notice, no telemetry, and a gate
#   that exists for exactly one case never seeing it. Two layers both
#   answering "may this model chat" is one authority too many.
#
# So the division asserted throughout is: the router picks a model for an
# UNPINNED turn; the gate decides whether the model in hand may chat.

from __future__ import annotations

import pytest

from backend.chat import model_router as router
from backend.chat.model_router import (
    TURN_CHAT,
    TURN_CLASSIFICATION,
    TURN_HEAVY,
    TURN_TOOLS,
    select_model_for_turn,
)
from backend.config.model_roles import installed_model_for
from backend.core.turn_types import SessionState, TurnRequest

ROUTER_MODEL = installed_model_for("qwen2.5-0.5b")
CHAT_MODEL = installed_model_for("phi-3-mini-4k-instruct-q4")
TOOL_MODEL = installed_model_for("mistral-7b")
HEAVY_MODEL = installed_model_for("mistral-nemo-12b")


def turn(text: str, **kw) -> TurnRequest:
    messages = kw.pop("messages", [{"role": "user", "content": text}])
    return TurnRequest(
        messages=messages,
        latest_user_text=text,
        conversation_id="c1",
        session=kw.pop("session", SessionState(mode="local")),
        **kw,
    )


# ======================================================
# 1. Classification -- the 0.5B's one job
# ======================================================
def test_a_classification_turn_uses_the_smallest_model():
    choice = select_model_for_turn(turn("anything at all"), classification_only=True)

    assert choice.turn_kind == TURN_CLASSIFICATION
    assert choice.model_id == ROUTER_MODEL


def test_classification_wins_over_everything_the_text_says():
    # The verdict is a single token nobody reads. What the sentence looks
    # like is irrelevant -- a classification turn is defined by its
    # caller, not by its content.
    choice = select_model_for_turn(
        turn("think carefully and edit the file src/main.py"), classification_only=True)

    assert choice.model_id == ROUTER_MODEL


def test_the_smallest_model_is_used_for_nothing_else():
    for text in ("hello", "edit the file a.py", "think carefully about this"):
        assert select_model_for_turn(turn(text)).model_id != ROUTER_MODEL


# ======================================================
# 2. Tools
# ======================================================
@pytest.mark.parametrize("text", [
    "edit the file src/main.py and add logging",
    "create a file called notes.md",
    "run the tests",
    "search the web for the pytest release notes",
    "apply the change to the workspace",
])
def test_a_tool_turn_uses_the_tool_model(text):
    choice = select_model_for_turn(turn(text))

    assert choice.turn_kind == TURN_TOOLS
    assert choice.model_id == TOOL_MODEL


def test_a_tool_turn_that_is_also_deep_work_escalates():
    # The tool plan is the harder half, so this escalates rather than
    # choosing between the two.
    choice = select_model_for_turn(
        turn("think carefully, then refactor the architecture and edit the file"))

    assert choice.turn_kind == TURN_HEAVY
    assert choice.model_id == HEAVY_MODEL
    assert choice.escalated is True


# ======================================================
# 3. Heavy reasoning
# ======================================================
@pytest.mark.parametrize("text", [
    "think carefully about why this fails",
    "walk me through the architecture in depth",
    "what is the root cause of the shader mismatch",
])
def test_asking_for_depth_reaches_the_heaviest_model(text):
    choice = select_model_for_turn(turn(text))

    assert choice.turn_kind == TURN_HEAVY
    assert choice.model_id == HEAVY_MODEL


def test_a_long_conversation_escalates_whatever_it_says():
    # phi-3's window is 4k tokens. A turn approaching it has to move
    # regardless of how simple the last question sounded.
    long_history = [
        {"role": "user", "content": "x" * (router.HEAVY_CONTEXT_CHARS + 10)},
        {"role": "user", "content": "ok"},
    ]
    choice = select_model_for_turn(turn("ok", messages=long_history))

    assert choice.turn_kind == TURN_HEAVY
    assert choice.model_id == HEAVY_MODEL


def test_a_short_conversation_does_not_escalate():
    assert select_model_for_turn(turn("hello")).turn_kind == TURN_CHAT


# ======================================================
# 4. Ordinary chat
# ======================================================
@pytest.mark.parametrize("text", [
    "hello", "hi there, how are you", "thanks, that helped",
    "what did you mean by that",
])
def test_ordinary_chat_uses_the_chat_model(text):
    choice = select_model_for_turn(turn(text))

    assert choice.turn_kind == TURN_CHAT
    assert choice.model_id == CHAT_MODEL


def test_chat_does_not_wake_the_twelve_b():
    # The point of the whole layer, from the user's side: "hello" used to
    # load a 12B because it was the session default.
    assert select_model_for_turn(turn("hello")).model_id != HEAVY_MODEL


# ======================================================
# Pins
# ======================================================
def test_a_pinned_model_is_returned_unchanged():
    choice = select_model_for_turn(turn("hello", requested_model_id="mistral-7b-q4km"))

    assert choice.model_id == "mistral-7b-q4km"
    assert choice.pinned is True


def test_a_pin_that_cannot_chat_is_left_for_the_gate():
    # NOT overridden here. chat_capability_gate is the one authority on
    # "may this model chat", and it redirects this a few lines later in
    # the orchestrator with its own notice. Overriding it here means the
    # gate never fires for the single case it was written for.
    weak = "qwen2.5-0.5b-instruct-q4_k_m"
    choice = select_model_for_turn(turn("hello", requested_model_id=weak))

    assert choice.model_id == weak
    assert choice.pinned is True


def test_a_pin_that_cannot_use_tools_is_still_honoured():
    # phi-3 has can_tools False and the user asked for it anyway. Their
    # choice: the supervisor repairs a malformed action block rather than
    # the router quietly moving them off the model they picked.
    choice = select_model_for_turn(
        turn("edit the file a.py", requested_model_id="phi-3-mini-4k-instruct-q4"))

    assert choice.model_id == "phi-3-mini-4k-instruct-q4"


def test_a_session_pin_counts_as_a_pin():
    session = SessionState(mode="local", explicit_model_override="mistral-7b-q4km")
    choice = select_model_for_turn(turn("hello", session=session))

    assert choice.model_id == "mistral-7b-q4km"
    assert choice.pinned is True


def test_an_explicit_no_pin_is_not_re_derived_from_the_request():
    # The bug this parameter exists for. _resolve_model_id drops a pin
    # that violates mode separation and tells the router there is none;
    # reading requested_model_id again would put the rejected model
    # straight back.
    choice = select_model_for_turn(
        turn("hello", requested_model_id="gpt-4o"), pin=None)

    assert choice.model_id == CHAT_MODEL
    assert choice.pinned is False


def test_the_orchestrator_passes_the_resolved_pin():
    import inspect

    from backend.core import turn_orchestrator

    source = inspect.getsource(turn_orchestrator.orchestrate_turn)
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#"))

    # If this call ever stops passing `pin`, the router falls back to
    # reading the request and mode separation is bypassed again.
    assert "pin=model_id if explicit else None" in code


# ======================================================
# Mode separation
# ======================================================
@pytest.mark.parametrize("mode", ["cloud", "auto"])
def test_a_non_local_turn_never_names_a_local_model(mode):
    choice = select_model_for_turn(turn("hello", session=SessionState(mode=mode)))

    # None is "ProviderRouter chooses", which is what the rest of this
    # codebase means by it. Naming a local model here would break the
    # boundary while looking like an optimisation.
    assert choice.model_id is None


def test_mode_separation_is_checked_before_anything_else():
    # Including for a classification turn, which would otherwise reach
    # for a local 0.5B in Cloud Mode.
    choice = select_model_for_turn(
        turn("hello", session=SessionState(mode="cloud")), classification_only=True)

    assert choice.model_id is None


# ======================================================
# Missing models
# ======================================================
def test_a_missing_ideal_model_defers_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(router.model_roles, "installed_model_for", lambda family: None)

    choice = select_model_for_turn(turn("hello"))

    # None, not a name. Handing back a model that is not installed sends
    # the caller to a load that cannot succeed, and does it while looking
    # exactly like success.
    assert choice.model_id is None


def test_the_orchestrator_keeps_its_own_answer_when_the_router_defers():
    import inspect

    from backend.core import turn_orchestrator

    source = inspect.getsource(turn_orchestrator.orchestrate_turn)
    assert "routing.model_id is not None" in source


# ======================================================
# It decides, it does not act
# ======================================================
def test_the_router_loads_nothing():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(router))
    called = {
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        for node in ast.walk(tree) if isinstance(node, ast.Call)
    }

    for forbidden in ("set_active_model", "load_model", "make_generator",
                      "evaluate_safety", "set_explicit_model_override"):
        assert forbidden not in called, f"the router calls {forbidden}()"


def test_every_choice_explains_itself():
    for text in ("hello", "edit the file a.py", "think carefully about this"):
        assert select_model_for_turn(turn(text)).reason
