# backend/tests/test_phase8_planning.py
#
# Phase 8: multi-document planning.
#
# The first thing planning has to get right is producing nothing. Most
# questions have one document or none behind them, and a plan for those is a
# heading with no information under it -- so the single-step case, and the
# prompt's silence on it, are tested as heavily as the multi-step one.
#
# The second is the dependency graph. A plan is read in the order it is
# printed, so a step that depends on a later one is a plan that cannot be
# followed; every plan built here is checked against that.
#
# No model, no clock, no database.

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
from backend.files import file_ingestion as ingestion
from backend.planning.plan import (
    CONVERSATION_TARGET,
    KINDS,
    KIND_ANALYZE,
    KIND_ANSWER,
    KIND_EDIT,
    KIND_READ,
    KIND_SUMMARIZE,
    KIND_TEST,
    Plan,
    PlanStep,
)
from backend.planning.plan_builder import MAX_READ_STEPS, PlanBuilder, build_plan

BUILDER = PlanBuilder()
FIX_GOAL = GoalState(goal="fix the build pipeline", topic="unity", explicit=True)
EXPLAIN_GOAL = GoalState(goal="explain the build pipeline", topic="unity", explicit=True)


def chunk(file_id: int, path: str, text: str = "some content here", score: float = 0.8) -> dict:
    return {
        "type": "file_chunk", "file_id": file_id, "path": path,
        "section": "Pipeline", "text": text, "combined_score": score,
    }


def note(note_id: int, text: str, score: float = 0.8) -> dict:
    return {"type": "note", "id": note_id, "text": text, "combined_score": score}


ONE_FILE = [chunk(1, "docs/build.md", "The build pipeline has 3 steps.", 0.9)]
THREE_FILES = [
    chunk(1, "docs/build.md", "The build pipeline has 3 steps.", 0.9),
    chunk(2, "docs/deploy.md", "Deployment requires a signed artifact.", 0.8),
    chunk(3, "docs/ci.md", "CI runs on every merge.", 0.7),
]


def bundle_of(items, query="explain the pipeline", conversation=None):
    return build_evidence_bundle(query, items, conversation=conversation, now="FIXED")


def plan_for(query, items, goal=None) -> Plan:
    return BUILDER.build(query, bundle_of(items, query), "unity", goal)


# ======================================================
# 1. Single-file and empty queries
# ======================================================
def test_a_single_file_query_plans_one_answer_step():
    plan = plan_for("explain the build pipeline", ONE_FILE)
    assert len(plan) == 1
    assert plan.steps[0].kind == KIND_ANSWER
    assert plan.steps[0].target == CONVERSATION_TARGET
    assert plan.is_single_answer


def test_a_no_file_query_plans_one_answer_step():
    assert plan_for("explain the pipeline", [note(1, "The pipeline runs nightly.")]).is_single_answer


def test_an_empty_bundle_plans_one_answer_step():
    assert plan_for("anything", []).is_single_answer


def test_several_chunks_of_one_file_are_one_document():
    items = [
        chunk(1, "docs/build.md", "The first passage.", 0.9),
        chunk(1, "docs/build.md", "The second passage.", 0.8),
    ]
    assert plan_for("explain the build", items).is_single_answer


def test_notes_do_not_trigger_a_plan():
    # A note is quoted whole in the prompt; a step telling the model to read
    # something already fully in front of it says nothing.
    items = ONE_FILE + [note(n, f"note number {n}") for n in range(4)]
    assert plan_for("explain the build pipeline", items).is_single_answer


# ======================================================
# 2. Multi-file plans
# ======================================================
def test_one_read_step_per_document():
    plan = plan_for("explain how the docs relate", THREE_FILES)
    reads = plan.of_kind(KIND_READ)
    assert [step.target for step in reads] == ["build.md", "deploy.md", "ci.md"]


def test_read_steps_follow_evidence_order():
    # Best-scoring document first, so the plan works through the most
    # relevant one before the rest.
    plan = plan_for("explain how the docs relate", list(reversed(THREE_FILES)))
    assert plan.of_kind(KIND_READ)[0].target == "build.md"


def test_the_analyze_step_depends_on_every_read():
    plan = plan_for("explain how the docs relate", THREE_FILES)
    analyze = plan.of_kind(KIND_ANALYZE)[0]
    assert analyze.depends_on == [step.id for step in plan.of_kind(KIND_READ)]


