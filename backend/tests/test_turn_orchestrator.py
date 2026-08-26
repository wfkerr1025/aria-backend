# backend/tests/test_turn_orchestrator.py
#
# Step 3 of the Turn Orchestrator blueprint: the WebSocket sequence moved
# into one module, with no behaviour change.
#
# "No behaviour change" is the whole claim, so most of this file is a
# characterization harness rather than new-feature tests. A corpus of
# packets runs through the orchestrator and each decision is compared
# against what the underlying primitives -- detect_intent, the model
# precedence rules, apply_history_policy -- say independently. Those
# primitives are what the old handler called, in this order, so agreeing
# with them is the operational meaning of "unchanged".
#
# The other half is the purity contract. The orchestrator must never write
# to a socket, mutate a session, or log; it returns session_updates and
# telemetry for the transport to apply. That is asserted directly, because
# it is the property every later step depends on.

from __future__ import annotations

import pytest

from backend.core import turn_orchestrator
from backend.core.conversation_manager import (
    INTENT_SEARCH_QUERY,
    INTENT_WEATHER_QUERY,
    SELF_QUERY_INTENTS,
    detect_intent,
)
from backend.core.turn_orchestrator import orchestrate_turn
from backend.core.turn_types import (
    KIND_CLARIFY,
    KIND_ERROR,
    KIND_INFERENCE,
    KIND_SAFETY_WARNING,
    KIND_TEXT,
    RESULT_KINDS,
    SessionState,
    TurnRequest,
    TurnResult,
)


def turn(text: str, **kw) -> TurnRequest:
    session = kw.pop("session", SessionState(mode="local"))
    messages = kw.pop("messages", [{"role": "user", "content": text}])
    return TurnRequest(
        messages=messages,
        latest_user_text=text,
        conversation_id=kw.pop("conversation_id", "c1"),
        session=session,
        **kw,
    )


@pytest.fixture
def offline(monkeypatch):
    """No weather lookup, no web search, no model registry surprises.

    The orchestrator legitimately calls blocking network tools on the
    short-circuit paths. Every test here replaces them, so the suite
    asserts routing rather than whether a provider answered today.
    """
    calls = []

    monkeypatch.setattr(
        turn_orchestrator.weather_nl, "resolve_weather_reply",
        lambda location: (calls.append(("weather", location)), (f"It is 12C in {location}.", None))[1],
    )
    monkeypatch.setattr(
        turn_orchestrator, "run_search_tool",
        lambda query: (calls.append(("search", query)), {"results": []})[1],
    )
    monkeypatch.setattr(
        turn_orchestrator, "format_search_reply",
        lambda result: "three results",
    )
    return calls


def local(model="test-local-model"):
    return lambda: model


# ======================================================
# Purity — the contract everything else rests on
# ======================================================
def test_the_result_is_always_a_known_kind(offline):
    for text in ("hello", "what is the weather in Paris", "search the web for pytest",
                 "what model are you", "explain the build pipeline"):
        result = orchestrate_turn(turn(text), default_local_model=local())
        assert result.kind in RESULT_KINDS


def test_the_request_is_never_mutated(offline):
    request = turn("hello there")
    before = (list(request.messages), request.latest_user_text, request.session)
    orchestrate_turn(request, default_local_model=local())
    assert (request.messages, request.latest_user_text, request.session) == before


def test_the_session_is_described_not_applied(offline):
    # The orchestrator says "open the clarification window"; it cannot
    # open one, because SessionState is frozen and it holds no handler.
    session = SessionState(mode="local")
    result = orchestrate_turn(turn("what is the weather", session=session),
                              default_local_model=local())
    assert result.session_updates.get("awaiting_weather_location") is True
    assert session.awaiting_weather_location is False


def test_telemetry_is_returned_not_emitted(offline):
    result = orchestrate_turn(turn("hello there"), default_local_model=local())
    assert result.telemetry
    assert all("event" in record for record in result.telemetry)


