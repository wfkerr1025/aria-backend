# backend/tests/test_gate_and_pins.py
#
# The 3B floor, and what happens to a model the user pinned themselves.
#
# Two layers know something about which model should run a turn, and
# keeping them from fighting is most of what this file checks:
#
#   model_router  picks a model for an UNPINNED turn, from what the turn
#                 is going to do
#   the gate      decides whether the model in hand may chat at all, by
#                 parameter count, and redirects if not
#
# The first version had the router overriding pins whose role said they
# could not chat. The redirect still happened, so nothing looked broken
# -- and the gate never fired, its notice was never emitted, and its
# telemetry event vanished. Two layers answering "may this model chat" is
# one authority too many.

from __future__ import annotations

import pytest

from backend.chat.model_router import select_model_for_turn
from backend.config.model_roles import installed_model_for
from backend.core import chat_capability_gate as gate
from backend.core import turn_orchestrator
from backend.core.turn_orchestrator import orchestrate_turn
from backend.core.turn_types import KIND_INFERENCE, SessionState, TurnRequest

WEAK = "qwen2.5-0.5b-instruct-q4_k_m"
CHAT = installed_model_for("phi-3-mini-4k-instruct-q4")
TOOL = installed_model_for("mistral-7b")
HEAVY = installed_model_for("mistral-nemo-12b")


def turn(text: str, **kw) -> TurnRequest:
    return TurnRequest(
        messages=[{"role": "user", "content": text}],
        latest_user_text=text,
        conversation_id="c1",
        session=kw.pop("session", SessionState(mode="local")),
        **kw,
    )


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(turn_orchestrator, "run_search_tool", lambda q: {"results": []})
    monkeypatch.setattr(turn_orchestrator, "format_search_reply", lambda r: "results")


# ======================================================
# The floor redirects the 0.5B and nothing else
# ======================================================
def test_the_floor_is_three_billion():
    assert gate.CHAT_PARAM_FLOOR == 3_000_000_000


@pytest.mark.parametrize("model_id,redirected", [
    (WEAK, True),
    ("phi-3-mini-4k-instruct-q4", False),
    ("mistral-7b-q4km", False),
    ("nemo-12b-q5", False),
    ("some-model-nobody-registered", False),
    (None, False),
])
def test_only_the_smallest_model_is_redirected(model_id, redirected):
    assert gate.too_weak_for_chat(model_id) is redirected


def test_an_unknown_model_keeps_its_turn():
    # The gate OVERRIDES a choice the user made on purpose, so silence is
    # not evidence. A model the registry has never heard of was installed
    # deliberately and keeps the turn.
    assert gate.ensure_tool_capable("brand-new-model").switched is False


# ======================================================
# Pins
# ======================================================
def test_the_router_hands_a_pin_straight_back():
    # Including one it knows cannot chat. The gate is the authority on
    # that, and overriding here means the gate never runs.
    assert select_model_for_turn(turn("hello", requested_model_id=WEAK)).model_id == WEAK


def test_a_pin_to_the_smallest_model_is_redirected_for_chat(offline):
    result = orchestrate_turn(turn("hello there", requested_model_id=WEAK),
                              default_local_model=lambda: WEAK)

    assert result.kind == KIND_INFERENCE
    assert result.model_id == CHAT


def test_a_pin_to_the_smallest_model_is_redirected_for_tools(offline):
    result = orchestrate_turn(turn("edit main.py", requested_model_id=WEAK),
                              default_local_model=lambda: WEAK)

    assert result.model_id == TOOL


@pytest.mark.parametrize("text", ["hello there", "edit main.py"])
def test_a_redirect_always_says_so(offline, text):
    result = orchestrate_turn(turn(text, requested_model_id=WEAK),
                              default_local_model=lambda: WEAK)

    assert len(result.notices) == 1
    notice = result.notices[0]
    assert "cannot follow" in notice["message"]
    assert "chat protocol" in notice["message"]
    assert notice["model_id"] == result.model_id
    assert set(notice) == {"id", "level", "message", "model_id"}

    events = [record["event"] for record in result.telemetry]
    assert "chat_capability_switch" in events


@pytest.mark.parametrize("pinned", ["phi-3-mini-4k-instruct-q4",
                                    "mistral-7b-q4km", "nemo-12b-q5"])
def test_a_capable_pin_is_honoured_silently(offline, pinned):
    result = orchestrate_turn(turn("hello there", requested_model_id=pinned),
                              default_local_model=lambda: pinned)

    assert result.model_id == pinned
    assert result.notices == []