def test_there_is_exactly_one_analyze_step():
    assert len(plan_for("explain how the docs relate", THREE_FILES).of_kind(KIND_ANALYZE)) == 1


def test_the_terminal_step_depends_on_the_analysis():
    plan = plan_for("explain how the docs relate", THREE_FILES)
    terminal = plan.of_kind(KIND_SUMMARIZE)[0]
    assert terminal.depends_on == [plan.of_kind(KIND_ANALYZE)[0].id]


def test_step_ids_are_sequential():
    plan = plan_for("explain how the docs relate", THREE_FILES)
    assert [step.id for step in plan.steps] == [f"step{n}" for n in range(1, len(plan) + 1)]


def test_every_dependency_points_at_an_earlier_step():
    for query in ("explain how the docs relate", "fix the docs", "fix the failing tests"):
        assert plan_for(query, THREE_FILES).dependencies_resolve


def test_read_steps_are_capped():
    many = [chunk(n, f"docs/file{n}.md", "content", 0.9 - n / 100) for n in range(12)]
    plan = BUILDER.build("explain them", build_evidence_bundle("q", many, now="FIXED"))
    assert len(plan.of_kind(KIND_READ)) <= MAX_READ_STEPS


def test_the_cap_is_configurable():
    many = [chunk(n, f"docs/file{n}.md", "content", 0.9 - n / 100) for n in range(5)]
    plan = PlanBuilder(max_read_steps=2).build("explain them", build_evidence_bundle("q", many, now="FIXED"))
    assert len(plan.of_kind(KIND_READ)) == 2


# ======================================================
# 3. Terminal step: edit vs summarize
# ======================================================
@pytest.mark.parametrize("query", [
    "fix the mismatch between the docs",
    "refactor the build steps",
    "change the deployment process",
    "update the pipeline docs",
    "rename the build target",
])
def test_a_change_request_ends_in_an_edit(query):
    assert plan_for(query, THREE_FILES).of_kind(KIND_EDIT)


@pytest.mark.parametrize("query", [
    "explain how the docs relate",
    "summarize the deployment process",
    "compare the build and deploy docs",
    "what is the pipeline",
])
def test_an_explanation_request_ends_in_a_summary(query):
    plan = plan_for(query, THREE_FILES)
    assert plan.of_kind(KIND_SUMMARIZE)
    assert not plan.of_kind(KIND_EDIT)


def test_a_change_word_beats_an_explanation_word():
    # "Fix the pipeline and explain why" wants the change; an answer shaped
    # as an explanation would not contain it.
    plan = plan_for("fix the pipeline and explain why it broke", THREE_FILES)
    assert plan.of_kind(KIND_EDIT)


def test_an_unmarked_query_takes_its_shape_from_the_goal():
    assert plan_for("continue", THREE_FILES, FIX_GOAL).of_kind(KIND_EDIT)


def test_an_explaining_goal_does_not_force_an_edit():
    assert plan_for("continue", THREE_FILES, EXPLAIN_GOAL).of_kind(KIND_SUMMARIZE)


def test_the_query_outranks_the_goal():
    assert plan_for("explain how they relate", THREE_FILES, FIX_GOAL).of_kind(KIND_SUMMARIZE)


def test_no_goal_defaults_to_a_summary():
    assert plan_for("continue", THREE_FILES).of_kind(KIND_SUMMARIZE)


# ======================================================
# 4. Test steps
# ======================================================
def test_a_test_step_follows_an_edit():
    plan = plan_for("fix the failing tests across the docs", THREE_FILES)
    tests = plan.of_kind(KIND_TEST)
    assert tests
    assert tests[0].depends_on == [plan.of_kind(KIND_EDIT)[0].id]


def test_the_test_step_is_last():
    plan = plan_for("fix the failing tests across the docs", THREE_FILES)
    assert plan.steps[-1].kind == KIND_TEST


@pytest.mark.parametrize("query", [
    "fix the failing tests",
    "fix the assertion in both docs",
    "update the pytest coverage",
    "fix the regression",
])
def test_each_test_word_adds_a_test_step(query):
    assert plan_for(query, THREE_FILES).of_kind(KIND_TEST)


