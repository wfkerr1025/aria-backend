# backend/tests/test_chat_auto_switch.py
#
# The chat-level capability gate: a model too small to hold ARIA's chat
# protocol does not get the turn.
#
# The reported failure is qwen2.5-0.5b, which answers an ordinary
# question by inventing a system prompt and replying to one nobody asked.
# The evidence ladder already keeps an evidence-bearing turn off a model
# like that; ordinary chat had no such floor, so an explicit pin reached
# the provider and came back as noise.
#
# Half of this file is about what the gate must NOT do. Pinning a model
# was implemented, measured and removed once before -- a concrete
# model_id is one the safety gate evaluates, and pinning refused turns on
# a loaded machine that would otherwise have run. So the tests that
# matter most here are the ones asserting the gate stays narrow: it acts
# only on evidence, it never runs ahead of the safety gate, and it never
# crosses the local/cloud boundary.

from __future__ import annotations

import pytest

from backend.core import chat_capability_gate as gate
from backend.core import turn_orchestrator
from backend.core.turn_orchestrator import orchestrate_turn
from backend.core.turn_types import KIND_INFERENCE, KIND_TEXT, SessionState, TurnRequest

WEAK = "qwen2.5-0.5b-instruct-q4_k_m"
CAPABLE = "nemo-12b-q5"
# On this machine, and deliberately: 3.8B, NOT on the evidence allowlist,
# and perfectly able to hold a conversation. It is the model that proves
# the gate uses a floor rather than the allowlist.
MID = "phi-3-mini-4k-instruct-q4"


def turn(text: str, **kw) -> TurnRequest:
    session = kw.pop("session", SessionState(mode="local"))
    return TurnRequest(
        messages=[{"role": "user", "content": text}],
        latest_user_text=text,
        conversation_id="c1",
        session=session,
        **kw,
    )


# ======================================================
# The floor
# ======================================================
def test_the_reported_model_is_below_the_floor():
    assert gate.too_weak_for_chat(WEAK) is True


@pytest.mark.parametrize("model_id", [CAPABLE, MID])
def test_a_model_that_can_hold_a_conversation_is_left_alone(model_id):
    assert gate.too_weak_for_chat(model_id) is False


def test_an_unknown_model_is_left_alone():
    # The important one. This gate OVERRIDES a choice the user made on
    # purpose, so silence is not evidence: a model the registry has never
    # heard of keeps the turn. The first version of this used the
    # evidence allowlist and moved turns off every id it did not
    # recognise, including the whole test corpus.
    assert gate.too_weak_for_chat("some-model-nobody-registered") is False


def test_a_registry_entry_without_a_parameter_count_is_left_alone(monkeypatch):
    monkeypatch.setattr("backend.core.model_registry.get_model",
                        lambda model_id: {"id": model_id})
    assert gate.too_weak_for_chat(WEAK) is False


def test_none_means_the_router_chooses_and_is_never_gated():
    # None is Cloud or Automatic, where a frontier model answers.
    assert gate.too_weak_for_chat(None) is False
    assert gate.ensure_tool_capable(None).model_id is None


def test_the_floor_sits_between_the_two_models_that_set_it():
    # Written down so a later change to the constant has to argue with
    # the two measurements behind it rather than just move a number.
    assert 500_000_000 < gate.CHAT_PARAM_FLOOR <= 3_800_000_000


# ======================================================
# What it does when it fires
# ======================================================
def test_a_weak_model_is_moved_to_one_that_can():
    outcome = gate.ensure_tool_capable(WEAK)

    assert outcome.switched is True
    assert outcome.switched_from == WEAK
    assert outcome.model_id != WEAK
    assert gate.too_weak_for_chat(outcome.model_id) is False


def test_the_warning_names_the_model_it_switched_to():
    outcome = gate.ensure_tool_capable(WEAK)

    assert outcome.warning == (
        "Active model cannot follow ARIA's chat protocol. "
        f"Switching to: {outcome.model_id}."
    )


def test_a_capable_model_produces_no_warning_at_all():
    # Silence unless it acts. A notice on every turn would train the user
    # to ignore the one turn where it matters.
    outcome = gate.ensure_tool_capable(CAPABLE)

    assert outcome.switched is False
    assert outcome.warning is None
    assert outcome.refused is False


def test_nothing_installed_that_clears_the_floor_refuses_the_turn(monkeypatch):
    from backend.core import complexity_router as cr

    monkeypatch.setattr(cr, "_is_installed", lambda model_id: False)

    outcome = gate.ensure_tool_capable(WEAK)

    assert outcome.refused is True
    assert outcome.switched is False


def test_the_replacement_is_judged_by_the_same_rule_that_condemned_the_original(monkeypatch):
    # A gate that decides with one rule and repairs with another can
    # recommend a model it would then reject. Everything under the floor
    # is unavailable here, so the only honest answer is a refusal.
    monkeypatch.setattr(gate, "too_weak_for_chat", lambda model_id: True)

    assert gate.ensure_tool_capable(WEAK).refused is True


