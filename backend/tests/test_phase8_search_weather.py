# backend/tests/test_phase8_search_weather.py
#
# Phase 8.1: the search and weather plan kinds.
#
# These are the first plan steps that reach the network. Both tools are
# PERMISSION_NETWORK, and a search step puts the user's own words on the wire
# to a third party -- so the detection rules are tested far harder for what
# they refuse than for what they catch. A false positive here is not a wrong
# answer, it is an unasked-for outbound request carrying private text.
#
# Nothing in this file makes a real network call. Every execution test
# replaces the registered handler, so the assertions are about wiring rather
# than about whether a provider was reachable today.

from __future__ import annotations

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory
from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.aria_memory import memory_ranking
from backend.aria_synthesis.bundle_builder import build_evidence_bundle
from backend.aria_synthesis.synthesis_engine import answer_with_evidence
from backend.aria_synthesis.synthesis_prompt import build_synthesis_prompt
from backend.context.goal_state import GoalState
from backend.core import tool_registry as core_registry
from backend.core.tool_registry import PERMISSION_NETWORK, get_tool_schema
from backend.files import file_ingestion as ingestion
from backend.planning.plan import (
    CONVERSATION_TARGET,
    KIND_ANALYZE,
    KIND_ANSWER,
    KIND_READ,
    KIND_SEARCH,
    KIND_SUMMARIZE,
    KIND_WEATHER,
    KINDS,
    LOOKUP_KINDS,
    Plan,
    PlanStep,
)
from backend.planning.plan_builder import (
    LOCAL_SCOPE_WORDS,
    SEARCH_WORDS,
    WEATHER_WORDS,
    PlanBuilder,
)
from backend.tools.tool_executor import ToolExecutor, summarize
from backend.tools.tool_registry import (
    STATUS_ERROR,
    STATUS_OK,
    TOOLS,
    ToolInvocation,
    ToolResult,
    tool_for_kind,
)
from backend.tools.tool_router import ToolRouter

BUILDER = PlanBuilder()
ROUTER = ToolRouter()
EXECUTOR = ToolExecutor()


def bundle_of(items=None, query="q"):
    return build_evidence_bundle(query, items or [], now="FIXED")


def plan_for(query, items=None, goal=None) -> Plan:
    return BUILDER.build(query, bundle_of(items, query), "general", goal)


def chunk(file_id: int, path: str, score: float = 0.8) -> dict:
    return {"type": "file_chunk", "file_id": file_id, "path": path,
            "section": "S", "text": "content", "combined_score": score}


TWO_FILES = [chunk(1, "docs/build.md", 0.9), chunk(2, "docs/deploy.md", 0.8)]


@pytest.fixture
def no_network(monkeypatch):
    """Replace both network handlers so no test can reach the wire.

    Returns the call log, so a test can assert what the tool was asked for
    without asserting anything about what the internet said.
    """
    calls = []

    def fake_weather(location: str) -> dict:
        calls.append(("weather", location))
        return {"location": location, "temperature": "12C",
                "conditions": "cloudy", "provider": "test-provider"}

    def fake_search(query: str) -> dict:
        calls.append(("web_search", query))
        return {"raw": {"results": []}, "reply": f"three results for {query}"}

    monkeypatch.setattr(core_registry, "_weather_handler", fake_weather)
    monkeypatch.setattr(core_registry, "_search_handler", fake_search)
    core_registry.register_tool(
        core_registry.ToolSchema(
            name="weather", description="test double",
            parameters={"location": {"type": "string", "required": True}},
            permission=PERMISSION_NETWORK,
        ),
        fake_weather,
    )
    core_registry.register_tool(
        core_registry.ToolSchema(
            name="web_search", description="test double",
            parameters={"query": {"type": "string", "required": True}},
            permission=PERMISSION_NETWORK,
        ),
        fake_search,
    )
    yield calls
    core_registry.register_builtin_tools()


# ======================================================
# FIX 1 - the kinds exist and change nothing else
# ======================================================
def test_the_new_kinds_are_known():
    assert KIND_SEARCH in KINDS
    assert KIND_WEATHER in KINDS
    assert LOOKUP_KINDS == (KIND_WEATHER, KIND_SEARCH)


def test_the_existing_kinds_are_untouched():
    for kind in ("read", "analyze", "edit", "test", "summarize", "answer"):
        assert kind in KINDS


def test_a_step_without_args_is_still_valid():
    # The new field is additive: a five-field PlanStep is what every earlier
    # phase builds, and it must keep working unchanged.
    step = PlanStep(id="step1", kind=KIND_READ, target="build.md", description="d")
    assert step.args == {}
    assert step.line == "read build.md: d"


