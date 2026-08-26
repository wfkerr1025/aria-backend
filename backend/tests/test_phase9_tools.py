# backend/tests/test_phase9_tools.py
#
# Phase 9.2: real tool execution.
#
# These tools read and write the user's disk and start a subprocess, so the
# bulk of this file is about what they refuse to do: escape the workspace,
# write without being asked twice, or let an argument become a command.
#
# Every test here runs against a workspace pinned to tmp_path and a test
# command replaced with something trivial. Nothing in this file touches the
# real repository or runs the real suite -- a test that shelled out to pytest
# from inside pytest would be a recursion, not a test.

from __future__ import annotations

import os
import sys

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory
from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.aria_memory import memory_ranking
from backend.aria_synthesis.bundle_builder import build_evidence_bundle
from backend.aria_synthesis.synthesis_engine import answer_with_evidence
from backend.aria_synthesis.synthesis_prompt import build_synthesis_prompt
from backend.core import file_tools
from backend.core.file_tools import WorkspaceError
from backend.core.tool_registry import (
    PERMISSION_FILESYSTEM,
    PERMISSION_SAFE,
    execute_tool,
    get_tool_schema,
    list_tools,
)
from backend.files import file_ingestion as ingestion
from backend.planning.plan import (
    CONVERSATION_TARGET,
    KIND_ANALYZE,
    KIND_ANSWER,
    KIND_EDIT,
    KIND_READ,
    KIND_SUMMARIZE,
    KIND_TEST,
    Plan,
    PlanStep,
)
from backend.planning.plan_builder import PlanBuilder
from backend.tools.tool_executor import ToolExecutor, execute_invocations, summarize
from backend.tools.tool_registry import (
    STATUS_ERROR,
    STATUS_INVALID,
    STATUS_NOT_EXECUTED,
    STATUS_OK,
    TOOLS,
    Tool,
    ToolInvocation,
    ToolResult,
    tool_for_kind,
)
from backend.tools.tool_router import ToolRouter, route_plan