def test_a_pin_that_cannot_use_tools_is_moved_and_the_user_is_told(offline):
    """This reverses an earlier decision, on evidence.

    It used to assert the pin was kept, reasoning that "the supervisor
    repairs a malformed action block; the router does not quietly move
    them off the model they picked". Respecting a pin is right, and the
    premise turned out to be false: there was no malformed block to
    repair.

    Measured live. Asked for a player_inventory.cs with a pinned
    phi-3-mini, it produced two complete C# implementations, several
    hundred tokens each, and not one action block. Nothing was staged and
    nothing was created. The user asked for a file and got an essay about
    a file, twice, with no indication anything had gone wrong.

    So the choice is not "their model" versus "our model". It is a turn
    that runs and one that silently does nothing -- and the gate's own
    rule already covers that case: it moves a turn off a model that
    cannot do the job, and it says so. The notice is what keeps this
    from being the quiet override the old test was guarding against.
    """
    result = orchestrate_turn(
        turn("edit main.py", requested_model_id="phi-3-mini-4k-instruct-q4"),
        default_local_model=lambda: "phi-3-mini-4k-instruct-q4")

    assert result.model_id != "phi-3-mini-4k-instruct-q4"
    assert result.notices, "moving a pinned model must never be silent"
    assert result.notices[0]["model_id"] == result.model_id


def test_a_pin_that_cannot_use_tools_is_kept_for_a_chat_turn(offline):
    """The reversal is scoped to tool turns. phi-3-mini converses fine,
    and nothing about this change may take an ordinary chat turn off the
    model the user chose."""
    result = orchestrate_turn(
        turn("what do you think about inventory systems",
             requested_model_id="phi-3-mini-4k-instruct-q4"),
        default_local_model=lambda: "phi-3-mini-4k-instruct-q4")

    assert result.model_id == "phi-3-mini-4k-instruct-q4"
    assert result.notices == []


def test_a_session_pin_behaves_like_a_request_pin(offline):
    session = SessionState(mode="local", explicit_model_override=WEAK)

    result = orchestrate_turn(turn("hello there", session=session),
                              default_local_model=lambda: WEAK)

    assert result.model_id == CHAT
    assert result.notices


# ======================================================
# The router does not shadow the gate, or resurrect a rejected pin
# ======================================================
def test_the_gate_fires_even_though_the_router_knows_the_role():
    from backend.config.model_roles import can_chat

    # The router HAS the information -- the role table says the 0.5B
    # cannot chat -- and deliberately does not act on it.
    assert can_chat(WEAK) is False
    assert select_model_for_turn(turn("hello", requested_model_id=WEAK)).model_id == WEAK
    assert gate.ensure_tool_capable(WEAK).switched is True


def test_a_pin_rejected_for_mode_separation_is_not_put_back():
    # _resolve_model_id drops a pin that crosses the local/cloud boundary
    # and tells the router there is none. Reading requested_model_id
    # again would reinstate it -- by the module whose docstring says it
    # must never name a model in the wrong registry.
    choice = select_model_for_turn(turn("hello", requested_model_id="gpt-4o"), pin=None)

    assert choice.model_id == CHAT


@pytest.mark.parametrize("mode", ["cloud", "auto"])
def test_a_non_local_turn_names_no_local_model(mode):
    assert select_model_for_turn(
        turn("hello", session=SessionState(mode=mode))).model_id is None


def test_the_gate_runs_before_the_safety_gate():
    import inspect

    source = inspect.getsource(turn_orchestrator.orchestrate_turn)
    code = " ".join(line for line in source.splitlines()
                    if line.strip() and not line.strip().startswith("#"))

    # The substituted model must be the one _evaluate_safety sees, or the
    # gate would be evaluating a model that never loads.
    assert code.index("ensure_tool_capable") < code.index("_evaluate_safety")


def test_the_gate_performs_no_switch_of_its_own():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(gate))
    called = {
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        for node in ast.walk(tree) if isinstance(node, ast.Call)
    }

    for forbidden in ("set_active_model", "switch_model", "load_model",
                      "set_explicit_model_override"):
        assert forbidden not in called


# ======================================================
# The sandbox, which none of the above may widen
# ======================================================
@pytest.mark.parametrize("bad", [
    "../outside.txt", "/etc/passwd", "..\\..\\evil.txt",
])
def test_the_workspace_boundary_holds_for_edits(tmp_path, monkeypatch, bad):
    from backend.core import file_tools

    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))

    with pytest.raises(Exception):
        file_tools.edit_file(bad, "x", confirm=True)


