# backend/tests/test_phase6_templates.py
#
# Phase 6.3: structured answer templates.
#
# Two things get pinned here. The obvious one is that each kind of question
# gets the shape it asked for. The less obvious one, and the reason the
# priority order is tested case by case, is what happens when a query carries
# two cues at once -- "how do I fix the build error" is both a how-to and a
# failure, and only one of them can win. Those orderings are judgement calls,
# so they are written down as tests rather than left to whichever marker the
# classifier happened to check first.
#
# No model is loaded anywhere in this file.

from __future__ import annotations

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory
from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.aria_memory import memory_ranking
from backend.aria_synthesis import template_classifier
from backend.aria_synthesis.bundle_builder import build_evidence_bundle
from backend.aria_synthesis.conflict_detection import detect_conflicts
from backend.aria_synthesis.conflict_summary import summarize_conflicts
from backend.aria_synthesis.synthesis_engine import answer_with_evidence
from backend.aria_synthesis.synthesis_prompt import build_synthesis_prompt
from backend.aria_synthesis.template_classifier import (
    TEMPLATE_PRIORITY,
    TemplateType,
    classify_template,
    evidence_signals,
    query_signals,
)
from backend.aria_synthesis.template_renderers import (
    CONFLICT_PLACEMENT,
    TEMPLATE_INSTRUCTIONS,
    render_template,
)
from backend.files import file_ingestion as ingestion

T = TemplateType

# Deliberately shapeless: prose with no ordered list, no causal language and
# no tradeoffs, so a classification can only have come from the query.
NEUTRAL = "The release pipeline is configured in build.md and owned by the platform team."


def bundle_of(*texts, query: str = "pipeline", **kwargs):
    items = [
        {"type": "note", "id": index + 1, "text": text, "combined_score": 0.9 - index * 0.05}
        for index, text in enumerate(texts or (NEUTRAL,))
    ]
    kwargs.setdefault("now", "FIXED")
    return build_evidence_bundle(query, items, **kwargs)


def classify(query: str, *texts) -> TemplateType:
    return classify_template(query, bundle_of(*texts, query=query))


# ======================================================
# 1. Template classification
# ======================================================
@pytest.mark.parametrize("query", [
    "how do I set up the pipeline",
    "how to configure addressables",
    "steps to deploy the backend",
    "walk me through the release procedure",
    "how can I install the model",
])
def test_a_procedure_question_is_steps(query):
    assert classify(query) is T.STEPS


@pytest.mark.parametrize("query", [
    "why is the build failing",
    "the pipeline is not working",
    "shader compilation error",
    "the editor crashes on load",
    "help me troubleshoot the websocket",
])
def test_a_failure_question_is_diagnostic(query):
    assert classify(query) is T.DIAGNOSTIC


@pytest.mark.parametrize("query", [
    "compare addressables and assetbundles",
    "addressables vs assetbundles",
    "difference between a prefab and a scene",
    "how does it differ from the old pipeline",
])
def test_a_side_by_side_question_is_comparison(query):
    assert classify(query) is T.COMPARISON


@pytest.mark.parametrize("query", [
    "summarize the deployment process",
    "give me an overview of the memory system",
    "brief me on the routing layer",
    "recap the migration",
])
def test_a_condensation_request_is_summary(query):
    assert classify(query) is T.SUMMARY


@pytest.mark.parametrize("query", [
    "what are the prerequisites for deployment",
    "deployment requirements",
    "what do I need before running the migration",
    "give me a checklist for the release",
])
def test_a_readiness_question_is_checklist(query):
    assert classify(query) is T.CHECKLIST


@pytest.mark.parametrize("query", [
    "which should I use for asset loading",
    "should I switch to addressables",
    "which one is better for large scenes",
    "can you recommend a chunk size",
])
def test_a_recommendation_question_is_a_decision_guide(query):
    assert classify(query) is T.DECISION_GUIDE