# ======================================================
# FIX 2 - detection
# ======================================================
@pytest.mark.parametrize("query,location", [
    ("what is the weather in Paris", "Paris"),
    ("forecast for Oslo", "Oslo"),
    ("the temperature at Heathrow", "Heathrow"),
    ("what's the weather in Richmond VA", "Richmond VA"),
])
def test_a_weather_question_plans_a_weather_step(query, location):
    steps = plan_for(query).of_kind(KIND_WEATHER)
    assert steps
    assert steps[0].args == {"location": location}
    assert steps[0].target == location


@pytest.mark.parametrize("query,terms", [
    ("search the web for pytest release notes", "pytest release notes"),
    ("look up the release notes for pytest", "release notes for pytest"),
    ("google the current version of numpy", "current version of numpy"),
])
def test_a_lookup_question_plans_a_search_step(query, terms):
    steps = plan_for(query).of_kind(KIND_SEARCH)
    assert steps
    assert steps[0].args == {"query": terms}


def test_a_weather_question_with_no_location_plans_nothing():
    # The registered tool needs a location and has no useful behaviour
    # without one, so the model asks which city rather than the plan
    # emitting a call that can only fail validation.
    assert plan_for("what is the weather").is_single_answer


@pytest.mark.parametrize("query", [
    "explain the build pipeline",
    "the latest build failed",
    "why is the shader broken",
    "summarize the deployment process",
])
def test_an_ordinary_question_plans_no_lookup(query):
    plan = plan_for(query)
    assert not plan.of_kind(KIND_SEARCH)
    assert not plan.of_kind(KIND_WEATHER)


# --- the guard that matters most ---
@pytest.mark.parametrize("query", [
    "search my notes for the pipeline",
    "look up my documents about the build",
    "search for the shader error in my files",
    "look up this repo for the config",
    "search for it in the codebase",
])
def test_a_local_lookup_never_becomes_a_web_search(query):
    # A question about the user's own material is a retrieval question
    # however it is phrased. Sending these words to a search engine would
    # both miss the answer and hand private text to a third party.
    assert not plan_for(query).of_kind(KIND_SEARCH)


def test_local_scope_also_suppresses_weather():
    assert not plan_for("what does my notes file say about the forecast").of_kind(KIND_WEATHER)


def test_a_bare_search_word_does_not_over_match():
    """
    This used to require every entry to be a phrase, on the reasoning that
    "search" alone catches half the questions anyone asks a memory system.

    The requirement was the wrong shape for the risk. SEARCH_WORDS was the
    planner's list and conversation_manager's TOOL_KEYWORDS was the
    router's; the router's had been widened to catch bare imperatives and
    this one had not, so a query could route as a search and then plan no
    search step -- classified as needing the web, then answered from the
    model's weights. Both now come from backend/core/search_intent.py.

    What actually keeps a bare word safe is the two properties asserted
    here: matching is whole-word, and a query about the user's own
    material is vetoed outright. Those are the guarantees; "every entry
    contains a space" was only ever a proxy for them.
    """
    from backend.planning.plan_builder import PlanBuilder

    builder = PlanBuilder()

    # Whole-word: a word that merely contains a search word is not one.
    for query in ("research the history of Rome",
                  "the searchable index is stale",
                  "he googled it yesterday"):
        assert builder.search_step(query, 1) is None, query

    # The veto: the strongest search word there is, still local.
    for query in ("search my notes for the shader error",
                  "search this repo for the failing assert",
                  "look up what we discussed about the pipeline"):
        assert builder.search_step(query, 1) is None, query

    # And the bare imperative that made the widening necessary.
    assert builder.search_step("search the latest world news", 1) is not None


def test_the_weather_words_come_from_the_expansion_tables():
    from backend.llm.query_expansion import DOMAIN_MARKERS, ExpansionDomain

    assert WEATHER_WORDS is DOMAIN_MARKERS[ExpansionDomain.WEATHER]


def test_local_scope_words_are_not_empty():
    assert LOCAL_SCOPE_WORDS


# --- plan shape ---
def test_a_lookup_only_plan_ends_in_a_terminal_step():
    plan = plan_for("what is the weather in Paris")
    assert plan.kinds == [KIND_WEATHER, KIND_SUMMARIZE]
    assert plan.steps[1].depends_on == ["step1"]


def test_a_lookup_plan_is_not_a_single_answer():
    assert not plan_for("what is the weather in Paris").is_single_answer


