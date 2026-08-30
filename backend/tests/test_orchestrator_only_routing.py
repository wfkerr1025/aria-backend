# backend/tests/test_orchestrator_only_routing.py
#
# The rules that keep the Turn Orchestrator the only router.
#
# Extracting a shared sequence is easy to do and easy to undo: a transport
# only has to import detect_intent "just for this one case" and there are
# two routers again, disagreeing quietly. The suites elsewhere check that
# the orchestrator decides correctly. This one checks that nothing else
# decides at all.
#
# Four things are enforced here:
#
#   1. no transport calls an orchestration primitive, and the four
#      direct-answer helpers no longer exist
#   2. a query that asks for a lookup actually runs one
#   3. what the lookup returned reaches the model as evidence
#   4. no turn answers from the retired bypass's canned text
#
# (2) and (3) are the hallucination bug stated as tests. It was never that
# the model lied without reason -- it was asked a question about the
# current price of something, given no evidence, and told to answer.

from __future__ import annotations

import ast
import pathlib

import pytest

from backend.core import turn_orchestrator
from backend.core.turn_orchestrator import orchestrate_turn
from backend.core.turn_types import KIND_INFERENCE, KIND_TEXT, SessionState, TurnRequest


REPO = pathlib.Path(__file__).resolve().parents[2]

TRANSPORTS = (
    REPO / "backend" / "websocket" / "handlers.py",
    REPO / "backend" / "rest" / "router.py",
)

# The decisions that make up a turn. Every one of these is the
# orchestrator's to make; a transport that calls one is making it again,
# separately, with no test comparing the two answers.
ORCHESTRATION_PRIMITIVES = frozenset({
    "detect_intent",
    "apply_history_policy",
    "evaluate_safety",
    "model_violates_mode_separation",
    "extract_weather_location",
    "run_search_tool",
    # Same rule, same reason -- these travelled with the four deleted
    # helpers and would bring the bypass back with them.
    "extract_search_query",
    "format_search_reply",
    "intent_hint",
    "resolve_self_query",
})

DELETED_HELPERS = (
    "_answer_tool_query_directly",
    "_answer_self_query_directly",
    "_answer_weather_intent_directly",
    "_ask_weather_clarification",
)