def test_it_never_crosses_the_local_cloud_boundary(monkeypatch):
    # Absolute mode separation is checked, not assumed. If the only
    # replacement available would violate it, the gate refuses rather
    # than putting a local model in front of a cloud turn.
    monkeypatch.setattr(
        "backend.core.chat_capability_gate.model_violates_mode_separation",
        lambda model_id, mode: True,
    )

    assert gate.ensure_tool_capable(WEAK, mode="cloud").refused is True


# ======================================================
# Wired into the turn
# ======================================================
@pytest.fixture
def offline(monkeypatch):
    """No live search or weather; this file asserts routing only."""
    monkeypatch.setattr(turn_orchestrator, "run_search_tool", lambda query: {"results": []})
    monkeypatch.setattr(turn_orchestrator, "format_search_reply", lambda result: "results")


def test_an_ordinary_turn_on_a_weak_model_is_moved(offline):
    result = orchestrate_turn(
        turn("explain the build pipeline", requested_model_id=WEAK),
        default_local_model=lambda: WEAK,
    )

    assert result.kind == KIND_INFERENCE
    assert result.model_id != WEAK
    # The provider request carries the substitute too, not just the
    # reported model_id. Reporting one model and loading another is the
    # failure this assertion exists to catch.
    assert result.inference_request.model_id == result.model_id


def test_the_turn_says_it_switched(offline):
    result = orchestrate_turn(
        turn("explain the build pipeline", requested_model_id=WEAK),
        default_local_model=lambda: WEAK,
    )

    assert len(result.notices) == 1
    notice = result.notices[0]
    assert "cannot follow ARIA's chat protocol" in notice["message"]
    assert notice["model_id"] == result.model_id
    # Shaped like a warning_event, because that packet and its banner
    # already exist; the transport sends it verbatim.
    assert set(notice) == {"id", "level", "message", "model_id"}


def test_the_switch_is_in_the_telemetry(offline):
    result = orchestrate_turn(
        turn("explain the build pipeline", requested_model_id=WEAK),
        default_local_model=lambda: WEAK,
    )

    events = [record["event"] for record in result.telemetry]
    assert "chat_capability_switch" in events


def test_an_ordinary_turn_on_a_capable_model_carries_no_notice(offline):
    result = orchestrate_turn(
        turn("explain the build pipeline", requested_model_id=CAPABLE),
        default_local_model=lambda: CAPABLE,
    )

    assert result.notices == []


def test_nothing_capable_installed_answers_instead_of_generating(offline, monkeypatch):
    from backend.core import complexity_router as cr

    monkeypatch.setattr(cr, "_is_installed", lambda model_id: False)

    result = orchestrate_turn(
        turn("explain the build pipeline", requested_model_id=WEAK),
        default_local_model=lambda: WEAK,
    )

    # Answered, not generated. Asking the incapable model anyway produces
    # text that looks like an answer and is not.
    assert result.kind == KIND_TEXT
    assert result.text == "I'm unable to process that request with the current model."
    assert result.inference_request is None


# ======================================================
# What it deliberately does not do
# ======================================================
def test_the_gate_runs_before_the_safety_gate_and_not_instead_of_it():
    import inspect

    source = inspect.getsource(turn_orchestrator.orchestrate_turn)
    code = " ".join(
        line for line in source.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )

    # Order, in the code that runs rather than in the comments: the
    # substituted model must reach _evaluate_safety, so the gate
    # evaluates the model that will actually be loaded.
    assert code.index("ensure_tool_capable") < code.index("_evaluate_safety")


def test_the_gate_switches_nothing_itself():
    import ast
    import inspect

    # Parsed, not grepped. Stripping comment LINES is not enough -- this
    # module's docstring explains at length why it does not call
    # switch_model(), and a substring check reads that explanation as the
    # violation. That mistake has now been made in three separate source
    # assertions in this codebase, so this one looks at called names in
    # the syntax tree, where prose cannot reach.
    tree = ast.parse(inspect.getsource(gate))
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called.add(func.id if isinstance(func, ast.Name)
                       else getattr(func, "attr", ""))

    # "Do NOT implement unsafe switch_model() before safety gate." This
    # module returns a value and performs nothing -- no load, no
    # persisted override, no set_active_model. The orchestrator uses the
    # returned id, and the safety gate then evaluates it like any other.
    for forbidden in ("set_active_model", "switch_model", "load_model",
                      "set_explicit_model_override", "set_mode"):
        assert forbidden not in called, \
            f"the gate calls {forbidden}() instead of describing a choice"


