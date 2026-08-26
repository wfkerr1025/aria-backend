# backend/tests/test_evidence_routing.py
#
# Matching the model to the turn when evidence is involved.
#
# Measured on this repo, same query, same stubbed lookup, the price sitting
# in the prompt:
#
#     nemo-12b / mistral-7b / gpt-4   answer from the evidence
#     phi-3-mini (3.8B)               "I'm unable to provide real-time stock
#                                      prices, but I can perform a web search"
#     qwen-0.5b (0.5B)                answers from it, then degenerates
#
# Note what that rules out. A parameter floor is the obvious rule and it
# does not work: phi-3-mini is 3.8B and clears any threshold qwen-0.5b
# fails, yet phi-3 is the one that ignores evidence. So this is an
# allowlist of observed capability, not a size test -- and the first test
# below pins that, because "just raise the minimum size" is the change
# someone will reach for next.

from __future__ import annotations

import pytest

from backend.core import evidence_routing as routing
from backend.core.evidence_routing import (
    ROUTE_CLOUD,
    ROUTE_NORMAL,
    ROUTE_RAW_EVIDENCE,
    ROUTE_SIMPLIFIED,
    SYNTHESIS_FULL,
    SYNTHESIS_RAW_EVIDENCE,
    SYNTHESIS_SIMPLIFIED,
)
from backend.core.turn_orchestrator import orchestrate_turn
from backend.core.turn_types import KIND_INFERENCE, KIND_TEXT, SessionState, TurnRequest


SMALL = "phi-3-mini-4k-instruct-q4"
CAPABLE = "mistral-7b-q4km"
EVIDENCE = "Microsoft (MSFT) is trading at $412.30 (source: example.com)"


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


def run(text, *, model=SMALL, mode="local", cloud=False, monkeypatch=None):
    """One turn, with the model and the deployment situation pinned."""
    from backend.core import turn_orchestrator as orch

    monkeypatch.setattr(
        orch.key_manager, "list_configured_providers",
        lambda: {"openai": cloud, "anthropic": False},
    )
    return orchestrate_turn(
        TurnRequest(
            messages=[{"role": "user", "content": text}],
            latest_user_text=text,
            conversation_id="c1",
            # Pinned rather than defaulted: outside Local Mode the default
            # resolves to None (ProviderRouter chooses), which is trusted
            # with evidence and so would never reach the routing under
            # test. skip_safety_check keeps the real gate -- which reads
            # live CPU and RAM -- from short-circuiting the turn on a busy
            # machine.
            session=SessionState(mode=mode, explicit_model_override=model),
            skip_safety_check=True,
        ),
        default_local_model=lambda: model,
    )


def prompt_of(result):
    return result.inference_request.messages[-1].content


# ======================================================
# The allowlist is capability, not size
# ======================================================
def test_the_offending_model_would_pass_a_parameter_floor():
    """Why this is an allowlist and not a threshold.

    phi-3-mini is 3.8B and clears a 2B floor comfortably; qwen-0.5b does
    not. But phi-3 is the model that ignores evidence and qwen is not, so
    a floor excludes the wrong one.
    """
    from backend.core.model_metadata import get_model_params
    from backend.core.model_registry import get_model

    assert get_model_params(get_model(SMALL)) >= 2_000_000_000
    assert not routing.model_can_synthesize_evidence(SMALL)


@pytest.mark.parametrize("model_id", ["nemo-12b-q5", "mistral-7b-q4km", None])
def test_capable_models_are_trusted_with_evidence(model_id):
    """None is cloud/Automatic routing, where a frontier model answers."""
    assert routing.model_can_synthesize_evidence(model_id)


@pytest.mark.parametrize("model_id", [SMALL, "qwen2.5-0.5b-instruct-q4_k_m"])
def test_small_models_are_not(model_id):
    assert not routing.model_can_synthesize_evidence(model_id)


# ======================================================
# is_evidence_bearing
# ======================================================
@pytest.mark.parametrize("tool", ["web_search", "file_lookup", "news_search", "product_search"])
def test_an_evidence_tool_makes_the_turn_evidence_bearing(tool):
    assert routing.is_evidence_bearing((tool,))


@pytest.mark.parametrize("runs", [(), ("get_weather",), ("run_tests",), None])
def test_everything_else_does_not(runs):
    assert not routing.is_evidence_bearing(runs)


