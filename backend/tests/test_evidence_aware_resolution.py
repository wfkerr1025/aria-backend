# backend/tests/test_evidence_aware_resolution.py
#
# Choosing a model for a turn that depends on a lookup.
#
# The bug this arc fixes, traced end to end on the query "What is the
# Stock price of Microsoft?":
#
#   classify_task_complexity measures one thing -- prompt length. The
#   question is 37 characters, so it classified "low", and "low" maps to
#   the 0.5B emergency model. That question cannot be answered from any
#   model's weights, only from a lookup.
#
#   Worse, it happened precisely when the lookup had FAILED. With evidence
#   the prompt is the assembled synthesis document (~1600 chars, "high",
#   the 12B model); without it the turn fell back to the raw question (37
#   chars, "low", the 0.5B model). Losing the evidence also lost the model
#   that might have said "I don't know".
#
# So two things change. Evidence in the prompt overrides the length
# heuristic, and a turn whose evidence never arrived stops rather than
# answering from weights.

from __future__ import annotations

import pytest

from backend.core import complexity_router as cr
from backend.core import evidence_routing as routing
from backend.core.complexity_router import (
    DIFFICULT_MODEL_ID,
    MEDIUM_MODEL_ID,
    SIMPLE_MODEL_ID,
    TRIVIAL_MODEL_ID,
    select_local_model_for_prompt,
)
from backend.core.turn_orchestrator import EVIDENCE_MISSING_ANSWER, orchestrate_turn
from backend.core.turn_types import KIND_INFERENCE, KIND_TEXT, SessionState, TurnRequest


QUERY = "What is the Stock price of Microsoft?"
EVIDENCE = "Microsoft (MSFT) is trading at $412.30 (source: example.com)"

# The two prompt shapes that carry evidence, as the code actually builds
# them. Kept realistic rather than minimal: what is being tested is that
# these are recognised, so a stand-in would test nothing.
FULL_SYNTHESIS_PROMPT = (
    "User Query: What is the Stock price of Microsoft?\n\n"
    "Evidence Summary: 1 tool result below, listed under Tool Results.\n\n"
    "Tool Results:\n"
    f"- web_search (step1): {EVIDENCE}\n\n"
    "Synthesis Instructions:\n"
    "1. Use ONLY the evidence above.\n"
)
SIMPLIFIED_PROMPT = routing.simplified_prompt(QUERY, [f"web_search (step1): {EVIDENCE}"])

CAPABLE_TIERS = {MEDIUM_MODEL_ID, DIFFICULT_MODEL_ID}


@pytest.fixture
def search_tool():
    from backend.core import tool_registry as core_reg

    core_reg.register_tool(
        core_reg.ToolSchema(
            name="web_search", description="test double",
            parameters={"query": {"type": "string", "required": True}},
            permission=core_reg.PERMISSION_NETWORK,
        ),
        lambda query: {"raw": {}, "reply": EVIDENCE},
    )
    try:
        yield
    finally:
        core_reg.register_builtin_tools()


@pytest.fixture
def broken_search_tool():
    """A lookup that fails, which is what makes evidence go missing."""
    from backend.core import tool_registry as core_reg

    def explode(query):
        raise RuntimeError("network down")

    core_reg.register_tool(
        core_reg.ToolSchema(
            name="web_search", description="test double",
            parameters={"query": {"type": "string", "required": True}},
            permission=core_reg.PERMISSION_NETWORK,
        ),
        explode,
    )
    try:
        yield
    finally:
        core_reg.register_builtin_tools()


def run(text=QUERY, *, model=None, mode="automatic", cloud=False, debug=False, monkeypatch):
    from backend.core import turn_orchestrator as orch

    monkeypatch.setattr(orch.key_manager, "list_configured_providers",
                        lambda: {"openai": cloud, "anthropic": False})
    return orchestrate_turn(
        TurnRequest(
            messages=[{"role": "user", "content": text}],
            latest_user_text=text,
            conversation_id="c1",
            session=SessionState(mode=mode, explicit_model_override=model),
            skip_safety_check=True,
            debug_trace=debug,
        ),
        default_local_model=lambda: model or DIFFICULT_MODEL_ID,
    )


# ======================================================
# 1 + 4. Evidence overrides the length heuristic
# ======================================================
def test_the_bug_the_length_heuristic_causes():
    """Pinned so the fix has something to be a fix of.

    The bare question is short, so without evidence it still routes to the
    emergency model. That is correct for an ordinary short chat message --
    and catastrophic for this one, which is why the turn never reaches it
    any more (see the evidence-missing tests below).
    """
    assert select_local_model_for_prompt(QUERY) == TRIVIAL_MODEL_ID