def test_a_safety_refusal_still_reaches_the_user_after_a_switch(offline, monkeypatch):
    # The point of running before the gate rather than around it: a
    # substituted model that the safety gate refuses produces the
    # ordinary safety_warning, with its ordinary "Proceed Anyway".
    # A real SafetyDecision rather than a stub, and a fixed one rather
    # than the live gate: a test that puts a 12B in front of the real
    # resource check passes or fails with whatever else is running on the
    # machine, which this suite has already been bitten by once.
    from backend.core.safety_manager import SafetyDecision

    refusal = SafetyDecision(
        safe_to_run=True, requires_warning=True, severity="caution",
        message="Not enough free RAM.", profile=None, snapshot=None,
    )
    monkeypatch.setattr(
        turn_orchestrator, "_evaluate_safety",
        lambda model_id, request, suggester: (
            refusal, {"id": model_id}, {"type": "safety_warning", "model_id": model_id},
        ),
    )

    result = orchestrate_turn(
        turn("explain the build pipeline", requested_model_id=WEAK),
        default_local_model=lambda: WEAK,
    )

    assert result.kind == "safety_warning"
    # And the warning is about the model that would actually have been
    # loaded, not the one the user typed.
    assert result.warning["model_id"] == result.model_id != WEAK
    # The switch is still reported, so the user can see why the warning
    # names a model they did not choose.
    assert result.notices


# ======================================================
# Where a redirected turn lands
#
# The gate used to send every redirected turn to the strongest installed
# model, which is correct and needlessly slow: "hello" on the 0.5B became
# "hello" on the 12B. It now asks the routing layer what the turn is FOR
# and lands it on the model for that job.
#
# The floor itself is untouched. Which model a redirect goes to is a
# preference; whether a redirect happens at all is still the parameter
# floor and nothing else.
# ======================================================
def test_a_chat_turn_redirected_off_the_smallest_model_lands_on_the_chat_model(offline):
    from backend.config.model_roles import installed_model_for

    result = orchestrate_turn(
        turn("hello, how are you", requested_model_id=WEAK),
        default_local_model=lambda: WEAK,
    )

    assert result.kind == KIND_INFERENCE
    assert result.model_id == installed_model_for("phi-3-mini-4k-instruct-q4")
    assert result.inference_request.model_id == result.model_id


def test_a_tool_turn_redirected_off_the_smallest_model_lands_on_the_tool_model(offline):
    from backend.config.model_roles import installed_model_for

    result = orchestrate_turn(
        turn("edit the file src/main.py and add logging", requested_model_id=WEAK),
        default_local_model=lambda: WEAK,
    )

    assert result.kind == KIND_INFERENCE
    assert result.model_id == installed_model_for("mistral-7b")


def test_a_redirect_still_emits_the_notice_whichever_model_it_lands_on(offline):
    for text in ("hello, how are you", "edit the file src/main.py"):
        result = orchestrate_turn(
            turn(text, requested_model_id=WEAK), default_local_model=lambda: WEAK,
        )

        assert len(result.notices) == 1, f"no notice for {text!r}"
        notice = result.notices[0]
        assert "cannot follow ARIA's chat protocol" in notice["message"]
        assert notice["model_id"] == result.model_id
        assert set(notice) == {"id", "level", "message", "model_id"}


def test_the_routing_layer_does_not_shadow_the_gate(offline):
    # The bug this pins. The router knows the 0.5B's role says it cannot
    # chat, and for one revision it acted on that -- moving the model
    # aside before the gate ever saw it. The redirect still happened, so
    # nothing looked broken, but the gate's notice and telemetry both
    # disappeared and the floor stopped being the authority it is.
    result = orchestrate_turn(
        turn("hello", requested_model_id=WEAK), default_local_model=lambda: WEAK,
    )

    events = [record["event"] for record in result.telemetry]
    assert "chat_capability_switch" in events


def test_an_unknown_pinned_model_is_left_where_the_user_put_it(offline):
    # Neither layer touches it: the role table defaults an unknown model
    # to can_chat, and the floor has no parameter count to judge it by.
    # A model the user installed on purpose keeps its turn.
    result = orchestrate_turn(
        turn("hello", requested_model_id="some-model-nobody-registered"),
        default_local_model=lambda: CAPABLE,
    )

    assert result.model_id == "some-model-nobody-registered"
    assert result.notices == []


@pytest.mark.parametrize("turn_kind,family", [
    ("chat", "phi-3-mini-4k-instruct-q4"),
    ("tools", "mistral-7b"),
    ("heavy_reasoning", "mistral-nemo-12b"),
])
def test_the_gate_honours_the_turn_kind_directly(turn_kind, family):
    from backend.config.model_roles import installed_model_for

    outcome = gate.ensure_tool_capable(WEAK, turn_kind=turn_kind)

    assert outcome.model_id == installed_model_for(family)


def test_an_unroutable_turn_kind_falls_back_rather_than_refusing():
    # A preference, not an authority. Not knowing where to send a turn is
    # a reason to pick the ladder's answer, never a reason to tell the
    # user their turn cannot run.
    outcome = gate.ensure_tool_capable(WEAK, turn_kind="something-invented-later")

    assert outcome.refused is False
    assert outcome.switched is True
    assert gate.too_weak_for_chat(outcome.model_id) is False


def test_a_preferred_replacement_that_is_not_installed_falls_back(monkeypatch):
    monkeypatch.setattr(
        "backend.config.model_roles.installed_model_for", lambda family: None)

    outcome = gate.ensure_tool_capable(WEAK, turn_kind="chat")

    assert outcome.switched is True
    assert outcome.model_id is not None