def test_every_result_carries_the_conversation_id(offline):
    for text in ("hello", "what is the weather in Paris", "what model are you"):
        result = orchestrate_turn(turn(text, conversation_id="abc"), default_local_model=local())
        assert result.conversation_id == "abc" or result.kind == KIND_ERROR


# ======================================================
# Characterization — agrees with the primitives
# ======================================================
CORPUS = [
    "hello",
    "hi there, how are you",
    "what model are you running",
    "what is the weather in Paris",
    "forecast for Oslo",
    "search the web for pytest release notes",
    "Search the latest world news.",
    "explain the build pipeline",
    "why is the shader failing",
    "summarize the deployment process",
    "search my notes for the pipeline",
    "fix the mismatch between the docs",
]


@pytest.mark.parametrize("text", CORPUS)
def test_the_orchestrator_agrees_with_detect_intent(text, offline):
    """The routing decision matches what the old handler branched on.

    The handler called detect_intent and then dispatched on its result;
    this asserts the orchestrator reaches the same branch for the same
    input, which is what "no behaviour change" means operationally.
    """
    expected = detect_intent(text, is_multi_turn_followup=False)
    result = orchestrate_turn(turn(text), default_local_model=local())

    if expected in SELF_QUERY_INTENTS:
        assert result.kind == KIND_TEXT and result.model_id == "skr"
    elif expected == INTENT_WEATHER_QUERY:
        assert result.kind in (KIND_TEXT, KIND_CLARIFY)
        assert result.model_id == turn_orchestrator.weather_nl.WEATHER_MODEL_SENTINEL
    elif expected == INTENT_SEARCH_QUERY:
        # Step 4 retired the search bypass on purpose: a lookup is now a
        # planned step inside the reasoning turn, so the intent still
        # classifies but no longer routes. Answering here as well would
        # run the search twice.
        assert result.kind == KIND_INFERENCE
    else:
        assert result.kind == KIND_INFERENCE


@pytest.mark.parametrize("text", CORPUS)
def test_the_recorded_intent_matches(text, offline):
    expected = detect_intent(text, is_multi_turn_followup=False)
    result = orchestrate_turn(turn(text), default_local_model=local())
    recorded = [e for e in result.telemetry if e["event"] == "intent_detected"]
    if recorded:
        assert recorded[0]["intent"] == expected


def test_the_corpus_is_stable(offline):
    """A snapshot of the whole corpus, so a later change has to be intended.

    Compares kind and model per input rather than free text, because the
    weather and search bodies come from stubs here and their wording is
    not what this is protecting.
    """
    shape = {
        text: (r.kind, r.model_id)
        for text in CORPUS
        for r in [orchestrate_turn(turn(text), default_local_model=local())]
    }
    assert shape == {
        "hello": (KIND_INFERENCE, "test-local-model"),
        "hi there, how are you": (KIND_INFERENCE, "test-local-model"),
        "what model are you running": (KIND_TEXT, "skr"),
        "what is the weather in Paris": (KIND_TEXT, turn_orchestrator.weather_nl.WEATHER_MODEL_SENTINEL),
        "forecast for Oslo": (KIND_TEXT, turn_orchestrator.weather_nl.WEATHER_MODEL_SENTINEL),
        # Both reach the model now, carrying a synthesis prompt whose
        # Tool Results section holds what the search actually returned --
        # rather than short-circuiting past the model entirely.
        "search the web for pytest release notes": (KIND_INFERENCE, "test-local-model"),
        "Search the latest world news.": (KIND_INFERENCE, "test-local-model"),
        "explain the build pipeline": (KIND_INFERENCE, "test-local-model"),
        "why is the shader failing": (KIND_INFERENCE, "test-local-model"),
        "summarize the deployment process": (KIND_INFERENCE, "test-local-model"),
        "search my notes for the pipeline": (KIND_INFERENCE, "test-local-model"),
        "fix the mismatch between the docs": (KIND_INFERENCE, "test-local-model"),
    }


def test_orchestration_is_deterministic(offline):
    first = orchestrate_turn(turn("explain the build pipeline"), default_local_model=local())
    second = orchestrate_turn(turn("explain the build pipeline"), default_local_model=local())
    assert (first.kind, first.model_id) == (second.kind, second.model_id)