@pytest.mark.parametrize("prompt", [FULL_SYNTHESIS_PROMPT, SIMPLIFIED_PROMPT])
def test_a_prompt_carrying_evidence_is_never_trivial(prompt):
    assert cr.prompt_carries_evidence(prompt)
    assert select_local_model_for_prompt(prompt) in CAPABLE_TIERS


def test_the_simplified_prompt_does_not_depend_on_its_length():
    """It measures 314 characters -- 14 from being classified "low".

    A routing decision resting on a 14-character margin is a decision
    waiting to flip, so length is not what decides it.
    """
    assert len(SIMPLIFIED_PROMPT) < 600, "if this grows the test has stopped testing the risk"
    assert select_local_model_for_prompt(SIMPLIFIED_PROMPT) in CAPABLE_TIERS

    padded = SIMPLIFIED_PROMPT + "\n" + ("filler. " * 200)
    assert select_local_model_for_prompt(padded) in CAPABLE_TIERS


@pytest.mark.parametrize("length", [1, 20, 37, 299, 301, 5000])
def test_length_is_ignored_entirely_when_evidence_is_present(length):
    assert select_local_model_for_prompt("x" * length, evidence_present=True) in CAPABLE_TIERS


def test_an_explicit_flag_beats_the_sniff():
    """A caller that knows is believed over the marker scan."""
    assert select_local_model_for_prompt(QUERY, evidence_present=True) in CAPABLE_TIERS
    assert select_local_model_for_prompt(FULL_SYNTHESIS_PROMPT, evidence_present=False) not in (None,)


def test_tool_use_counts_as_evidence():
    """The parameter existed and was recorded but never used."""
    assert select_local_model_for_prompt(QUERY, tool_use=True) in CAPABLE_TIERS


def test_a_busy_machine_cannot_step_below_the_floor(monkeypatch):
    """CPU saturation steps down a rung -- but not off the cliff.

    The tiers below the floor are the ones that cannot read evidence at
    all, so "the machine is loaded" is not a reason to reach them.
    """
    import types

    monkeypatch.setattr(cr, "get_resource_snapshot", lambda: types.SimpleNamespace(
        ram_total_gb=64.0, ram_used_gb=60.0, cpu_usage=99.0,
    ))
    assert select_local_model_for_prompt(QUERY, evidence_present=True) in CAPABLE_TIERS
    # The same pressure on an ordinary turn still steps down, as before.
    assert select_local_model_for_prompt(QUERY) == TRIVIAL_MODEL_ID


def test_an_ordinary_short_message_still_gets_the_small_model():
    """The heuristic is overridden for evidence, not replaced."""
    assert select_local_model_for_prompt("hello there") == TRIVIAL_MODEL_ID


# ======================================================
# 2. Automatic mode no longer bypasses capability routing
# ======================================================
def test_automatic_mode_defers_to_the_router_and_says_so(search_tool, monkeypatch):
    """Automatic leaves model_id None, and that is deliberately kept.

    Pinning the evidence floor here was implemented and then removed: a
    concrete model_id is one the safety gate evaluates, and on a loaded
    machine the pinned 7B came back safety_warning/caution -- refusing a
    turn that model_id=None would have run, because AutoSelector does its
    own hardware stepping without a blocking gate.

    The guarantee is kept elsewhere: complexity_router floors the tier for
    an evidence-bearing prompt, so Automatic's local branch cannot reach
    the emergency model for this turn. See the next test.
    """
    result = run(cloud=False, monkeypatch=monkeypatch)

    assert result.kind == KIND_INFERENCE
    assert result.model_id is None
    assert any(e.get("decision") == routing.ROUTE_DEFERRED_TO_ROUTER for e in result.telemetry)


def test_the_prompt_automatic_mode_produces_cannot_reach_the_emergency_model(search_tool, monkeypatch):
    """The other half of the guarantee, at the layer that actually decides.

    This is what closes the Automatic-mode bypass: whatever ProviderRouter
    does with model_id=None, the prompt it classifies carries evidence, so
    the ladder is floored.
    """
    from backend.core.streaming_engine import _extract_prompt_text

    result = run(cloud=False, monkeypatch=monkeypatch)
    prompt = _extract_prompt_text(result.inference_request)

    assert cr.prompt_carries_evidence(prompt)
    assert select_local_model_for_prompt(prompt) in CAPABLE_TIERS


def test_automatic_mode_with_cloud_leaves_the_choice_open(search_tool, monkeypatch):
    """model_id=None is how this codebase says "ProviderRouter chooses".

    Pinning a local model here would take Automatic's cloud option away.
    If it does choose local, complexity_router's floor still applies.
    """
    result = run(cloud=True, monkeypatch=monkeypatch)
    assert result.model_id is None