def test_lookups_come_before_reads():
    plan = plan_for("search the web for pytest release notes", TWO_FILES)
    assert plan.kinds[0] == KIND_SEARCH
    assert plan.kinds[1] == KIND_READ


def test_the_analysis_waits_on_the_lookup_too():
    # A comparison that ran before the search came back would be comparing
    # the documents against nothing.
    plan = plan_for("search the web for pytest release notes", TWO_FILES)
    analyze = plan.of_kind(KIND_ANALYZE)[0]
    assert plan.of_kind(KIND_SEARCH)[0].id in analyze.depends_on


def test_dependencies_still_resolve():
    for query in ("what is the weather in Paris",
                  "search the web for pytest release notes",
                  "search the web for pytest and fix the docs"):
        assert plan_for(query, TWO_FILES).dependencies_resolve
        assert plan_for(query).dependencies_resolve


def test_weather_is_ordered_before_search():
    plan = plan_for("what is the weather in Paris, and look up the forecast model docs")
    kinds = [kind for kind in plan.kinds if kind in LOOKUP_KINDS]
    assert kinds == sorted(kinds, key=LOOKUP_KINDS.index)


def test_step_ids_stay_sequential_with_lookups():
    plan = plan_for("search the web for pytest release notes", TWO_FILES)
    assert [step.id for step in plan.steps] == [f"step{n}" for n in range(1, len(plan) + 1)]


def test_detection_is_deterministic():
    plans = [plan_for("what is the weather in Paris") for _ in range(5)]
    assert all(plan == plans[0] for plan in plans)


def test_detection_makes_no_model_calls(monkeypatch):
    from backend.llm import semantic_embeddings

    def explode(*args, **kwargs):
        raise AssertionError("plan detection must not call a model")

    monkeypatch.setattr(semantic_embeddings, "embed_texts_semantic", explode)
    monkeypatch.setattr(semantic_embeddings, "embed_text_semantic", explode)
    assert plan_for("what is the weather in Paris").of_kind(KIND_WEATHER)


# ======================================================
# FIX 3 - routing
# ======================================================
def test_a_search_step_routes_to_web_search():
    plan = plan_for("search the web for pytest release notes")
    invocations = ROUTER.route(plan)
    assert [i.tool_name for i in invocations] == ["web_search"]
    assert invocations[0].args == {"query": "pytest release notes"}


def test_a_weather_step_routes_to_the_weather_tool():
    invocations = ROUTER.route(plan_for("what is the weather in Paris"))
    assert [i.tool_name for i in invocations] == ["weather"]
    assert invocations[0].args == {"location": "Paris"}


@pytest.mark.parametrize("kind,name", [(KIND_SEARCH, "web_search"), (KIND_WEATHER, "weather")])
def test_each_new_kind_maps_to_its_tool(kind, name):
    assert tool_for_kind(kind).name == name


def test_a_lookup_step_missing_its_argument_is_not_routed():
    # Calling a network tool on an invented argument is worse than not
    # calling it, so a malformed step is dropped rather than guessed at.
    bare = Plan([PlanStep(id="step1", kind=KIND_WEATHER, target="x", description="d")])
    assert ROUTER.route(bare) == []
    empty = Plan([
        PlanStep(id="step1", kind=KIND_SEARCH, target=CONVERSATION_TARGET,
                 description="d", args={"query": ""})
    ])
    assert ROUTER.route(empty) == []


def test_the_goal_does_not_change_lookup_routing():
    plan = plan_for("what is the weather in Paris")
    goal = GoalState(goal="fix the shader error", topic="unity", explicit=True)
    assert ROUTER.route(plan) == ROUTER.route(plan, goal)


def test_routing_is_deterministic():
    plan = plan_for("search the web for pytest release notes", TWO_FILES)
    assert ROUTER.route(plan) == ROUTER.route(plan)


def test_routing_makes_no_model_calls(monkeypatch):
    from backend.llm import semantic_embeddings

    monkeypatch.setattr(
        semantic_embeddings, "embed_text_semantic",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model in routing")),
    )
    assert ROUTER.route(plan_for("what is the weather in Paris"))


def test_the_new_tools_are_declared_in_the_planning_registry():
    assert {"web_search", "weather"} <= set(TOOLS)


def test_the_new_tools_are_registered_in_the_core_registry():
    for name in ("web_search", "weather"):
        assert get_tool_schema(name) is not None
        assert get_tool_schema(name).permission == PERMISSION_NETWORK


# ======================================================
# FIX 4 - execution
# ======================================================
def test_a_weather_invocation_executes(no_network):
    results = EXECUTOR.execute(ROUTER.route(plan_for("what is the weather in Paris")))
    assert [r.status for r in results] == [STATUS_OK]
    assert no_network == [("weather", "Paris")]