@pytest.mark.parametrize("query", [
    "what is the release pipeline",
    "why does the indexer chunk notes",
    "how does semantic search work",
    "explain the scoring model",
])
def test_a_plain_question_is_an_explanation(query):
    assert classify(query) is T.EXPLANATION


def test_a_bare_topic_with_shapeless_evidence_is_an_explanation():
    assert classify("the release pipeline") is T.EXPLANATION


# --- priority: queries carrying two cues ---
def test_a_recommendation_beats_a_comparison():
    # "which should I use, A or B" asks us to make the call; answering with
    # a bare comparison hands it straight back.
    assert classify("which should I use, addressables vs assetbundles") is T.DECISION_GUIDE


def test_a_failure_beats_a_procedure():
    # A procedure cannot be written until the cause is known.
    assert classify("how do I fix the build error") is T.DIAGNOSTIC


def test_a_procedure_with_no_failure_stays_steps():
    assert classify("how do I run the build") is T.STEPS


def test_readiness_beats_a_failure():
    assert classify("what are the requirements, the deploy fails without them") is T.CHECKLIST


def test_a_procedure_beats_a_summary():
    # A numbered list is already a summary of a procedure.
    assert classify("summarize how to deploy the backend") is T.STEPS


def test_the_priority_order_covers_every_template():
    assert set(TEMPLATE_PRIORITY) == set(TemplateType)
    assert TEMPLATE_PRIORITY[-1] is T.EXPLANATION


def test_query_signals_come_back_in_priority_order():
    signals = query_signals("which should I use, addressables vs assetbundles")
    assert signals == [template for template in TEMPLATE_PRIORITY if template in signals]


# --- the evidence decides when the query is silent ---
def test_ordered_evidence_suggests_steps():
    assert classify("the migration", "1. Back up the database. 2. Run the migration.") is T.STEPS


def test_step_words_in_the_evidence_suggest_steps():
    assert classify("the migration", "First back up the database, then run the migration.") is T.STEPS


def test_causal_evidence_suggests_a_diagnostic():
    assert classify("the timeout", "The request times out because the provider is cold.") is T.DIAGNOSTIC


def test_requirement_evidence_suggests_a_checklist():
    assert classify("the deploy", "You must have a signed build and a release tag.") is T.CHECKLIST


def test_broad_evidence_suggests_a_summary():
    # Broad means both halves of retrieval contributed. Five notes alone is
    # an ordinary result for any query, and treating that as "broad" would
    # turn every unmarked question into an overview.
    items = [
        {"type": "note", "id": n, "text": f"Topic number {n} stands on its own.",
         "combined_score": 0.9 - n / 100}
        for n in range(4)
    ] + [
        {"type": "file_chunk", "file_id": n, "path": f"docs/f{n}.md",
         "text": f"Section {n} stands on its own.", "combined_score": 0.5}
        for n in range(3)
    ]
    bundle = build_evidence_bundle("the system", items, now="FIXED")
    assert bundle.item_count >= template_classifier.BROAD_ITEM_COUNT
    assert classify_template("the system", bundle) is T.SUMMARY


def test_an_ordinary_five_note_result_is_not_broad():
    assert classify("the system", *[f"Topic {n} stands alone." for n in range(5)]) is T.EXPLANATION


def test_the_query_outranks_the_evidence():
    # The notes are a numbered procedure, but the user asked what it is.
    assert classify("what is the migration", "1. Back up. 2. Run the migration.") is T.EXPLANATION


def test_shapeless_evidence_suggests_nothing():
    assert evidence_signals(bundle_of(NEUTRAL)) == []


def test_an_empty_bundle_suggests_nothing():
    assert evidence_signals(bundle_of()) == []
    assert classify_template("the pipeline", bundle_of()) is T.EXPLANATION


# --- robustness ---
def test_classification_is_case_insensitive():
    assert classify("HOW DO I SET UP THE PIPELINE") is T.STEPS


def test_punctuation_does_not_hide_a_marker():
    assert classify("addressables vs. assetbundles") is T.COMPARISON
    assert classify("why doesn't the build work?") is T.DIAGNOSTIC


