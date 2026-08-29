# backend/tests/test_rest_turn_wiring.py
#
# REST joins the shared Turn Orchestrator.
#
# _prepare_chat_turn used to be, by its own docstring, "a third
# independent orchestration of the same backend.core primitives". This
# suite pins what that orchestration decides -- the dict it returns, which
# is the contract POST /v1/chat and POST /v1/chat/stream both branch on --
# so the rewire underneath it can be shown to preserve behaviour rather
# than asserted to.
#
# The dict is the right seam to characterize: both routes consume it and
# nothing else does, so a corpus over it covers every REST chat decision
# without a TestClient, an API key, or a running provider.
#
# Two things REST owns that the WebSocket path does not, and that
# therefore need coverage here specifically:
#
#   * weather continuation state comes out of `history`, not a connection
#     object -- REST has no per-connection state to hold it in
#   * an unknown model_id is an HTTP 400, not a silent pass-through
#
# Written and run green BEFORE the rewire, so any disagreement afterwards
# is a real difference and not a guess about what the old code did.

from __future__ import annotations

import asyncio
import types

import pytest

from backend.core import weather_nl
from backend.rest.router import ChatMessage, ChatRequest


LOCAL_MODEL = "test-local-model"

# What an UNPINNED chat turn actually lands on.
#
# LOCAL_MODEL above is what get_default_model_id() is stubbed to return,
# and it used to be the answer for every ordinary turn. It is not any
# more: backend/chat/model_router.py routes an unpinned turn by what the
# turn will do, and plain conversation goes to the chat-role model. The
# stub still matters -- it is what precedence resolves before routing
# runs, and what a turn falls back to when routing has no opinion.
#
# Read from the role table rather than hard-coded, so moving the chat
# role to another model does not silently leave this asserting the old
# one.
from backend.config.model_roles import installed_model_for  # noqa: E402

CHAT_MODEL = installed_model_for("phi-3-mini-4k-instruct-q4")
# A lookup is a tool turn, and a tool turn routes to the tool model --
# which is also where the evidence floor would have put it anyway.
TOOL_MODEL = installed_model_for("mistral-7b")


def prepare(message: str, **kw):
    """One turn through REST's resolution step."""
    from backend.rest.router import _prepare_chat_turn

    history = kw.pop("history", None)
    payload = ChatRequest(
        message=message,
        history=[ChatMessage(role=r, content=c) for r, c in (history or [])],
        **kw,
    )

    async def run():
        return await _prepare_chat_turn(payload, asyncio.get_running_loop())

    return asyncio.run(run())


def shape(result: dict) -> tuple:
    """(short-circuit kind or None, model_id) -- the decision, not the prose."""
    sc = result["short_circuit"]
    return (sc["kind"] if sc else None, result["model_id"])


@pytest.fixture
def offline(monkeypatch):
    """No network, no persisted model, no live resource snapshot.

    Everything is patched at BOTH possible call sites -- the router's own
    name binding and the orchestrator's -- so the same fixture holds
    before and after the rewire and the corpus below stays comparable
    across it. `raising=False` covers the names that only exist on one
    side.
    """
    from backend.core import turn_orchestrator as orch
    from backend.rest import router as rest

    def both(attr, value):
        for module in (rest, orch):
            monkeypatch.setattr(module, attr, value, raising=False)

    monkeypatch.setattr(
        weather_nl, "resolve_weather_reply",
        lambda location: (f"Weather for {location}: 12C, Clear.", None),
    )
    both("run_search_tool", lambda query: {"results": [], "query": query})
    both("format_search_reply", lambda result: "three results")
    both("get_default_model_id", lambda: LOCAL_MODEL)
    both("model_violates_mode_separation", lambda model_id, mode: False)
    both("get_model", lambda model_id: {"id": model_id, "provider": "llamacpp"})
    both("evaluate_safety", lambda model_cfg: types.SimpleNamespace(
        requires_warning=False, severity="ok", message="", snapshot=None,
        projected_cpu_pct=0.0, projected_ram_pct=0.0, projected_vram_pct=0.0,
    ))

    mode = {"mode": "local", "override": None, "pinned": []}
    monkeypatch.setattr(rest, "ModeManager", lambda: types.SimpleNamespace(
        get_mode=lambda: mode["mode"],
        get_explicit_model_override=lambda: mode["override"],
        # Recorded rather than persisted: the real one writes to disk.
        set_explicit_model_override=mode["pinned"].append,
    ), raising=False)
    return mode