# ======================================================
# A -- cloud fallback
# ======================================================
def test_a_small_model_with_cloud_available_hands_the_turn_to_cloud(search_tool, monkeypatch):
    result = run("what is the stock price of Microsoft",
                 model=SMALL, mode="automatic", cloud=True, monkeypatch=monkeypatch)

    assert result.kind == KIND_INFERENCE
    # model_id=None is how this codebase says "ProviderRouter chooses",
    # which in Automatic mode with a key configured means cloud. There is
    # no "cloud-default" id to assert instead: the registry holds no cloud
    # models by design.
    assert result.model_id is None
    assert result.synthesis_mode == SYNTHESIS_FULL
    assert any(e.get("decision") == ROUTE_CLOUD for e in result.telemetry)


def test_a_capable_local_model_is_left_alone_even_with_cloud_available(search_tool, monkeypatch):
    result = run("what is the stock price of Microsoft",
                 model=CAPABLE, mode="automatic", cloud=True, monkeypatch=monkeypatch)

    assert result.model_id == CAPABLE
    assert result.synthesis_mode == SYNTHESIS_FULL


def test_local_mode_never_routes_to_cloud_however_capable_it_would_be(search_tool, monkeypatch):
    """Absolute mode separation is not traded against answer quality.

    A user in Local Mode has said their data does not leave the machine.
    A better answer is not worth breaking that, so an evidence-bearing
    turn on a small local model is simplified, never sent out.
    """
    result = run("what is the stock price of Microsoft",
                 model=SMALL, mode="local", cloud=True, monkeypatch=monkeypatch)

    assert result.model_id == SMALL
    assert result.synthesis_mode == SYNTHESIS_SIMPLIFIED


# ======================================================
# B -- simplified local synthesis
# ======================================================
def test_local_only_with_a_small_model_simplifies_the_prompt(search_tool, monkeypatch):
    result = run("what is the stock price of Microsoft",
                 model=SMALL, mode="local", cloud=False, monkeypatch=monkeypatch)

    assert result.kind == KIND_INFERENCE
    assert result.model_id == SMALL
    assert result.synthesis_mode == SYNTHESIS_SIMPLIFIED
    assert any(e.get("decision") == ROUTE_SIMPLIFIED for e in result.telemetry)


def test_the_simplified_prompt_keeps_the_evidence_and_drops_the_scaffolding(search_tool, monkeypatch):
    """What a small model gets wrong is the structure, not the facts.

    The evidence summary, the plan, the conflict block and the eight
    synthesis rules are what a small model continues rather than follows.
    The findings are what it needs.
    """
    result = run("what is the stock price of Microsoft",
                 model=SMALL, mode="local", cloud=False, monkeypatch=monkeypatch)
    prompt = prompt_of(result)

    assert "$412.30" in prompt
    assert "Evidence Summary:" not in prompt
    assert "Synthesis Instructions:" not in prompt
    assert "Conflicts Detected:" not in prompt
    assert "Plan:" not in prompt


# ======================================================
# C -- raw evidence, no model
# ======================================================
def test_no_cloud_and_a_small_model_returns_the_evidence_itself(search_tool, monkeypatch):
    result = run("what is the stock price of Microsoft",
                 model=SMALL, mode="automatic", cloud=False, monkeypatch=monkeypatch)

    assert result.kind == KIND_TEXT
    assert result.synthesis_mode == SYNTHESIS_RAW_EVIDENCE
    assert result.inference_request is None, "no model may be asked to synthesize here"
    assert "$412.30" in result.text
    assert any(e.get("decision") == ROUTE_RAW_EVIDENCE for e in result.telemetry)


def test_the_raw_answer_says_why_it_is_raw(search_tool, monkeypatch):
    """A wall of search results with no explanation reads as a failure."""
    result = run("what is the stock price of Microsoft",
                 model=SMALL, mode="automatic", cloud=False, monkeypatch=monkeypatch)
    assert "summar" in result.text.lower()


def test_the_raw_path_still_ran_the_lookup(search_tool, monkeypatch):
    result = run("what is the stock price of Microsoft",
                 model=SMALL, mode="automatic", cloud=False, monkeypatch=monkeypatch)
    assert "web_search" in result.metadata["tool_runs"]