def test_a_search_invocation_executes(no_network):
    results = EXECUTOR.execute(
        ROUTER.route(plan_for("search the web for pytest release notes"))
    )
    assert [r.status for r in results] == [STATUS_OK]
    assert no_network == [("web_search", "pytest release notes")]


def test_the_result_carries_a_payload(no_network):
    result = EXECUTOR.execute(ROUTER.route(plan_for("forecast for Oslo")))[0]
    assert isinstance(result, ToolResult)
    assert result.payload["conditions"] == "cloudy"


def test_a_failing_lookup_becomes_an_error_result(monkeypatch):
    def explode(location: str) -> dict:
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(core_registry, "_weather_handler", explode)
    core_registry.register_tool(
        core_registry.ToolSchema(
            name="weather", description="failing double",
            parameters={"location": {"type": "string", "required": True}},
            permission=PERMISSION_NETWORK,
        ),
        explode,
    )
    try:
        result = EXECUTOR.execute(ROUTER.route(plan_for("forecast for Oslo")))[0]
        assert result.status == STATUS_ERROR
        assert "unreachable" in result.summary or result.summary
    finally:
        core_registry.register_builtin_tools()


def test_a_lookup_is_not_silently_dropped(no_network):
    invocations = ROUTER.route(plan_for("what is the weather in Paris"))
    assert len(EXECUTOR.execute(invocations)) == len(invocations)


def test_execution_makes_no_model_calls(no_network, monkeypatch):
    from backend.llm import semantic_embeddings

    def explode(*args, **kwargs):
        raise AssertionError("tool execution must not call a model")

    monkeypatch.setattr(semantic_embeddings, "embed_texts_semantic", explode)
    monkeypatch.setattr(semantic_embeddings, "embed_text_semantic", explode)
    assert EXECUTOR.execute(ROUTER.route(plan_for("forecast for Oslo")))[0].status == STATUS_OK


# --- summaries ---
def test_a_weather_summary_reports_the_reading():
    line = summarize("weather", {"location": "Paris", "temperature": "12C",
                                 "conditions": "cloudy", "provider": "noaa"})
    assert "Paris" in line and "12C" in line and "noaa" in line


def test_a_weather_summary_with_no_reading_says_so():
    line = summarize("weather", {"location": "Paris"})
    assert "no reading returned" in line
    assert "None" not in line


def test_a_search_summary_uses_the_reply():
    assert summarize("web_search", {"reply": "three results for pytest"}) == (
        "three results for pytest"
    )


def test_a_search_summary_with_no_reply_says_so():
    assert summarize("web_search", {"raw": {}}) == "search returned no summary"


# ======================================================
# FIX 5 - prompt integration
# ======================================================
def test_lookup_results_render_like_any_other(no_network):
    results = EXECUTOR.execute(ROUTER.route(plan_for("what is the weather in Paris")))
    prompt = build_synthesis_prompt("q", bundle_of(), [], None, None, None, results)
    assert "Tool Results:" in prompt
    assert "- weather (step1): Paris: 12C, cloudy (via test-provider)" in prompt


def test_a_search_result_renders(no_network):
    results = EXECUTOR.execute(
        ROUTER.route(plan_for("search the web for pytest release notes"))
    )
    prompt = build_synthesis_prompt("q", bundle_of(), [], None, None, None, results)
    assert "- web_search (step1): three results for pytest release notes" in prompt


def test_a_lookup_that_did_not_run_is_omitted():
    from backend.tools.tool_registry import STATUS_NOT_EXECUTED

    results = [ToolResult("weather", "step1", STATUS_NOT_EXECUTED, "skipped")]
    assert "Tool Results" not in build_synthesis_prompt(
        "q", bundle_of(), [], None, None, None, results
    )


def test_a_failed_lookup_is_shown_and_flagged():
    results = [ToolResult("weather", "step1", STATUS_ERROR, "provider unreachable")]
    prompt = build_synthesis_prompt("q", bundle_of(), [], None, None, None, results)
    assert "- weather (step1): provider unreachable" in prompt
    assert "1 of these failed" in prompt


def test_the_plan_section_lists_the_lookup_steps(no_network):
    plan = plan_for("what is the weather in Paris")
    prompt = build_synthesis_prompt("q", bundle_of(), [], None, None, plan, None)
    assert "1. weather Paris:" in prompt