@pytest.fixture
def stub_search_tool():
    """A web_search double in the core registry.

    Once the reasoning core owns lookups, a search turn reaches the tool
    through the plan rather than through a bypass, so the registry is
    where it has to be intercepted.
    """
    from backend.core import tool_registry as core_reg

    calls = []
    core_reg.register_tool(
        core_reg.ToolSchema(
            name="web_search", description="test double",
            parameters={"query": {"type": "string", "required": True}},
            permission=core_reg.PERMISSION_NETWORK,
        ),
        lambda query: (calls.append(query), {"raw": {}, "reply": f"3 results for {query}"})[1],
    )
    try:
        yield calls
    finally:
        core_reg.register_builtin_tools()


# ======================================================
# The corpus -- every REST chat decision, pinned
# ======================================================
def test_a_plain_message_reaches_the_model(offline):
    result = prepare("explain the build pipeline")
    assert shape(result) == (None, CHAT_MODEL)
    assert result["inference_request"] is not None


def test_a_self_query_is_answered_without_a_model(offline):
    result = prepare("what model are you running")
    assert shape(result) == ("text", "skr")
    assert result["inference_request"] is None


def test_a_weather_query_is_answered_from_the_fusion_engine(offline):
    result = prepare("what is the weather in Paris")
    assert shape(result) == ("text", weather_nl.WEATHER_MODEL_SENTINEL)
    assert "Paris" in result["short_circuit"]["text"]


def test_a_weather_query_with_no_place_asks_for_one(offline):
    result = prepare("what is the weather")
    assert shape(result) == ("text", "system")
    assert result["short_circuit"]["text"] == weather_nl.CLARIFICATION_PROMPT


def test_a_search_query_reaches_the_tool_through_the_plan(offline, stub_search_tool):
    """The one intended difference the rewire brings to REST.

    REST used to answer a search straight from the tool and never invoke
    a model, exactly as the WebSocket path did. Both bypasses were
    retired when lookups became a planned step inside the reasoning turn
    -- running the bypass as well would search twice. The lookup still
    happens (stub_search_tool records it); what changed is that its
    result now arrives as evidence in the prompt rather than as the whole
    reply.
    """
    result = prepare("search the web for pytest release notes")
    assert shape(result) == (None, TOOL_MODEL)
    assert stub_search_tool, "the planned lookup did not reach the tool"

    # Not the section heading: the prompt shape depends on whether the
    # model is trusted with evidence (backend/core/evidence_routing.py).
    prompt = result["inference_request"].messages[-1].content
    assert "3 results for" in prompt


# ======================================================
# Continuation state, read out of history
# ======================================================
def test_a_bare_location_reply_is_recognised_from_history(offline):
    result = prepare(
        "Orange, VA, 22960",
        history=[("user", "what is the weather"),
                 ("assistant", weather_nl.CLARIFICATION_PROMPT)],
    )
    assert shape(result) == ("text", weather_nl.WEATHER_MODEL_SENTINEL)
    assert "Orange, VA, 22960" in result["short_circuit"]["text"]


def test_a_correction_after_a_weather_answer_reopens_the_window(offline):
    result = prepare(
        "that is incorrect, try again",
        history=[("user", "weather in Orange VA"),
                 ("assistant", "Weather for Orange, Virginia: 66F, Cloudy.")],
    )
    assert shape(result) == ("text", "system")
    assert result["short_circuit"]["text"] == weather_nl.CLARIFICATION_PROMPT