@pytest.mark.parametrize("bad", [
    "../outside.txt", "/etc/passwd", "..\\..\\evil.txt",
])
def test_the_workspace_boundary_holds_for_operations(tmp_path, monkeypatch, bad):
    from backend.core import file_tools, fs_plan

    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))

    # The five new operations go through the same resolve_in_workspace as
    # every write. A second sandbox would be a second opinion.
    for op in fs_plan.FILE_OPERATIONS:
        with pytest.raises(fs_plan.PlanError):
            fs_plan.stage_operation(op, bad, "elsewhere.txt")


# ======================================================
# The tool floor
# ======================================================
#
# There was a floor for CHAT and none for TOOLS.
#
# Measured live: a turn asking for a player_inventory.cs ran on
# phi-3-mini, which is 3.8B and so clears CHAT_PARAM_FLOOR. It produced
# two complete C# implementations, several hundred tokens each, and not
# one action block. The user asked for a file and got an essay about a
# file, twice.
#
# complexity_router's role floor already raises a tool turn to a tool
# model -- but only when the ROUTER is choosing. A model that arrives
# pinned (an explicit override, "switch to a lighter model", a restored
# session) never passes through it. Same shape as every other hole this
# codebase has closed: a floor works wherever the decision is made, a
# gate only works where it is placed.

def test_a_tool_turn_is_moved_off_a_model_that_cannot_emit_actions():
    from backend.core.chat_capability_gate import ensure_tool_capable

    outcome = ensure_tool_capable("phi-3-mini-4k-instruct-q4",
                                  mode="local", turn_kind="tools")

    assert outcome.model_id == "nemo-12b-q5"
    assert outcome.switched_from == "phi-3-mini-4k-instruct-q4"
    assert outcome.refused is False


def test_the_same_model_is_left_alone_on_a_chat_turn():
    """phi-3-mini converses fine. The tool floor is about tools only."""
    from backend.core.chat_capability_gate import ensure_tool_capable

    outcome = ensure_tool_capable("phi-3-mini-4k-instruct-q4",
                                  mode="local", turn_kind="chat")

    assert outcome.model_id == "phi-3-mini-4k-instruct-q4"
    assert outcome.switched_from is None


def test_a_tool_capable_model_is_never_moved():
    from backend.core.chat_capability_gate import ensure_tool_capable

    for model in ("mistral-7b-q4km", "nemo-12b-q5"):
        for kind in ("chat", "tools", "heavy"):
            outcome = ensure_tool_capable(model, mode="local", turn_kind=kind)
            assert outcome.model_id == model, f"{model} moved on a {kind} turn"


def test_the_tool_floor_never_refuses_a_turn():
    """The model-pinning precedent: refusing turns is the failure mode.

    Pinning was implemented, measured and removed once because it refused
    turns on a loaded machine. A floor that cannot find a substitute
    leaves the user with the model they chose.
    """
    from backend.core import chat_capability_gate as gate

    outcome = gate.ensure_tool_capable("phi-3-mini-4k-instruct-q4",
                                       mode="local", turn_kind="tools")
    assert outcome.refused is False


def test_a_substitute_that_does_not_fit_in_memory_is_not_chosen(monkeypatch):
    """Trading a useless answer for one that never arrives is a bad trade."""
    from backend.core import chat_capability_gate as gate
    from backend.core import complexity_router

    monkeypatch.setattr(complexity_router, "_fits_in_memory",
                        lambda model_id: model_id != "nemo-12b-q5")

    outcome = gate.ensure_tool_capable("phi-3-mini-4k-instruct-q4",
                                       mode="local", turn_kind="tools")

    assert outcome.model_id == "mistral-7b-q4km"
    assert outcome.refused is False


def test_the_tool_floor_never_crosses_the_mode_boundary(monkeypatch):
    from backend.core import chat_capability_gate as gate

    monkeypatch.setattr(gate, "model_violates_mode_separation",
                        lambda model_id, mode: True)

    outcome = gate.ensure_tool_capable("phi-3-mini-4k-instruct-q4",
                                       mode="cloud", turn_kind="tools")

    assert outcome.model_id == "phi-3-mini-4k-instruct-q4"
    assert outcome.refused is False


def test_none_still_means_the_router_chooses():
    """Automatic routing is not a pin and must pass through untouched."""
    from backend.core.chat_capability_gate import ensure_tool_capable

    outcome = ensure_tool_capable(None, mode="automatic", turn_kind="tools")
    assert outcome.model_id is None
    assert outcome.switched_from is None