def test_a_test_word_without_an_edit_adds_no_test_step():
    # "Why are the tests failing" asks for a diagnosis, not a test run.
    # A verification step on an explanation is a step the answer cannot take.
    plan = plan_for("explain why the tests are failing", THREE_FILES)
    assert plan.of_kind(KIND_SUMMARIZE)
    assert not plan.of_kind(KIND_TEST)


def test_an_edit_without_a_test_word_adds_no_test_step():
    assert not plan_for("fix the mismatch", THREE_FILES).of_kind(KIND_TEST)


def test_a_test_word_in_the_goal_counts():
    goal = GoalState(goal="fix the failing tests", topic="unity", explicit=True)
    assert plan_for("continue", THREE_FILES, goal).of_kind(KIND_TEST)


def test_a_single_file_query_never_gets_a_test_step():
    assert not plan_for("fix the failing tests", ONE_FILE).of_kind(KIND_TEST)


# ======================================================
# 5. Determinism and purity
# ======================================================
def test_the_same_query_and_bundle_give_the_same_plan():
    bundle = bundle_of(THREE_FILES)
    assert BUILDER.build("fix the docs", bundle) == BUILDER.build("fix the docs", bundle)


def test_planning_is_deterministic_across_many_builds():
    plans = [plan_for("fix the failing tests", THREE_FILES) for _ in range(5)]
    assert all(plan == plans[0] for plan in plans)


def test_planning_does_not_mutate_the_bundle():
    bundle = bundle_of(THREE_FILES)
    before = (list(bundle.notes), list(bundle.files), dict(bundle.meta))
    BUILDER.build("fix the docs", bundle)
    assert (bundle.notes, bundle.files, bundle.meta) == before


def test_planning_makes_no_model_calls(monkeypatch):
    from backend.llm import semantic_embeddings

    def explode(*args, **kwargs):
        raise AssertionError("planning must not call a model")

    monkeypatch.setattr(semantic_embeddings, "embed_texts_semantic", explode)
    monkeypatch.setattr(semantic_embeddings, "embed_text_semantic", explode)
    assert build_plan("fix the docs", bundle_of(THREE_FILES)).of_kind(KIND_EDIT)


def test_matching_is_case_insensitive():
    assert plan_for("FIX THE DOCS", THREE_FILES).of_kind(KIND_EDIT)


def test_a_keyword_does_not_fire_inside_a_longer_word():
    # "prefix" contains "fix", "contest" contains "test".
    plan = plan_for("explain the prefix in the contest results", THREE_FILES)
    assert plan.of_kind(KIND_SUMMARIZE)
    assert not plan.of_kind(KIND_TEST)


# ======================================================
# 6. Plan and PlanStep
# ======================================================
def test_every_kind_used_is_a_known_kind():
    for query in ("explain them", "fix them", "fix the failing tests"):
        assert set(plan_for(query, THREE_FILES).kinds) <= KINDS


def test_a_step_renders_as_a_line():
    step = PlanStep(id="step1", kind=KIND_READ, target="build.md", description="Work through it.")
    assert step.line == "read build.md: Work through it."


def test_a_plan_can_be_looked_up_by_id():
    plan = plan_for("explain them", THREE_FILES)
    assert plan.by_id("step1") is plan.steps[0]
    assert plan.by_id("nope") is None


def test_an_empty_plan_is_falsey():
    assert not Plan()
    assert not Plan().is_single_answer


def test_a_forward_dependency_is_reported_as_unresolved():
    broken = Plan([
        PlanStep(id="step1", kind=KIND_ANALYZE, target="x", description="d", depends_on=["step2"]),
        PlanStep(id="step2", kind=KIND_READ, target="y", description="d"),
    ])
    assert not broken.dependencies_resolve


def test_a_missing_dependency_is_reported_as_unresolved():
    broken = Plan([
        PlanStep(id="step1", kind=KIND_ANALYZE, target="x", description="d", depends_on=["nope"]),
    ])
    assert not broken.dependencies_resolve