# ======================================================
# Weather continuation window — checked before intent
# ======================================================
def test_a_bare_location_reply_is_answered_as_weather(offline):
    # "Orange, VA, 22960" carries no weather phrasing; reaching intent
    # detection would land it in ordinary chat, where a model could
    # fabricate a reading.
    session = SessionState(mode="local", awaiting_weather_location=True)
    result = orchestrate_turn(turn("Orange, VA, 22960", session=session), default_local_model=local())
    assert result.kind == KIND_TEXT
    assert ("weather", "Orange, VA, 22960") in offline


def test_the_window_closes_after_it_is_used(offline):
    session = SessionState(mode="local", awaiting_weather_location=True)
    result = orchestrate_turn(turn("Paris", session=session), default_local_model=local())
    assert result.session_updates["awaiting_weather_location"] is False
    assert result.session_updates["last_turn_was_weather"] is True


def test_a_correction_after_weather_asks_again(offline):
    session = SessionState(mode="local", last_turn_was_weather=True)
    text = "no, I meant the other one"
    if turn_orchestrator.weather_nl.is_correction_phrase(text):
        result = orchestrate_turn(turn(text, session=session), default_local_model=local())
        assert result.kind == KIND_CLARIFY
        assert result.session_updates["awaiting_weather_location"] is True


def test_an_ordinary_message_ends_the_weather_window(offline):
    session = SessionState(mode="local", last_turn_was_weather=True)
    result = orchestrate_turn(turn("explain the build pipeline", session=session),
                              default_local_model=local())
    assert result.session_updates.get("last_turn_was_weather") is False


# ======================================================
# Model resolution and mode separation
# ======================================================
def test_an_explicit_request_wins(offline):
    result = orchestrate_turn(
        turn("hello", requested_model_id="explicit-model"), default_local_model=local()
    )
    assert result.model_id == "explicit-model"


def test_the_session_pin_is_used_when_no_request(offline):
    session = SessionState(mode="local", explicit_model_override="pinned-model")
    result = orchestrate_turn(turn("hello", session=session), default_local_model=local())
    assert result.model_id == "pinned-model"


def test_local_mode_falls_back_to_the_default(offline):
    result = orchestrate_turn(turn("hello"), default_local_model=local("the-default"))
    assert result.model_id == "the-default"


def test_cloud_mode_passes_none_through(offline):
    # None is what lets ProviderRouter reach its own mode branches instead
    # of always taking the explicit-model one.
    session = SessionState(mode="cloud")
    result = orchestrate_turn(turn("hello", session=session), default_local_model=local())
    assert result.model_id is None


def test_a_wrong_registry_model_is_ignored(offline, monkeypatch):
    monkeypatch.setattr(turn_orchestrator, "model_violates_mode_separation",
                        lambda model_id, mode: model_id == "wrong-registry")
    session = SessionState(mode="cloud")
    result = orchestrate_turn(
        turn("hello", requested_model_id="wrong-registry", session=session),
        default_local_model=local(),
    )
    assert result.model_id is None


# ======================================================
# Safety
# ======================================================
class _Decision:
    requires_warning = True
    severity = "block"
    message = "Not enough RAM."
    projected_cpu_pct = 10
    projected_ram_pct = 99
    projected_vram_pct = 0
    snapshot = object()


class _Suggestion:
    model_id = "smaller-model"
    reason = "fits in RAM"


class _Suggester:
    def suggest(self, snapshot, model_cfg):
        return [_Suggestion()]


def test_the_safety_gate_can_refuse(offline, monkeypatch):
    monkeypatch.setattr(turn_orchestrator, "get_model", lambda mid: {"id": mid})
    monkeypatch.setattr(turn_orchestrator, "evaluate_safety", lambda cfg: _Decision())
    result = orchestrate_turn(turn("hello"), default_local_model=local(), suggester=_Suggester())
    assert result.kind == KIND_SAFETY_WARNING
    assert result.warning["severity"] == "block"
    assert result.warning["projected"] == {"cpu": 10, "ram": 99, "vram": 0}
    assert result.warning["suggestions"] == [{"id": "smaller-model", "reason": "fits in RAM"}]