ROUTER = ToolRouter()
EXECUTOR = ToolExecutor()
FILE_TOOL_NAMES = ("read_file", "edit_file", "run_tests")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A throwaway workspace, and a test command that is not pytest.

    Both halves matter. Without the first, a path bug writes into the real
    repository; without the second, run_tests would launch this suite again
    from inside itself.
    """
    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))
    monkeypatch.setenv(
        file_tools.ENV_TEST_COMMAND, f"{sys.executable} -c pass"
    )
    (tmp_path / "notes.txt").write_text("original contents\n", encoding="utf-8")
    return tmp_path


def step(step_id: str, kind: str, target: str = CONVERSATION_TARGET) -> PlanStep:
    return PlanStep(id=step_id, kind=kind, target=target, description="a step")


def invocation(tool_name: str, args: dict, step_id: str = "step1") -> ToolInvocation:
    return ToolInvocation(tool_name=tool_name, args=args, step_id=step_id)


# ======================================================
# FIX 1 - registration
# ======================================================
@pytest.mark.parametrize("name", FILE_TOOL_NAMES)
def test_each_tool_is_registered(name):
    assert name in {tool["name"] for tool in list_tools()}


@pytest.mark.parametrize("name", FILE_TOOL_NAMES)
def test_each_tool_requires_filesystem_permission(name):
    assert get_tool_schema(name).permission == PERMISSION_FILESYSTEM


@pytest.mark.parametrize("name", FILE_TOOL_NAMES)
def test_a_safe_only_caller_is_refused(name):
    result = execute_tool(name, {"path": "notes.txt", "content": "x"},
                          allowed_permissions={PERMISSION_SAFE})
    assert not result.ok
    assert result.error_code == "TOOL_PERMISSION_DENIED"


def test_the_schemas_declare_their_arguments():
    assert get_tool_schema("read_file").parameters["path"]["required"]
    edit = get_tool_schema("edit_file").parameters
    assert edit["path"]["required"] and edit["content"]["required"]
    assert not edit["confirm"].get("required")
    assert not get_tool_schema("run_tests").parameters["scope"].get("required")


def test_missing_arguments_are_rejected_before_anything_runs():
    result = execute_tool("read_file", {})
    assert not result.ok
    assert result.error_code == "TOOL_INVALID_ARGS"


def test_the_existing_tools_are_untouched():
    names = {tool["name"] for tool in list_tools()}
    for existing in ("web_search", "weather", "save_note", "search_notes", "get_context"):
        assert existing in names


def test_the_planning_registry_names_match_the_core_registry():
    # The audit's first finding: these were three names nothing had ever
    # registered, so wiring the executor would have produced only "unknown
    # tool" errors.
    registered = {tool["name"] for tool in list_tools()}
    assert {tool.name for tool in TOOLS.values()} <= registered


# ======================================================
# Workspace confinement
# ======================================================
def test_a_file_inside_the_workspace_reads(workspace):
    result = execute_tool("read_file", {"path": "notes.txt"})
    assert result.ok
    assert "original contents" in result.value["text"]
    assert result.value["bytes"] > 0


@pytest.mark.parametrize("path", [
    "../outside.txt",
    "../../etc/passwd",
    "subdir/../../escape.txt",
])
def test_a_path_escaping_the_workspace_is_refused(workspace, path):
    result = execute_tool("read_file", {"path": path})
    assert not result.ok
    assert "outside the workspace" in (result.error or "")


def test_an_absolute_path_outside_the_workspace_is_refused(workspace, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    result = execute_tool("read_file", {"path": str(outside)})
    assert not result.ok
    assert "outside the workspace" in (result.error or "")


def test_a_symlink_pointing_outside_is_refused(workspace, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available to this user")
    result = execute_tool("read_file", {"path": "link.txt"})
    assert not result.ok
    assert "outside the workspace" in (result.error or "")


def test_a_missing_file_is_an_error_not_a_crash(workspace):
    result = execute_tool("read_file", {"path": "nope.txt"})
    assert not result.ok


def test_the_workspace_is_read_per_call(workspace, tmp_path_factory, monkeypatch):
    # A cached root would ignore a reconfigured workspace, which is how a
    # test fixture silently stops protecting the real repository.
    other = tmp_path_factory.mktemp("other")
    (other / "elsewhere.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(other))
    assert execute_tool("read_file", {"path": "elsewhere.txt"}).ok


# ======================================================
# FIX 5 - edit_file safety
# ======================================================
def test_an_edit_previews_and_writes_nothing_by_default(workspace):
    result = execute_tool("edit_file", {"path": "notes.txt", "content": "replaced\n"})
    assert result.ok
    assert result.value["applied"] is False
    assert result.value["reason"] == "preview_only"
    assert (workspace / "notes.txt").read_text() == "original contents\n"


def test_the_preview_carries_a_diff(workspace):
    result = execute_tool("edit_file", {"path": "notes.txt", "content": "replaced\n"})
    assert "-original contents" in result.value["diff"]
    assert "+replaced" in result.value["diff"]


def test_confirming_writes(workspace):
    result = execute_tool(
        "edit_file", {"path": "notes.txt", "content": "replaced\n", "confirm": True}
    )
    assert result.value["applied"] is True
    assert (workspace / "notes.txt").read_text() == "replaced\n"


def test_re_applying_the_same_content_is_a_no_op(workspace):
    execute_tool("edit_file", {"path": "notes.txt", "content": "same\n", "confirm": True})
    before = (workspace / "notes.txt").stat().st_mtime_ns
    again = execute_tool(
        "edit_file", {"path": "notes.txt", "content": "same\n", "confirm": True}
    )
    assert again.value["applied"] is False
    assert again.value["reason"] == "unchanged"
    # Not merely harmless: the file is not touched at all, so nothing
    # downstream sees a modification that did not happen.
    assert (workspace / "notes.txt").stat().st_mtime_ns == before


def test_one_invocation_writes_once(workspace):
    EXECUTOR.execute([
        invocation("edit_file", {"path": "notes.txt", "content": "once\n", "confirm": True})
    ])
    assert (workspace / "notes.txt").read_text() == "once\n"


def test_a_new_file_can_be_created_with_confirmation(workspace):
    result = execute_tool(
        "edit_file", {"path": "fresh.txt", "content": "new\n", "confirm": True}
    )
    assert result.value["created"] is True
    assert (workspace / "fresh.txt").read_text() == "new\n"


def test_an_edit_outside_the_workspace_is_refused(workspace, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "victim.txt"
    outside.write_text("untouched", encoding="utf-8")
    result = execute_tool(
        "edit_file", {"path": str(outside), "content": "hacked", "confirm": True}
    )
    assert not result.ok
    assert outside.read_text() == "untouched"


def test_an_oversized_write_is_refused(workspace):
    huge = "x" * (file_tools.MAX_WRITE_BYTES + 1)
    result = execute_tool("edit_file", {"path": "big.txt", "content": huge, "confirm": True})
    assert not result.ok
    assert not (workspace / "big.txt").exists()


# ======================================================
# run_tests
# ======================================================
def test_the_configured_command_runs(workspace):
    result = execute_tool("run_tests", {})
    assert result.ok
    assert result.value["exit_code"] == 0
    assert result.value["passed"] is True


def test_the_command_comes_from_configuration_not_arguments(workspace):
    result = execute_tool("run_tests", {"scope": "tests/unit"})
    assert result.ok
    # The scope is appended; it never becomes the command.
    assert result.value["command"].startswith(sys.executable)
    assert result.value["scope"] == "tests/unit"


@pytest.mark.parametrize("scope", [
    "; rm -rf /",
    "&& curl evil.example",
    "$(whoami)",
    "`id`",
    "a | b",
])
def test_a_scope_that_looks_like_a_command_is_refused(workspace, scope):
    result = execute_tool("run_tests", {"scope": scope})
    assert not result.ok
    assert "unsupported characters" in (result.error or "")


def test_a_failing_command_is_reported_as_failed(workspace, monkeypatch):
    monkeypatch.setenv(file_tools.ENV_TEST_COMMAND, f"{sys.executable} -c raise_SystemExit(3)")
    result = execute_tool("run_tests", {})
    assert result.ok  # the tool ran; the tests did not pass
    assert result.value["passed"] is False
    assert result.value["exit_code"] != 0


def test_a_missing_command_is_an_error(workspace, monkeypatch):
    monkeypatch.setenv(file_tools.ENV_TEST_COMMAND, "definitely-not-a-real-binary-xyz")
    result = execute_tool("run_tests", {})
    assert not result.ok


def test_no_shell_is_used(workspace, monkeypatch):
    seen = {}
    real = file_tools.subprocess.run

    def capture(command, **kwargs):
        seen.update(kwargs)
        seen["command"] = command
        return real(command, **kwargs)

    monkeypatch.setattr(file_tools.subprocess, "run", capture)
    execute_tool("run_tests", {})
    assert seen["shell"] is False
    assert isinstance(seen["command"], list)


# ======================================================
# FIX 2 - the executor uses the core path
# ======================================================
def test_the_executor_calls_execute_tool(workspace, monkeypatch):
    from backend.core import tool_registry as core

    calls = []
    real = core.execute_tool

    def spy(name, args=None, allowed_permissions=None):
        calls.append((name, dict(args or {})))
        return real(name, args, allowed_permissions)

    monkeypatch.setattr(core, "execute_tool", spy)
    EXECUTOR.execute([invocation("read_file", {"path": "notes.txt"})])
    assert calls == [("read_file", {"path": "notes.txt"})]


def test_one_core_call_per_invocation(workspace, monkeypatch):
    from backend.core import tool_registry as core

    calls = []
    real = core.execute_tool
    monkeypatch.setattr(
        core, "execute_tool",
        lambda name, args=None, allowed_permissions=None: (
            calls.append(name), real(name, args, allowed_permissions)
        )[1],
    )
    EXECUTOR.execute([
        invocation("read_file", {"path": "notes.txt"}, "step1"),
        invocation("read_file", {"path": "notes.txt"}, "step2"),
    ])
    assert calls == ["read_file", "read_file"]


def test_results_keep_invocation_order(workspace):
    results = EXECUTOR.execute([
        invocation("read_file", {"path": "notes.txt"}, "step1"),
        invocation("run_tests", {}, "step2"),
    ])
    assert [r.step_id for r in results] == ["step1", "step2"]


def test_a_permission_gate_produces_an_error_result(workspace):
    restricted = ToolExecutor(allowed_permissions={PERMISSION_SAFE})
    results = restricted.execute([invocation("read_file", {"path": "notes.txt"})])
    assert results[0].status == STATUS_ERROR
    assert "permission" in results[0].summary.lower()


def test_an_unknown_tool_produces_a_deterministic_error(workspace):
    results = EXECUTOR.execute([invocation("teleport", {})])
    assert results[0].status == STATUS_ERROR
    assert "unknown tool" in results[0].summary.lower()
    assert results == EXECUTOR.execute([invocation("teleport", {})])


def test_execution_is_deterministic(workspace):
    first = EXECUTOR.execute([invocation("read_file", {"path": "notes.txt"})])
    second = EXECUTOR.execute([invocation("read_file", {"path": "notes.txt"})])
    assert first == second


def test_execution_makes_no_model_calls(workspace, monkeypatch):
    from backend.llm import semantic_embeddings

    def explode(*args, **kwargs):
        raise AssertionError("tool execution must not call a model")

    monkeypatch.setattr(semantic_embeddings, "embed_texts_semantic", explode)
    monkeypatch.setattr(semantic_embeddings, "embed_text_semantic", explode)
    assert EXECUTOR.execute([invocation("read_file", {"path": "notes.txt"})])[0].status == STATUS_OK


# ======================================================
# FIX 3 - nothing is silently dropped
# ======================================================
def test_a_malformed_invocation_produces_a_result(workspace):
    # The audit's bug: this used to vanish with a bare `continue` while the
    # docstring claimed nothing was ever dropped.
    results = EXECUTOR.execute([{"tool_name": "read_file", "args": {}, "step_id": "step1"}])
    assert len(results) == 1
    assert results[0].status == STATUS_INVALID
    assert "nothing was run" in results[0].summary


def test_every_invocation_produces_exactly_one_result(workspace):
    mixed = [
        invocation("read_file", {"path": "notes.txt"}, "step1"),
        {"not": "an invocation"},
        invocation("teleport", {}, "step3"),
        None,
    ]
    results = EXECUTOR.execute(mixed)
    assert len(results) == len(mixed)


def test_a_malformed_entry_does_not_displace_its_neighbours(workspace):
    results = EXECUTOR.execute([
        invocation("read_file", {"path": "notes.txt"}, "step1"),
        "garbage",
        invocation("read_file", {"path": "notes.txt"}, "step3"),
    ])
    assert [r.status for r in results] == [STATUS_OK, STATUS_INVALID, STATUS_OK]
    assert [r.step_id for r in results] == ["step1", "index1", "step3"]


def test_an_empty_invocation_list_is_fine():
    assert EXECUTOR.execute([]) == []
    assert EXECUTOR.execute(None) == []


# ======================================================
# FIX 4 - ToolResult
# ======================================================
def test_the_executor_returns_tool_results(workspace):
    results = EXECUTOR.execute([invocation("read_file", {"path": "notes.txt"})])
    assert all(isinstance(result, ToolResult) for result in results)


def test_a_tool_result_carries_the_full_payload(workspace):
    result = EXECUTOR.execute([invocation("read_file", {"path": "notes.txt"})])[0]
    assert result.payload["text"].startswith("original")
    assert result.payload["bytes"] > 0


def test_ran_distinguishes_real_outcomes_from_skipped_ones():
    assert ToolResult("read_file", "step1", STATUS_OK, "read 1 bytes").ran
    assert ToolResult("read_file", "step1", STATUS_ERROR, "failed").ran
    assert not ToolResult("read_file", "step1", STATUS_INVALID, "malformed").ran
    assert not ToolResult("read_file", "step1", STATUS_NOT_EXECUTED, "skipped").ran


def test_a_tool_result_renders_a_line():
    result = ToolResult("run_tests", "step5", STATUS_OK, "tests passed (exit 0)")
    assert result.line == "run_tests (step5): tests passed (exit 0)"


def test_the_planning_result_is_not_the_registry_result():
    from backend.core.tool_registry import ToolResult as CoreResult

    assert ToolResult is not CoreResult
    assert not hasattr(ToolResult("x", "y", "ok", "s"), "sandbox")


# ======================================================
# Summaries never overstate
# ======================================================
def test_a_preview_summary_does_not_claim_an_edit(workspace):
    result = EXECUTOR.execute([
        invocation("edit_file", {"path": "notes.txt", "content": "new\n"})
    ])[0]
    assert "nothing written" in result.summary
    for claim in ("wrote", "created", "edited"):
        assert claim not in result.summary


def test_an_applied_edit_says_so(workspace):
    result = EXECUTOR.execute([
        invocation("edit_file", {"path": "notes.txt", "content": "new\n", "confirm": True})
    ])[0]
    assert "wrote notes.txt" in result.summary


def test_a_failing_test_run_is_not_summarized_as_passing(workspace, monkeypatch):
    monkeypatch.setenv(file_tools.ENV_TEST_COMMAND, f"{sys.executable} -c raise_SystemExit(1)")
    result = EXECUTOR.execute([invocation("run_tests", {})])[0]
    assert "tests failed" in result.summary


def test_an_error_summary_never_reads_as_success(workspace):
    result = EXECUTOR.execute([invocation("read_file", {"path": "nope.txt"})])[0]
    assert result.status == STATUS_ERROR
    for claim in ("passed", "succeeded", "completed", "wrote"):
        assert claim not in result.summary.lower()


def test_summarize_falls_back_for_an_unknown_tool():
    assert summarize("mystery", {"anything": 1})


# ======================================================
# FIX 6 - the prompt rule
# ======================================================
def bundle_of(items, query="fix the docs"):
    return build_evidence_bundle(query, items, now="FIXED")


def chunk(file_id: int, path: str, score: float = 0.8) -> dict:
    return {"type": "file_chunk", "file_id": file_id, "path": path,
            "section": "Pipeline", "text": "content", "combined_score": score}


TWO_FILES = [chunk(1, "docs/build.md", 0.9), chunk(2, "docs/deploy.md", 0.8)]


def test_results_that_ran_are_shown():
    results = [ToolResult("read_file", "step1", STATUS_OK, "read 42 bytes from build.md")]
    prompt = build_synthesis_prompt("q", bundle_of(TWO_FILES), [], None, None, None, results)
    assert "Tool Results:" in prompt
    assert "- read_file (step1): read 42 bytes from build.md" in prompt


def test_results_that_did_not_run_are_omitted_entirely():
    # Option A: a heading claiming results is never shown over things that
    # never happened.
    results = [ToolResult("run_tests", "step1", STATUS_NOT_EXECUTED, "skipped")]
    prompt = build_synthesis_prompt("q", bundle_of(TWO_FILES), [], None, None, None, results)
    assert "Tool Results" not in prompt


def test_a_malformed_result_is_omitted_too():
    results = [ToolResult("read_file", "step1", STATUS_INVALID, "malformed")]
    assert "Tool Results" not in build_synthesis_prompt(
        "q", bundle_of(TWO_FILES), [], None, None, None, results
    )


def test_errors_are_shown_because_they_happened():
    results = [ToolResult("read_file", "step1", STATUS_ERROR, "Not a file: build.md")]
    prompt = build_synthesis_prompt("q", bundle_of(TWO_FILES), [], None, None, None, results)
    assert "Tool Results:" in prompt
    assert "1 of these failed" in prompt
    assert "do not describe a failed tool as having done its job" in prompt


def test_a_clean_run_carries_no_failure_warning():
    results = [ToolResult("read_file", "step1", STATUS_OK, "read 42 bytes")]
    prompt = build_synthesis_prompt("q", bundle_of(TWO_FILES), [], None, None, None, results)
    assert "failed" not in prompt.split("Tool Results:")[1].split("\n\n")[0]


def test_the_section_is_omitted_without_results():
    assert "Tool Results" not in build_synthesis_prompt("q", bundle_of(TWO_FILES))
    assert "Tool Results" not in build_synthesis_prompt(
        "q", bundle_of(TWO_FILES), [], None, None, None, []
    )


def test_the_prompt_is_unchanged_without_the_new_argument():
    bundle = bundle_of(TWO_FILES)
    assert build_synthesis_prompt("q", bundle, [], None, None, None) == build_synthesis_prompt(
        "q", bundle, [], None, None, None, None
    )


def test_the_section_is_a_list_not_prose():
    results = [ToolResult("read_file", "step1", STATUS_OK, "read 42 bytes")]
    prompt = build_synthesis_prompt("q", bundle_of(TWO_FILES), [], None, None, None, results)
    body = prompt.split("Tool Results:")[1].split("\n\n")[0]
    for line in body.strip().splitlines():
        assert line.startswith("- ") or line.startswith("(")


def test_the_formatting_is_deterministic():
    results = [ToolResult("read_file", "step1", STATUS_OK, "read 42 bytes")]
    bundle = bundle_of(TWO_FILES)
    prompts = [
        build_synthesis_prompt("q", bundle, [], None, None, None, results) for _ in range(5)
    ]
    assert all(prompt == prompts[0] for prompt in prompts)


# ======================================================
# Routing (unchanged from Phase 9)
# ======================================================
def test_a_read_step_routes_to_read_file():
    invocations = ROUTER.route(Plan([step("step1", KIND_READ, "build.md")]))
    assert [i.tool_name for i in invocations] == ["read_file"]


def test_a_test_step_routes_to_run_tests():
    assert ROUTER.route(Plan([step("step1", KIND_TEST)]))[0].tool_name == "run_tests"


@pytest.mark.parametrize("kind", [KIND_ANALYZE, KIND_SUMMARIZE, KIND_ANSWER])
def test_a_reasoning_step_routes_to_nothing(kind):
    assert ROUTER.route(Plan([step("step1", kind)])) == []


def test_routing_is_deterministic():
    plan = Plan([step("step1", KIND_READ, "build.md"), step("step2", KIND_TEST)])
    assert ROUTER.route(plan) == ROUTER.route(plan)


def test_routing_still_makes_no_model_calls(monkeypatch):
    from backend.llm import semantic_embeddings

    monkeypatch.setattr(
        semantic_embeddings, "embed_text_semantic",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model in routing")),
    )
    assert route_plan(Plan([step("step1", KIND_TEST)]))


@pytest.mark.parametrize("kind,name", [
    (KIND_READ, "read_file"), (KIND_EDIT, "edit_file"), (KIND_TEST, "run_tests"),
])
def test_each_kind_maps_to_its_tool(kind, name):
    assert tool_for_kind(kind).name == name


# ======================================================
# End to end
# ======================================================
def test_a_planned_read_reaches_the_prompt(workspace):
    (workspace / "build.md").write_text("# Pipeline\n\nThree steps.\n", encoding="utf-8")
    (workspace / "deploy.md").write_text("# Pipeline\n\nSigned artifact.\n", encoding="utf-8")

    plan = PlanBuilder().build("explain how the docs relate", bundle_of(TWO_FILES))
    results = EXECUTOR.execute(ROUTER.route(plan))
    assert [r.status for r in results] == [STATUS_OK, STATUS_OK]

    prompt = build_synthesis_prompt(
        "q", bundle_of(TWO_FILES), [], None, None, plan, results
    )
    assert "read 26 bytes from build.md" in prompt or "read_file (step1)" in prompt


def test_the_engine_runs_tools_and_returns_the_answer(workspace):
    (workspace / "build.md").write_text("content\n", encoding="utf-8")
    (workspace / "deploy.md").write_text("content\n", encoding="utf-8")
    seen = []
    answer = answer_with_evidence(
        "explain how the docs relate", TWO_FILES,
        generate=lambda prompt: (seen.append(prompt), "an answer")[1],
    )
    assert answer == "an answer"
    assert "Tool Results:" in seen[0]


def test_the_engine_is_deterministic_with_tools(workspace):
    (workspace / "build.md").write_text("content\n", encoding="utf-8")
    (workspace / "deploy.md").write_text("content\n", encoding="utf-8")
    seen = []
    for _ in range(3):
        answer_with_evidence(
            "explain how the docs relate", TWO_FILES,
            generate=lambda prompt: (seen.append(prompt), "x")[1],
        )
    assert all(prompt == seen[0] for prompt in seen)


def test_a_single_document_query_calls_for_no_tools(workspace):
    seen = []
    answer_with_evidence(
        "explain the build", [chunk(1, "docs/build.md", 0.9)],
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert "Tool Results" not in seen[0]


def test_the_engine_survives_tools_that_fail(workspace):
    # The documents are not on disk, so every read errors. The answer is
    # still produced, and the prompt says the tools failed.
    seen = []
    answer = answer_with_evidence(
        "explain how the docs relate", TWO_FILES,
        generate=lambda prompt: (seen.append(prompt), "an answer")[1],
    )
    assert answer == "an answer"
    assert "of these failed" in seen[0]


# ======================================================
# Nothing else moved
# ======================================================
def test_the_planning_tool_registry_is_still_data_only():
    from backend.tools import tool_registry as planning

    assert not hasattr(planning, "execute_tool")
    for tool in TOOLS.values():
        assert isinstance(tool, Tool)


def test_ranking_weights_are_unchanged():
    assert memory_ranking.WEIGHT_SEMANTIC == 0.45
    assert memory_ranking.WEIGHT_KEYWORD == 0.25
    assert memory_ranking.WEIGHT_RECENCY == 0.20
    assert memory_ranking.WEIGHT_TYPE == 0.10


def test_score_floors_are_unchanged():
    assert semantic.DEFAULT_MIN_SCORE == 0.6
    assert ingestion.DEFAULT_MIN_SCORE == 0.55


def test_hybrid_output_is_unchanged_by_the_tool_layer(db, workspace):
    semantic.index_note(notes_store.save_note("the release pipeline runs nightly"))
    before = aria_memory.search_hybrid("release pipeline", min_score=0.0)
    EXECUTOR.execute([invocation("read_file", {"path": "notes.txt"})])
    after = aria_memory.search_hybrid("release pipeline", min_score=0.0)
    assert [i["keyword_score"] for i in before] == [i["keyword_score"] for i in after]
    assert [i["semantic_score"] for i in before] == [i["semantic_score"] for i in after]