# ======================================================
# 7. Prompt integration
# ======================================================
def prompt_for(query, items, conversation=None) -> str:
    seen = []
    answer_with_evidence(
        query, items, conversation=conversation,
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    return seen[0]


def test_the_plan_section_appears_for_a_multi_document_query():
    prompt = prompt_for("explain how the docs relate", THREE_FILES)
    assert "Plan:" in prompt
    assert "1. read build.md:" in prompt


def test_the_plan_section_is_omitted_for_a_single_document_query():
    assert "Plan:" not in prompt_for("explain the build pipeline", ONE_FILE)


def test_the_plan_section_is_omitted_without_a_plan():
    assert "Plan:" not in build_synthesis_prompt("q", bundle_of(THREE_FILES))


def test_a_single_answer_plan_renders_nothing():
    bundle = bundle_of(ONE_FILE)
    plan = BUILDER.build("explain it", bundle)
    assert "Plan:" not in build_synthesis_prompt("q", bundle, [], None, None, plan)


def test_the_steps_render_in_order_and_are_numbered():
    prompt = prompt_for("fix the failing tests across the docs", THREE_FILES)
    body = prompt.split("Plan:")[1].split("\n\n")[0]
    numbers = [line.split(".")[0].strip() for line in body.strip().splitlines()]
    assert numbers == [str(n) for n in range(1, len(numbers) + 1)]


def test_every_kind_and_target_reaches_the_prompt():
    prompt = prompt_for("fix the failing tests across the docs", THREE_FILES)
    for fragment in ("read build.md:", "read deploy.md:", "analyze conversation:",
                     "edit conversation:", "test conversation:"):
        assert fragment in prompt


def test_the_plan_precedes_the_answer_structure():
    prompt = prompt_for("fix the mismatch between the docs", THREE_FILES)
    assert prompt.index("Plan:") < prompt.index("Answer Structure:")


def test_the_plan_follows_the_evidence():
    prompt = prompt_for("explain how the docs relate", THREE_FILES)
    assert prompt.index("Files:") < prompt.index("Plan:")


def test_the_plan_section_is_a_list_not_prose():
    prompt = prompt_for("explain how the docs relate", THREE_FILES)
    body = prompt.split("Plan:")[1].split("\n\n")[0]
    assert all(line.strip()[0].isdigit() for line in body.strip().splitlines())


def test_the_prompt_is_deterministic_with_a_plan():
    prompts = [prompt_for("fix the failing tests across the docs", THREE_FILES) for _ in range(3)]
    assert all(prompt == prompts[0] for prompt in prompts)


def test_the_prompt_is_unchanged_without_the_new_argument():
    bundle = bundle_of(THREE_FILES)
    assert build_synthesis_prompt("q", bundle, [], None, None) == build_synthesis_prompt(
        "q", bundle, [], None, None, None
    )


# ======================================================
# 8. End to end, and nothing else moved
# ======================================================
def test_a_real_file_result_plans_cleanly(db, tmp_path):
    for name, text in (("build.md", "# Pipeline\n\nThe build has three steps."),
                       ("deploy.md", "# Pipeline\n\nDeployment needs a signed artifact.")):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        ingestion.ingest_and_index(str(path))

    hits = ingestion.search_files_semantic("pipeline", min_score=0.0)
    plan = BUILDER.build("explain how the pipeline docs relate", bundle_of(hits))
    assert len(plan.of_kind(KIND_READ)) == 2
    assert plan.dependencies_resolve


def test_the_engine_returns_the_answer_unchanged():
    assert answer_with_evidence(
        "fix the docs", THREE_FILES, generate=lambda prompt: "an answer"
    ) == "an answer"


def test_ranking_weights_are_unchanged():
    assert memory_ranking.WEIGHT_SEMANTIC == 0.45
    assert memory_ranking.WEIGHT_KEYWORD == 0.25
    assert memory_ranking.WEIGHT_RECENCY == 0.20
    assert memory_ranking.WEIGHT_TYPE == 0.10


def test_score_floors_are_unchanged():
    assert semantic.DEFAULT_MIN_SCORE == 0.6
    assert ingestion.DEFAULT_MIN_SCORE == 0.55


def test_hybrid_output_is_unchanged_by_planning(db):
    semantic.index_note(notes_store.save_note("the release pipeline runs nightly"))
    before = aria_memory.search_hybrid("release pipeline", min_score=0.0)
    BUILDER.build("fix the docs", bundle_of(THREE_FILES))
    after = aria_memory.search_hybrid("release pipeline", min_score=0.0)
    assert [i["keyword_score"] for i in before] == [i["keyword_score"] for i in after]
    assert [i["semantic_score"] for i in before] == [i["semantic_score"] for i in after]