def test_an_evidence_turn_never_resolves_to_the_emergency_model(search_tool, monkeypatch):
    """The headline guarantee, asserted at both layers.

    The orchestrator must not hand out the 0.5B model, and the router must
    not choose it for the prompt that turn produces.
    """
    from backend.core.streaming_engine import _extract_prompt_text

    for mode, cloud in (("automatic", False), ("automatic", True), ("local", False)):
        result = run(mode=mode, cloud=cloud, monkeypatch=monkeypatch)
        assert result.model_id != TRIVIAL_MODEL_ID

        if result.inference_request is not None:
            prompt = _extract_prompt_text(result.inference_request)
            assert select_local_model_for_prompt(prompt) != TRIVIAL_MODEL_ID


# ======================================================
# 3. Evidence expected and missing
# ======================================================
def test_a_failed_lookup_does_not_fall_back_to_the_model(broken_search_tool, monkeypatch):
    """The worst case in the old behaviour, now refused outright.

    A question about a current price, whose search failed, answered by the
    weakest installed model from its own weights, is fabrication by
    construction.
    """
    result = run(cloud=False, monkeypatch=monkeypatch)

    assert result.kind == KIND_TEXT
    assert result.evidence_missing is True
    assert result.text == EVIDENCE_MISSING_ANSWER
    assert result.inference_request is None, "no model may be asked to fill the gap"


def test_the_evidence_missing_turn_names_no_model(broken_search_tool, monkeypatch):
    result = run(cloud=False, monkeypatch=monkeypatch)
    assert result.model_id != TRIVIAL_MODEL_ID
    assert result.model_id is None


def test_the_message_says_what_happened_without_guessing(broken_search_tool, monkeypatch):
    """It must not contain a price, a hedge, or an invitation to retry."""
    result = run(cloud=False, monkeypatch=monkeypatch)
    lowered = result.text.lower()

    assert "could not retrieve current data" in lowered
    assert "$" not in result.text
    assert "microsoft" not in lowered, "it must not restate a fact it does not have"


def test_a_turn_that_never_expected_evidence_is_unaffected(monkeypatch):
    """No lookup was wanted, so nothing is missing."""
    result = run("explain the build pipeline", cloud=False, monkeypatch=monkeypatch)
    assert result.evidence_missing is False
    assert result.kind == KIND_INFERENCE


def test_a_successful_lookup_is_not_reported_as_missing(search_tool, monkeypatch):
    result = run(cloud=False, monkeypatch=monkeypatch)
    assert result.evidence_missing is False


def test_the_reasoning_core_failing_still_fails_open(monkeypatch):
    """A broken reasoning core is not the same as a failed lookup.

    Engine B returning nothing at all -- unavailable, or raising -- has
    always fallen through to ordinary chat, and still does. Only Engine B
    running and finding nothing counts as evidence missing.
    """
    from backend.core import turn_orchestrator as orch

    monkeypatch.setattr(orch, "_build_reasoning_prompt", lambda *a, **k: None)
    result = run(cloud=False, monkeypatch=monkeypatch)

    assert result.kind == KIND_INFERENCE
    assert result.evidence_missing is False


# ======================================================
# 5. The resolution trace
# ======================================================
def test_the_trace_is_present_when_asked_for(search_tool, monkeypatch):
    trace = run(cloud=False, debug=True, monkeypatch=monkeypatch).model_resolution_trace

    assert set(trace) == {
        "initial", "provider_router", "post_resolution", "capability_routing", "final",
    }
    # Automatic resolves to None and deliberately stays there -- see
    # test_automatic_mode_defers_to_the_router_and_says_so.
    assert trace["initial"] is None
    assert trace["post_resolution"] is None
    assert trace["capability_routing"] == routing.ROUTE_NORMAL


def test_the_trace_is_absent_by_default(search_tool, monkeypatch):
    assert run(cloud=False, debug=False, monkeypatch=monkeypatch).model_resolution_trace == {}


def test_the_trace_admits_what_it_cannot_see(search_tool, monkeypatch):
    """provider_router resolves after the orchestrator has returned.

    Recording a guess there would make the trace worse than useless to
    whoever is reading it to find out what happened.
    """
    trace = run(cloud=False, debug=True, monkeypatch=monkeypatch).model_resolution_trace
    assert trace["provider_router"] is None


def test_an_evidence_missing_turn_is_traced_too(broken_search_tool, monkeypatch):
    """The turn most worth debugging is the one that refused to answer."""
    trace = run(cloud=False, debug=True, monkeypatch=monkeypatch).model_resolution_trace
    assert trace, "the refusal path dropped the trace"
    assert "capability_routing" in trace