# ======================================================
# D -- everything that is not evidence-bearing
# ======================================================
def test_a_weather_turn_is_untouched(monkeypatch):
    """The fusion engine answers it; no model, no evidence, no routing."""
    from backend.core import turn_orchestrator as orch
    from backend.core import weather_nl

    monkeypatch.setattr(weather_nl, "resolve_weather_reply",
                        lambda location: (f"Weather for {location}: 12C.", None))
    result = run("what is the weather in Paris",
                 model=SMALL, mode="local", cloud=False, monkeypatch=monkeypatch)

    assert result.kind == KIND_TEXT
    assert result.synthesis_mode == SYNTHESIS_FULL
    assert result.model_id == weather_nl.WEATHER_MODEL_SENTINEL


def test_a_self_knowledge_turn_is_untouched(monkeypatch):
    result = run("what model are you running", model=SMALL, mode="local",
                 cloud=False, monkeypatch=monkeypatch)
    assert result.kind == KIND_TEXT
    assert result.model_id == "skr"
    assert result.synthesis_mode == SYNTHESIS_FULL


def test_a_trivial_turn_is_untouched(monkeypatch):
    result = run("hello", model=SMALL, mode="local", cloud=False, monkeypatch=monkeypatch)
    assert result.kind == KIND_INFERENCE
    assert result.model_id == SMALL
    assert result.synthesis_mode == SYNTHESIS_FULL


def test_an_ordinary_question_on_a_small_model_is_untouched(monkeypatch):
    """No lookup, no evidence, nothing to be incapable of reading."""
    result = run("explain the build pipeline", model=SMALL, mode="local",
                 cloud=False, monkeypatch=monkeypatch)
    assert result.kind == KIND_INFERENCE
    assert result.model_id == SMALL
    assert result.synthesis_mode == SYNTHESIS_FULL


def test_a_model_switch_turn_is_untouched(monkeypatch):
    from backend.core import turn_orchestrator as orch
    from backend.core.turn_types import KIND_MODEL_SWITCH

    monkeypatch.setattr(orch, "detect_model_switch_target", lambda text: "mistral")
    monkeypatch.setattr(orch, "resolve_model_switch_target",
                        lambda target: {"kind": "model", "model_id": CAPABLE})
    result = run("switch to mistral", model=SMALL, mode="local",
                 cloud=False, monkeypatch=monkeypatch)

    assert result.kind == KIND_MODEL_SWITCH
    assert result.synthesis_mode == SYNTHESIS_FULL


# ======================================================
# The coupling to Engine B's prompt format
# ======================================================
def test_the_tool_results_section_is_still_where_we_read_it_from():
    """Options B and C read the findings back out of the assembled prompt.

    That is a real coupling to synthesis_prompt's section format, chosen
    over running the tools a second time. Pinned here so a change to the
    heading fails loudly instead of silently yielding no evidence.
    """
    from backend.aria_synthesis.bundle_builder import build_evidence_bundle
    from backend.aria_synthesis.synthesis_prompt import build_synthesis_prompt
    from backend.tools.tool_registry import STATUS_OK, ToolResult

    bundle = build_evidence_bundle("q", [], now="FIXED")
    prompt = build_synthesis_prompt(
        "q", bundle, [], None, None, None,
        [ToolResult("web_search", "step1", STATUS_OK, EVIDENCE)],
    )
    extracted = routing.extract_tool_results(prompt)

    assert extracted, "the Tool Results section could not be found"
    assert any("$412.30" in line for line in extracted)


def test_extraction_stops_at_the_end_of_the_section():
    prompt = (
        "Evidence Summary: something\n\n"
        "Tool Results:\n"
        "- web_search (step1): the finding\n"
        "\n"
        "Synthesis Instructions:\n"
        "1. Use ONLY the evidence above.\n"
    )
    assert routing.extract_tool_results(prompt) == ["web_search (step1): the finding"]


def test_extraction_of_a_prompt_with_no_tools_is_empty():
    assert routing.extract_tool_results("User Query: hi\n\nNotes:\n(none)\n") == []


def test_raw_evidence_with_nothing_to_show_says_so():
    text = routing.format_raw_evidence([])
    assert "nothing usable" in text