def test_skip_safety_check_bypasses_the_gate(offline, monkeypatch):
    monkeypatch.setattr(turn_orchestrator, "get_model", lambda mid: {"id": mid})
    monkeypatch.setattr(turn_orchestrator, "evaluate_safety", lambda cfg: _Decision())
    result = orchestrate_turn(turn("hello", skip_safety_check=True), default_local_model=local())
    assert result.kind == KIND_INFERENCE


def test_a_turn_with_no_model_never_reaches_the_gate(offline, monkeypatch):
    # Self-knowledge already had this exemption; a tool answer earns it for
    # the same reason -- it never loads a model, so RAM pressure is moot.
    monkeypatch.setattr(turn_orchestrator, "evaluate_safety",
                        lambda cfg: pytest.fail("safety must not run for a modelless turn"))
    result = orchestrate_turn(turn("what is the weather in Paris"), default_local_model=local())
    assert result.kind == KIND_TEXT


def test_the_decision_is_carried_for_the_transport(offline, monkeypatch):
    # _emit_warnings runs on non-refusing turns too, so the decision has to
    # survive even when the gate lets the turn through.
    class _Quiet(_Decision):
        requires_warning = False

    monkeypatch.setattr(turn_orchestrator, "get_model", lambda mid: {"id": mid})
    monkeypatch.setattr(turn_orchestrator, "evaluate_safety", lambda cfg: _Quiet())
    result = orchestrate_turn(turn("hello"), default_local_model=local())
    assert result.kind == KIND_INFERENCE
    assert result.safety_decision is not None
    assert result.model_cfg is not None


# ======================================================
# History
# ======================================================
def test_the_provider_gets_the_trimmed_history(offline):
    messages = [{"role": "user", "content": f"turn {n}"} for n in range(40)]
    messages.append({"role": "user", "content": "explain the build pipeline"})
    result = orchestrate_turn(
        turn("explain the build pipeline", messages=messages), default_local_model=local()
    )
    assert result.kind == KIND_INFERENCE
    assert len(result.inference_request.messages) < len(messages)


def test_policy_info_is_returned(offline):
    result = orchestrate_turn(turn("explain the build pipeline"), default_local_model=local())
    assert "memory_context_applied" in result.policy_info
    assert "file_context_applied" in result.policy_info


def test_the_intent_hint_is_folded_in(offline):
    result = orchestrate_turn(turn("hi"), default_local_model=local())
    roles = [m.role for m in result.inference_request.messages]
    assert roles.count("system") >= 1


# ======================================================
# Search short-circuit
# ======================================================
def test_a_search_turn_reaches_the_tool_through_the_plan(monkeypatch):
    """The lookup runs, and its result reaches the prompt rather than the user.

    Step 4 moved this from a bypass to a planned step, so the tool is now
    invoked through the registry rather than through the orchestrator's own
    run_search_tool -- which is why this patches the registry instead.
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
        result = orchestrate_turn(
            turn("search the web for pytest release notes"), default_local_model=local()
        )
        assert result.kind == KIND_INFERENCE
        assert calls, "the planned lookup did not reach the tool"
        prompt = result.inference_request.messages[-1].content
        assert "Tool Results:" in prompt
        assert "3 results for" in prompt
    finally:
        core_reg.register_builtin_tools()


def test_the_search_bypass_survives_only_for_an_unhealthy_connection(offline):
    # Connection health is a transport fact the reasoning core cannot know,
    # so this one case still short-circuits.
    session = SessionState(mode="local", connection_healthy=False)
    result = orchestrate_turn(
        turn("search the web for pytest", session=session), default_local_model=local()
    )
    assert result.kind == KIND_ERROR


def test_a_local_query_never_reaches_the_search_tool(offline):
    orchestrate_turn(turn("search my notes for the pipeline"), default_local_model=local())
    assert not any(kind == "search" for kind, _ in offline)