@pytest.mark.parametrize("query", [
    "what is vsync",              # "vs" inside a word
    "what causes footsteps to stutter",   # "steps" inside a word
    "what is a cantilever bracket",       # "cant" inside a word
])
def test_a_marker_does_not_fire_inside_a_longer_word(query):
    assert classify(query) is T.EXPLANATION


def test_a_marker_spelled_out_in_full_does_fire():
    # The flip side: "versus" written as a word is a comparison request,
    # and matching it is correct rather than a false positive.
    assert classify("addressables versus assetbundles") is T.COMPARISON


def test_an_empty_query_is_an_explanation():
    assert classify("") is T.EXPLANATION
    assert classify_template(None, bundle_of()) is T.EXPLANATION


def test_classification_is_deterministic():
    bundle = bundle_of()
    assert all(
        classify_template("how do I deploy", bundle) is T.STEPS for _ in range(5)
    )


def test_classification_makes_no_model_calls(monkeypatch):
    from backend.llm import semantic_embeddings

    def explode(*args, **kwargs):
        raise AssertionError("template classification must not call a model")

    monkeypatch.setattr(semantic_embeddings, "embed_texts_semantic", explode)
    monkeypatch.setattr(semantic_embeddings, "embed_text_semantic", explode)
    assert classify("how do I deploy") is T.STEPS


# ======================================================
# 2. Template rendering
# ======================================================
@pytest.mark.parametrize("template,fragment", [
    (T.STEPS, "numbered sequence of steps"),
    (T.DIAGNOSTIC, "possible causes"),
    (T.COMPARISON, "comparison of the alternatives"),
    (T.CHECKLIST, "checklist of required items"),
    (T.DECISION_GUIDE, "decision guide"),
    (T.SUMMARY, "short overview"),
])
def test_each_template_renders_its_own_instruction(template, fragment):
    assert fragment in render_template(template, bundle_of(), [])


def test_explanation_renders_nothing():
    # The default shape. An instruction saying "write prose" only displaces
    # a more useful sentence.
    assert render_template(T.EXPLANATION, bundle_of(), []) == ""


def test_every_template_except_explanation_has_an_instruction():
    for template in TemplateType:
        rendered = render_template(template, bundle_of(), [])
        assert bool(rendered) == (template is not T.EXPLANATION)


def test_rendering_is_deterministic():
    for template in TemplateType:
        assert render_template(template, bundle_of(), []) == render_template(
            template, bundle_of(), []
        )


def test_rendering_leaks_no_evidence():
    # The instruction says how to arrange an answer, never what is in it.
    bundle = bundle_of("The pipeline runs nightly at 02:00 UTC.")
    for template in TemplateType:
        rendered = render_template(template, bundle, [])
        assert "02:00" not in rendered
        assert "note:" not in rendered
        assert "pipeline" not in rendered.lower()


def test_rendering_leaks_no_conflict_content():
    bundle = bundle_of(
        "The pipeline requires a rebuild.", "The pipeline does not require a rebuild."
    )
    conflicts = detect_conflicts(bundle)
    rendered = render_template(T.STEPS, bundle, conflicts)
    assert "rebuild" not in rendered.lower()
    assert "note:" not in rendered


def test_conflicts_add_a_placement_note():
    bundle = bundle_of(
        "The pipeline requires a rebuild.", "The pipeline does not require a rebuild."
    )
    conflicts = detect_conflicts(bundle)
    assert conflicts
    assert CONFLICT_PLACEMENT in render_template(T.STEPS, bundle, conflicts)


def test_no_conflicts_means_no_placement_note():
    assert CONFLICT_PLACEMENT not in render_template(T.STEPS, bundle_of(), [])


def test_an_explanation_stays_empty_even_with_conflicts():
    bundle = bundle_of(
        "The pipeline requires a rebuild.", "The pipeline does not require a rebuild."
    )
    assert render_template(T.EXPLANATION, bundle, detect_conflicts(bundle)) == ""