# ======================================================
# End to end
# ======================================================
def test_the_engine_runs_a_weather_lookup(no_network):
    seen = []
    answer = answer_with_evidence(
        "what is the weather in Paris", [],
        generate=lambda prompt: (seen.append(prompt), "an answer")[1],
    )
    assert answer == "an answer"
    assert no_network == [("weather", "Paris")]
    assert "Tool Results:" in seen[0]
    assert "12C" in seen[0]


def test_the_engine_makes_no_lookup_for_an_ordinary_question(no_network):
    # No evidence and no lookup: the empty-evidence guarantee is intact, the
    # model is never asked, and nothing reaches the network.
    from backend.aria_synthesis.synthesis_engine import NO_EVIDENCE_ANSWER

    seen = []
    answer = answer_with_evidence(
        "explain the build pipeline", [],
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert answer == NO_EVIDENCE_ANSWER
    assert seen == []
    assert no_network == []


def test_an_ordinary_question_with_evidence_still_calls_no_tool(no_network):
    seen = []
    answer_with_evidence(
        "explain the build pipeline", [chunk(1, "docs/build.md", 0.9)],
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert no_network == []
    assert "Tool Results" not in seen[0]


def test_a_failed_lookup_with_no_evidence_still_says_not_found(monkeypatch):
    # The guarantee enforced by outcome: a lookup that came back with
    # nothing leaves the query exactly as empty as it started.
    from backend.aria_synthesis.synthesis_engine import NO_EVIDENCE_ANSWER

    def explode(location: str) -> dict:
        raise RuntimeError("provider unreachable")

    core_registry.register_tool(
        core_registry.ToolSchema(
            name="weather", description="failing double",
            parameters={"location": {"type": "string", "required": True}},
            permission=PERMISSION_NETWORK,
        ),
        explode,
    )
    try:
        seen = []
        answer = answer_with_evidence(
            "what is the weather in Paris", [],
            generate=lambda prompt: (seen.append(prompt), "x")[1],
        )
        assert answer == NO_EVIDENCE_ANSWER
        assert seen == []
    finally:
        core_registry.register_builtin_tools()


def test_the_engine_makes_no_lookup_for_a_local_question(no_network):
    seen = []
    answer_with_evidence(
        "search my notes for the pipeline", [],
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert no_network == []


def test_the_whole_chain_is_deterministic(no_network):
    seen = []
    for _ in range(3):
        answer_with_evidence(
            "what is the weather in Paris", [],
            generate=lambda prompt: (seen.append(prompt), "x")[1],
        )
    assert all(prompt == seen[0] for prompt in seen)


def test_the_same_plan_routes_and_executes_identically(no_network):
    plan = plan_for("forecast for Oslo")
    first = EXECUTOR.execute(ROUTER.route(plan))
    second = EXECUTOR.execute(ROUTER.route(plan))
    assert first == second


# ======================================================
# Nothing else moved
# ======================================================
def test_the_existing_plan_kinds_still_behave(db):
    assert plan_for("explain the build pipeline", [chunk(1, "a.md", 0.9)]).is_single_answer
    multi = plan_for("explain how the docs relate", TWO_FILES)
    assert multi.of_kind(KIND_READ) and multi.of_kind(KIND_ANALYZE)


def test_an_ordinary_multi_document_plan_is_unchanged():
    plan = plan_for("explain how the docs relate", TWO_FILES)
    assert plan.kinds == [KIND_READ, KIND_READ, KIND_ANALYZE, KIND_SUMMARIZE]


def test_ranking_weights_are_unchanged():
    assert memory_ranking.WEIGHT_SEMANTIC == 0.45
    assert memory_ranking.WEIGHT_KEYWORD == 0.25
    assert memory_ranking.WEIGHT_RECENCY == 0.20
    assert memory_ranking.WEIGHT_TYPE == 0.10


def test_score_floors_are_unchanged():
    assert semantic.DEFAULT_MIN_SCORE == 0.6
    assert ingestion.DEFAULT_MIN_SCORE == 0.55


def test_unity_running_is_still_on_the_snapshot():
    from backend.core.resource_monitor import get_resource_snapshot

    assert isinstance(get_resource_snapshot().unity_running, bool)


def test_hybrid_output_is_unchanged(db, no_network):
    semantic.index_note(notes_store.save_note("the release pipeline runs nightly"))
    before = aria_memory.search_hybrid("release pipeline", min_score=0.0)
    EXECUTOR.execute(ROUTER.route(plan_for("forecast for Oslo")))
    after = aria_memory.search_hybrid("release pipeline", min_score=0.0)
    assert [i["keyword_score"] for i in before] == [i["keyword_score"] for i in after]