def test_a_correction_without_a_weather_turn_behind_it_is_ordinary_chat(offline):
    result = prepare(
        "that is incorrect, try again",
        history=[("user", "explain the pipeline"),
                 ("assistant", "The pipeline compiles shaders first.")],
    )
    assert shape(result) == (None, CHAT_MODEL)


# ======================================================
# Model precedence, mode separation, safety
# ======================================================
def test_an_explicit_model_id_wins(offline):
    result = prepare("explain the build pipeline", model_id="explicit-model")
    assert shape(result) == (None, "explicit-model")


def test_a_session_pin_is_used_when_the_request_names_nothing(offline):
    offline["override"] = "pinned-model"
    result = prepare("explain the build pipeline")
    assert shape(result) == (None, "pinned-model")


def test_a_model_from_the_wrong_registry_is_ignored(offline, monkeypatch):
    from backend.core import turn_orchestrator as orch
    from backend.rest import router as rest

    for module in (rest, orch):
        monkeypatch.setattr(
            module, "model_violates_mode_separation",
            lambda model_id, mode: model_id == "gpt-4o", raising=False,
        )
    result = prepare("explain the build pipeline", model_id="gpt-4o")
    assert shape(result) == (None, CHAT_MODEL)


def test_cloud_mode_with_no_explicit_model_passes_none_through(offline):
    offline["mode"] = "cloud"
    result = prepare("explain the build pipeline")
    assert result["short_circuit"] is None or result["short_circuit"]["kind"] == "safety_warning"


def test_the_safety_gate_short_circuits_with_the_documented_shape(offline, monkeypatch):
    from backend.core import turn_orchestrator as orch
    from backend.rest import router as rest

    decision = types.SimpleNamespace(
        requires_warning=True, severity="caution", message="too heavy",
        snapshot=None, projected_cpu_pct=50.0, projected_ram_pct=90.0,
        projected_vram_pct=0.0,
    )
    for module in (rest, orch):
        monkeypatch.setattr(module, "evaluate_safety", lambda cfg: decision, raising=False)
    monkeypatch.setattr(rest._lighter_model_engine, "suggest", lambda s, c: [])

    result = prepare("explain the build pipeline")
    sc = result["short_circuit"]
    assert sc["kind"] == "safety_warning"
    assert sc["payload"]["severity"] == "caution"
    assert sc["payload"]["projected"] == {"cpu": 50.0, "ram": 90.0, "vram": 0.0}
    assert sc["payload"]["model_id"] == CHAT_MODEL


def test_skip_safety_check_bypasses_the_gate_and_allows_override(offline, monkeypatch):
    from backend.core import turn_orchestrator as orch
    from backend.rest import router as rest

    def refuse(cfg):
        raise AssertionError("the gate ran despite skip_safety_check")

    for module in (rest, orch):
        monkeypatch.setattr(module, "evaluate_safety", refuse, raising=False)

    result = prepare("explain the build pipeline", skip_safety_check=True)
    assert result["short_circuit"] is None
    assert result["inference_request"].allow_override is True


def test_an_unknown_model_id_is_a_400(offline, monkeypatch):
    from backend import ipc_errors
    from backend.core import turn_orchestrator as orch
    from backend.rest import router as rest
    from fastapi import HTTPException

    for module in (rest, orch):
        monkeypatch.setattr(module, "get_model", lambda model_id: None, raising=False)

    with pytest.raises(HTTPException) as excinfo:
        prepare("explain the build pipeline", model_id="not-a-real-model")
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["error"]["code"] == ipc_errors.UNKNOWN_MODEL