def test_instructions_are_short():
    for instruction in TEMPLATE_INSTRUCTIONS.values():
        assert len(instruction) <= 260


def test_the_bundle_and_conflicts_are_optional():
    assert render_template(T.STEPS) == render_template(T.STEPS, bundle_of(), [])


# ======================================================
# 3. Prompt integration
# ======================================================
def prompt_with(query: str, *texts) -> str:
    bundle = bundle_of(*texts, query=query)
    conflicts = detect_conflicts(bundle)
    return build_synthesis_prompt(
        query,
        bundle,
        summarize_conflicts(conflicts),
        render_template(classify_template(query, bundle), bundle, conflicts),
    )


def test_the_structure_section_appears():
    prompt = prompt_with("how do I deploy the backend")
    assert "Answer Structure:" in prompt
    assert "numbered sequence of steps" in prompt


def test_the_section_is_omitted_for_an_explanation():
    prompt = prompt_with("what is the release pipeline")
    assert "Answer Structure" not in prompt


def test_the_section_is_omitted_for_an_empty_instruction():
    assert "Answer Structure" not in build_synthesis_prompt("q", bundle_of(), [], "")
    assert "Answer Structure" not in build_synthesis_prompt("q", bundle_of(), [], None)


def test_the_structure_follows_the_conflicts():
    prompt = prompt_with(
        "how do I deploy the backend",
        "The pipeline requires a rebuild.",
        "The pipeline does not require a rebuild.",
    )
    assert prompt.index("Conflicts Detected:") < prompt.index("Answer Structure:")


def test_the_structure_precedes_the_synthesis_instructions():
    prompt = prompt_with("how do I deploy the backend")
    assert prompt.index("Answer Structure:") < prompt.index("Synthesis Instructions:")


def test_the_structure_follows_the_evidence():
    prompt = prompt_with("how do I deploy the backend")
    assert prompt.index("Notes:") < prompt.index("Answer Structure:")


def test_the_prompt_without_a_template_is_unchanged_from_phase_6_2():
    bundle = bundle_of()
    assert build_synthesis_prompt("q", bundle, []) == build_synthesis_prompt("q", bundle, [], "")


def test_the_prompt_is_deterministic_with_a_template():
    prompts = [prompt_with("how do I deploy the backend") for _ in range(5)]
    assert all(prompt == prompts[0] for prompt in prompts)


def test_the_structure_section_holds_one_instruction():
    prompt = prompt_with("how do I deploy the backend")
    body = prompt.split("Answer Structure:")[1].split("Synthesis Instructions:")[0]
    assert len([line for line in body.splitlines() if line.strip()]) == 1


# ======================================================
# 4. Synthesis behaviour
# ======================================================
# What the model writes cannot be asserted deterministically. What can be
# asserted is that the instruction reached it, alongside everything Phase 6.1
# and 6.2 already put in front of it.
def prompt_seen_by_model(query: str, *texts) -> str:
    seen = []
    items = [
        {"type": "note", "id": index + 1, "text": text, "combined_score": 0.9 - index * 0.05}
        for index, text in enumerate(texts or (NEUTRAL,))
    ]
    answer_with_evidence(query, items, generate=lambda prompt: (seen.append(prompt), "x")[1])
    return seen[0]


def test_the_model_is_told_to_use_steps():
    assert "numbered sequence of steps" in prompt_seen_by_model("how do I deploy the backend")


def test_the_model_is_told_to_compare():
    assert "comparison of the alternatives" in prompt_seen_by_model(
        "compare addressables and assetbundles"
    )


def test_the_model_is_told_to_diagnose():
    assert "possible causes" in prompt_seen_by_model("why is the build failing")


def test_an_explanation_query_gets_no_structure_instruction():
    assert "Answer Structure" not in prompt_seen_by_model("what is the release pipeline")