def _tree(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _called_names(tree: ast.Module) -> set[str]:
    """Every name this module calls, however it reached it.

    AST rather than a text search, so a primitive named in a comment or a
    docstring -- and this codebase explains itself at length in both --
    does not read as a call. What is being asserted is behaviour, and only
    a Call node is behaviour.
    """
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _imported_names(tree: ast.Module) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names


# ======================================================
# 1. Orchestrator-only routing
# ======================================================
@pytest.mark.parametrize("path", TRANSPORTS, ids=lambda p: p.name)
def test_no_transport_calls_an_orchestration_primitive(path):
    called = _called_names(_tree(path))
    offenders = sorted(called & ORCHESTRATION_PRIMITIVES)
    assert not offenders, (
        f"{path.name} calls {offenders} directly. Every one of these is a routing "
        f"decision the orchestrator makes; calling it here makes it twice."
    )


@pytest.mark.parametrize("path", TRANSPORTS, ids=lambda p: p.name)
def test_no_transport_even_imports_an_orchestration_primitive(path):
    """Stronger than the call check, and deliberately so.

    An unused import of detect_intent is an invitation: the next person
    adding a branch finds it already in scope and uses it. The primitives
    should not be reachable from a transport at all.
    """
    imported = _imported_names(_tree(path))
    offenders = sorted(imported & ORCHESTRATION_PRIMITIVES)
    assert not offenders, f"{path.name} imports {offenders}; only the orchestrator should."


@pytest.mark.parametrize("path", TRANSPORTS, ids=lambda p: p.name)
def test_the_direct_answer_helpers_are_gone(path):
    defined = {
        node.name for node in ast.walk(_tree(path))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    survivors = sorted(set(DELETED_HELPERS) & defined)
    assert not survivors, f"{path.name} still defines {survivors}"


def test_the_orchestrator_is_the_only_caller_of_the_primitives():
    """The other half: they are still called, just in one place."""
    called = _called_names(_tree(REPO / "backend" / "core" / "turn_orchestrator.py"))
    for primitive in ("detect_intent", "apply_history_policy", "evaluate_safety",
                      "model_violates_mode_separation", "extract_weather_location"):
        assert primitive in called, f"the orchestrator stopped calling {primitive}"


# ======================================================
# 2 + 3. A lookup request runs a lookup, and it reaches the model
# ======================================================
# The five phrasings that must never be answered from a model's weights.
# Every one is a question whose true answer changes after the weights were
# frozen, so a fluent answer with no lookup behind it is a fabrication --
# which is the shape the original report took ("MSFT is $120.45").
LOOKUP_QUERIES = [
    "search the latest world news",
    "look up the pytest release notes",
    "what is the current price of Microsoft stock",
    "what is the stock price of MSFT",
    "how much is MSFT trading at",
]


@pytest.fixture
def search_tool():
    """A web_search double, registered where the plan will reach for it."""
    from backend.core import tool_registry as core_reg

    calls = []

    def fake_search(query):
        calls.append(query)
        return {"raw": {}, "reply": f"SENTINEL-EVIDENCE for {query}"}

    core_reg.register_tool(
        core_reg.ToolSchema(
            name="web_search", description="test double",
            parameters={"query": {"type": "string", "required": True}},
            permission=core_reg.PERMISSION_NETWORK,
        ),
        fake_search,
    )
    try:
        yield calls
    finally:
        core_reg.register_builtin_tools()


def run(text: str):
    request = TurnRequest(
        messages=[{"role": "user", "content": text}],
        latest_user_text=text,
        conversation_id="c1",
        session=SessionState(mode="local"),
    )
    return orchestrate_turn(request, default_local_model=lambda: "test-local-model")


@pytest.mark.parametrize("query", LOOKUP_QUERIES)
def test_a_lookup_query_runs_the_search_tool(query, search_tool):
    """Requirement: the search bypass can never reappear.

    Two failures are being excluded at once. The turn must reach a model
    (so no bypass answers it from the tool alone), and web_search must
    appear in tool_runs (so the model is not answering unaided). Before
    the routing and planning phrase lists were merged, these queries
    satisfied the first and failed the second -- classified as needing the
    web, then answered without it.
    """
    result = run(query)
    assert result.kind == KIND_INFERENCE
    assert "web_search" in result.metadata["tool_runs"], (
        f"{query!r} produced no web_search run; tool_runs="
        f"{result.metadata['tool_runs']}"
    )
    assert search_tool, "web_search was reported as run but the tool never executed"


@pytest.mark.parametrize("query", LOOKUP_QUERIES)
def test_a_lookup_query_puts_its_results_in_the_prompt(query, search_tool):
    """Requirement: search queries must produce a synthesis prompt with evidence.

    A tool that ran and whose output never reached the prompt is no better
    than one that did not run: the model still answers from its weights.
    The sentinel is what proves the round trip.

    The section heading is not asserted: which prompt the turn gets now
    depends on the model. A model that cannot be trusted to read evidence
    is handed a simplified prompt instead of the full synthesis document
    (backend/core/evidence_routing.py), and these fixtures use a model
    that is not on that allowlist. What has to hold either way -- and what
    this test is actually about -- is that the lookup's result reaches the
    model at all.
    """
    result = run(query)
    prompt = result.inference_request.messages[-1].content

    assert "SENTINEL-EVIDENCE" in prompt, (
        "the search ran but its result never reached the model"
    )


def test_a_local_scope_query_never_reaches_the_web(search_tool):
    """The veto half, which the widened phrase list makes load-bearing.

    "search my notes" contains the strongest search word there is and must
    still never leave the machine -- the answer is local, and sending the
    query out would leak private text to answer a question that was never
    about the web.
    """
    result = run("search my notes for the shader error")
    assert "web_search" not in result.metadata.get("tool_runs", ())
    assert not search_tool, "a local-scope query reached the web"


# ======================================================
# 4. No fallback to the deleted helpers' answers
# ======================================================
# Text only the retired bypass produced. It reached the user verbatim, as
# the whole reply, with no model involved -- so finding any of it in a
# result means the bypass is back.
BYPASS_TEXT = (
    "I couldn't search for that",
    "I searched, but didn't get a clear answer back",
)


@pytest.mark.parametrize("query", LOOKUP_QUERIES)
def test_no_turn_answers_with_the_retired_bypass_text(query, search_tool):
    result = run(query)

    assert result.model_id != turn_orchestrator.SEARCH_MODEL, (
        "the 'search' sentinel model id is the retired bypass's signature"
    )
    assert not (result.kind == KIND_TEXT and result.text), (
        f"{query!r} was answered without a model: {result.text!r}"
    )
    for fragment in BYPASS_TEXT:
        assert fragment not in (result.text or "")
        assert fragment not in (
            result.inference_request.messages[-1].content
            if result.inference_request else ""
        )


# ======================================================
# 5. The rules above are only enforced if the tests run
# ======================================================
def test_pytest_collects_both_naming_conventions():
    """A suite nobody runs enforces nothing.

    This used to check a hand-maintained list in run_all_tests.py, which
    was the wrong guard for the right worry: the list was the thing that
    went stale. Thirty-three *_tests.py suites were named in it and still
    never collected by pytest, because pytest's default python_files is
    "test_*.py *_test.py" and *_tests.py matches neither. Roughly five
    hundred assertions did not execute, and inside them sat four that had
    been contradicting shipped behaviour for months plus one real routing
    bug.

    The registry is gone. Collection is the guard now, so this checks the
    configuration that makes collection complete -- and it reads what
    pytest itself resolved, not the file, so a config moved to
    pyproject.toml or overridden on the command line still has to satisfy
    it.
    """
    import shlex

    patterns = pytestconfig_patterns()
    assert "test_*.py" in patterns and "*_tests.py" in patterns, (
        f"pytest is configured to collect {patterns}. Both naming "
        f"conventions are in use in backend/tests/, so both must be "
        f"collected or a suite silently stops running."
    )
    del shlex


def pytestconfig_patterns():
    """What pytest will actually treat as a test file, as it resolved it."""
    import _pytest.config

    config = _pytest.config.get_config()
    config.parse([])
    return list(config.getini("python_files"))


def test_every_suite_in_the_directory_is_collectable():
    """Named by the same rules pytest uses, and importable.

    A file that matches neither pattern is invisible; a file that matches
    one but cannot be imported is an ERROR that a "N passed" line does
    not mention.
    """
    import fnmatch

    tests_dir = pathlib.Path(__file__).resolve().parent
    patterns = pytestconfig_patterns()

    # Modules that hold fixtures or helpers, plus the pytest wrapper.
    helpers = {"run_all_tests.py", "__init__.py", "conftest.py",
               "_sandbox_subprocess_fixtures.py", "phase3_upgrade_helpers.py"}

    uncollected = sorted(
        path.name for path in tests_dir.glob("*.py")
        if path.name not in helpers
        and not any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns)
    )
    assert not uncollected, (
        f"{uncollected} are in backend/tests/ but match none of {patterns}, "
        f"so pytest will never collect them and nothing will run them."
    )


def test_the_runner_is_a_wrapper_and_not_a_second_source_of_truth():
    """run_all_tests.py must not hold a list that can disagree with pytest.

    It also must not run anything on import: it matches "*_tests.py", so
    pytest collects this very file, and an unguarded runner would import,
    run the whole suite, and collect itself again. That hung for four
    minutes before the guard existed.
    """
    import ast

    tests_dir = pathlib.Path(__file__).resolve().parent
    source = (tests_dir / "run_all_tests.py").read_text(encoding="utf-8")
    # Parsed, not grepped. The file EXPLAINS why those lists are gone, so
    # a substring search finds its own documentation and fails -- which
    # is the recurring way source assertions go wrong in this repo.
    tree = ast.parse(source)

    assert 'if __name__ == "__main__":' in source, (
        "run_all_tests.py must not do its work on import; pytest collects it."
    )

    assigned = {
        target.id
        for node in tree.body if isinstance(node, ast.Assign)
        for target in node.targets if isinstance(target, ast.Name)
    }
    for obsolete in ("TESTS", "PYTEST_TESTS", "LEGACY_TESTS", "NETWORK_TESTS"):
        assert obsolete not in assigned, (
            f"{obsolete} is a second registry of what to run. pytest.ini "
            f"decides that now, and two answers is how the first one went stale."
        )


def test_the_bypass_survives_only_for_an_unhealthy_connection(search_tool):
    """The one exception, and the reason it is one.

    Whether the connection is up is a transport fact the reasoning core
    cannot see, and a turn that cannot be delivered should not spend a
    network call first.
    """
    request = TurnRequest(
        messages=[{"role": "user", "content": "search the latest world news"}],
        latest_user_text="search the latest world news",
        conversation_id="c1",
        session=SessionState(mode="local", connection_healthy=False),
    )
    result = orchestrate_turn(request, default_local_model=lambda: "test-local-model")
    assert result.kind == "error"
    assert not search_tool, "a refused turn still ran a search"