def test_a_spoken_model_switch_is_applied_through_the_guard(offline, monkeypatch):
    """The second intended difference the rewire brings to REST.

    _prepare_chat_turn used to ignore INTENT_MODEL_SWITCH outright -- its
    docstring deferred it -- so "switch to X" reached a model as ordinary
    chat and nothing changed. The orchestrator resolves it for both
    transports now, and REST applies it through routing_guard, the same
    module the WebSocket path calls.

    The earlier version of this test stubbed resolve_model_switch_target
    as {"id", "name"}. That is not its contract -- it returns
    {"kind": "model"|"mode"|"provider", ...} -- and the stub was wrong in
    exactly the way the orchestrator was, so the two agreed and the test
    passed while a real switch answered "Switched to None." The stub now
    matches the real return shape, which is the only reason this test is
    worth having.
    """
    from backend.core import turn_orchestrator as orch
    from backend.rest import router as rest

    monkeypatch.setattr(orch, "detect_model_switch_target", lambda text: "mistral")
    monkeypatch.setattr(
        orch, "resolve_model_switch_target",
        lambda target: {"kind": "model", "model_id": "mistral-7b-q4km"},
    )
    monkeypatch.setattr(rest.model_manager, "set_active_model", lambda mid: {"ok": True})
    monkeypatch.setattr(
        rest.routing_guard, "attempt_model_override_switch",
        lambda mm, mid: types.SimpleNamespace(ok=True, error_message=None,
                                              display_name="Mistral 7B"),
    )

    result = prepare("switch to mistral")
    assert shape(result) == ("text", "system")
    assert "mistral-7b-q4km" in result["short_circuit"]["text"]
    assert result["inference_request"] is None


def test_a_rejected_model_switch_says_so_instead_of_claiming_success(offline, monkeypatch):
    """A guard refusal must not be reported as a switch that happened."""
    from backend.core import turn_orchestrator as orch
    from backend.rest import router as rest

    # "mistral" rather than a cloud id, because detect_intent re-resolves
    # the target itself against the real registry -- a phrase it cannot
    # resolve never reaches the model-switch branch at all.
    monkeypatch.setattr(orch, "detect_model_switch_target", lambda text: "mistral")
    monkeypatch.setattr(
        orch, "resolve_model_switch_target",
        lambda target: {"kind": "model", "model_id": "gpt-4o"},
    )
    monkeypatch.setattr(rest.model_manager, "set_active_model", lambda mid: {"ok": True})
    monkeypatch.setattr(
        rest.routing_guard, "attempt_model_override_switch",
        lambda mm, mid: types.SimpleNamespace(
            ok=False, error_message="gpt-4o is a cloud model and you are in Local Mode.",
            display_name=None),
    )

    result = prepare("switch to mistral")
    assert shape(result) == ("text", "system")
    assert "Local Mode" in result["short_circuit"]["text"]
    assert "Switched" not in result["short_circuit"]["text"]


# ======================================================
# The returned contract, which both routes depend on
# ======================================================
def test_every_result_carries_the_documented_keys(offline):
    # Exact, not a superset: both routes read this dict by name, and a key
    # appearing here that the docstring above prepare() does not mention is
    # a contract that drifted. `notices` was added with the chat capability
    # gate -- an HTTP caller whose named model was substituted learns it
    # here, since REST has no banner to push a warning to.
    for message in ("hello", "what model are you", "what is the weather in Paris",
                    "explain the build pipeline"):
        result = prepare(message)
        assert set(result) == {"short_circuit", "inference_request", "model_id",
                               "conversation_id", "notices"}


def test_an_ordinary_turn_carries_no_notices(offline):
    # The gate is silent unless it acts. A notice on every turn would train
    # the user to ignore the one turn it matters on.
    assert prepare("explain the build pipeline")["notices"] == []


def test_the_session_id_comes_back_as_the_conversation_id(offline):
    result = prepare("explain the build pipeline", session_id="s-42")
    assert result["conversation_id"] == "s-42"


def test_a_short_circuit_never_carries_an_inference_request(offline):
    for message in ("what model are you", "what is the weather in Paris"):
        result = prepare(message)
        assert result["short_circuit"] is not None
        assert result["inference_request"] is None