def test_conflicts_are_still_acknowledged_alongside_a_template():
    prompt = prompt_seen_by_model(
        "how do I deploy the backend",
        "The pipeline requires a rebuild.",
        "The pipeline does not require a rebuild.",
    )
    assert "Conflicts Detected:" in prompt
    assert "Answer Structure:" in prompt
    assert CONFLICT_PLACEMENT in prompt


def test_the_no_hallucination_rules_survive_a_template():
    prompt = prompt_seen_by_model("how do I deploy the backend")
    assert "Use ONLY the evidence above." in prompt
    assert "Never invent an answer." in prompt
    # And the structure instruction carries its own version of the rule.
    assert "rather than filling the gap" in prompt


def test_provenance_survives_a_template():
    prompt = prompt_seen_by_model("how do I deploy the backend")
    assert "[note:1]" in prompt


def test_the_engine_returns_the_answer_unchanged():
    assert answer_with_evidence(
        "how do I deploy",
        [{"type": "note", "id": 1, "text": NEUTRAL, "combined_score": 0.9}],
        generate=lambda prompt: "1. Do the thing.",
    ) == "1. Do the thing."


def test_the_engine_is_deterministic_with_a_template():
    seen = []
    for _ in range(3):
        answer_with_evidence(
            "how do I deploy the backend",
            [{"type": "note", "id": 1, "text": NEUTRAL, "combined_score": 0.9}],
            generate=lambda prompt: (seen.append(prompt), "x")[1],
        )
    assert all(prompt == seen[0] for prompt in seen)


def test_an_empty_bundle_never_reaches_classification(monkeypatch):
    monkeypatch.setattr(
        template_classifier, "classify_template",
        lambda query, bundle: (_ for _ in ()).throw(AssertionError("nothing to shape")),
    )
    from backend.aria_synthesis.synthesis_engine import NO_EVIDENCE_ANSWER

    assert answer_with_evidence("how do I deploy", [], generate=lambda p: "x") == NO_EVIDENCE_ANSWER


# ======================================================
# 5. No retrieval drift
# ======================================================
def test_ranking_weights_are_unchanged():
    assert memory_ranking.WEIGHT_SEMANTIC == 0.45
    assert memory_ranking.WEIGHT_KEYWORD == 0.25
    assert memory_ranking.WEIGHT_RECENCY == 0.20
    assert memory_ranking.WEIGHT_TYPE == 0.10


def test_score_floors_are_unchanged():
    assert semantic.DEFAULT_MIN_SCORE == 0.6
    assert ingestion.DEFAULT_MIN_SCORE == 0.55


def test_classification_does_not_mutate_the_bundle():
    bundle = bundle_of("1. Back up. 2. Migrate.")
    before = (list(bundle.notes), list(bundle.files), dict(bundle.meta))
    classify_template("the migration", bundle)
    assert (bundle.notes, bundle.files, bundle.meta) == before


def test_hybrid_search_output_is_unchanged_by_templating(db):
    semantic.index_note(notes_store.save_note("the release pipeline runs nightly"))

    before = aria_memory.search_hybrid("release pipeline", min_score=0.0)
    bundle = build_evidence_bundle("how do I run the release pipeline", before, now="FIXED")
    classify_template("how do I run the release pipeline", bundle)
    after = aria_memory.search_hybrid("release pipeline", min_score=0.0)

    assert [i["id"] for i in before] == [i["id"] for i in after]
    assert [i["keyword_score"] for i in before] == [i["keyword_score"] for i in after]
    assert [i["semantic_score"] for i in before] == [i["semantic_score"] for i in after]


def test_a_real_retrieval_gets_a_structured_prompt(db):
    for text in ("First back up the database.", "Then run the migration script."):
        semantic.index_note(notes_store.save_note(text))

    seen = []
    answer_with_evidence(
        "how do I run the migration",
        aria_memory.search_ranked("migration"),
        generate=lambda prompt: (seen.append(prompt), "answer")[1],
    )
    assert "Answer Structure:" in seen[0]
    assert "numbered sequence of steps" in seen[0]
