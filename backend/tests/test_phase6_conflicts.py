# backend/tests/test_phase6_conflicts.py
#
# Phase 6.2: conflict-aware multi-document fusion.
#
# Detection is judged on precision first. Roughly half of this file is
# statements that must NOT be reported as contradictions -- a general rule
# and its exception, two measurements of different things that share a unit,
# two sources agreeing in different words. A detector that finds every real
# conflict and invents one per query is worse than no detector, because the
# warning stops meaning anything.
#
# Everything here runs on plain dicts and a stub generate(); no model is
# loaded and no embedding backend is required.

from __future__ import annotations

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory
from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.aria_memory import memory_ranking
from backend.aria_synthesis import conflict_detection, conflict_summary
from backend.aria_synthesis.bundle_builder import build_evidence_bundle
from backend.aria_synthesis.conflict_detection import (
    KIND_NUMERIC,
    KIND_POLARITY,
    Conflict,
    ConflictStatement,
    detect_conflicts,
    sentences,
)
from backend.aria_synthesis.conflict_summary import (
    summarize_conflict,
    summarize_conflicts,
)
from backend.aria_synthesis.synthesis_engine import answer_with_evidence, synthesize
from backend.aria_synthesis.synthesis_prompt import (
    CONFLICT_INSTRUCTION,
    build_synthesis_prompt,
)
from backend.files import file_ingestion as ingestion


def bundle_of(*texts, query: str = "pipeline", **kwargs):
    """A bundle of one note per text, scored highest-first."""
    items = [
        {"type": "note", "id": index + 1, "text": text, "combined_score": 0.9 - index * 0.05}
        for index, text in enumerate(texts)
    ]
    kwargs.setdefault("now", "FIXED")
    return build_evidence_bundle(query, items, **kwargs)


def conflicts_between(*texts) -> list[Conflict]:
    return detect_conflicts(bundle_of(*texts))


def summaries_between(*texts) -> list[str]:
    return summarize_conflicts(conflicts_between(*texts))


# ======================================================
# 1. Conflict detection
# ======================================================
# --- numeric ---
def test_a_numeric_contradiction_is_detected():
    found = conflicts_between(
        "The build pipeline has 3 steps.", "The build pipeline has 4 steps."
    )
    assert len(found) == 1
    assert found[0].kind == KIND_NUMERIC


def test_a_time_contradiction_is_detected():
    found = conflicts_between(
        "The release pipeline runs nightly at 02:00 UTC.",
        "The release pipeline was moved to 06:00 UTC.",
    )
    assert found and found[0].kind == KIND_NUMERIC


def test_a_trailing_value_contradiction_is_detected():
    # The number ends the clause, so the noun it measures is behind it.
    found = conflicts_between("The score floor is 0.6.", "The score floor is 0.55.")
    assert found and found[0].detail["noun"] == "floor"


def test_a_numeric_conflict_carries_both_values():
    found = conflicts_between(
        "The build pipeline has 3 steps.", "The build pipeline has 4 steps."
    )
    assert found[0].detail["values"] == ["3", "4"]


def test_the_same_value_twice_is_not_a_conflict():
    assert conflicts_between(
        "The build pipeline has 3 steps.", "The build pipeline has 3 steps."
    ) == []


# --- boolean and polarity ---
def test_a_boolean_contradiction_is_detected():
    found = conflicts_between(
        "The radar endpoint is supported.", "The radar endpoint is not supported."
    )
    assert found and found[0].kind == KIND_POLARITY


def test_an_antonym_contradiction_is_detected():
    # No negator anywhere: "deprecated" is the negation of "active".
    found = conflicts_between(
        "The radar endpoint is deprecated.", "The radar endpoint is active."
    )
    assert found and found[0].kind == KIND_POLARITY


def test_a_mutually_exclusive_claim_is_detected():
    found = conflicts_between(
        "The pipeline requires a rebuild.", "The pipeline does not require a rebuild."
    )
    assert found and found[0].kind == KIND_POLARITY


def test_incompatible_instructions_are_detected():
    found = conflicts_between(
        "You should edit the config file.", "You should not edit the config file."
    )
    assert found


@pytest.mark.parametrize("negative,positive", [
    ("The feature is disabled.", "The feature is enabled."),
    ("The setting is optional.", "The setting is required."),
    ("The action is forbidden.", "The action is allowed."),
    ("The column is missing.", "The column is present."),
    ("The endpoint is unavailable.", "The endpoint is available."),
])
def test_each_antonym_pair_contradicts(negative, positive):
    assert conflicts_between(negative, positive)


@pytest.mark.parametrize("contraction", [
    "The pipeline doesn't require a rebuild.",
    "The pipeline does not require a rebuild.",
    "The pipeline never requires a rebuild.",
])
def test_negation_is_found_however_it_is_spelled(contraction):
    assert conflicts_between("The pipeline requires a rebuild.", contraction)


# --- no false positives ---
@pytest.mark.parametrize("left,right", [
    # Unrelated subjects.
    ("The build pipeline has 3 steps.", "My vacation starts in July."),
    # Same unit, different things measured -- the trap the extra shared-term
    # requirement exists to catch.
    ("The build takes 3 minutes.", "The deploy takes 5 minutes."),
    ("The cache holds 10 entries.", "The queue holds 20 jobs."),
    # A general rule and its exception. Both true.
    ("The pipeline runs nightly.", "The pipeline does not run on weekends."),
    ("Notes are indexed on save.", "Empty notes are not indexed on save."),
    # Agreement worded differently.
    ("The radar endpoint is active.", "The radar endpoint is active and healthy."),
    # Ordinary prose with no claims in it at all.
    ("The team met on Tuesday to review.", "Notes from the review were filed."),
    # Same polarity, different detail.
    ("The pipeline requires a rebuild.", "The pipeline requires a restart."),
])
def test_unrelated_or_compatible_text_is_not_a_conflict(left, right):
    assert conflicts_between(left, right) == []


@pytest.mark.parametrize("left,right", [
    # A restrictive qualifier narrows who the sentence is about, so the
    # second is an exception to the first rather than a denial of it. Both
    # differ by exactly one word, which a bare count would have allowed.
    ("Notes are indexed on save.", "Empty notes are not indexed on save."),
    ("Files are scanned on import.", "Large files are not scanned on import."),
    ("Chunks are embedded at ingest.", "Duplicate chunks are not embedded at ingest."),
])
def test_a_one_word_restrictive_qualifier_is_not_a_contradiction(left, right):
    assert conflicts_between(left, right) == []


@pytest.mark.parametrize("left,right", [
    # A word that dates a claim rather than narrowing it. These do conflict.
    ("The endpoint is active.", "The endpoint is no longer active."),
    ("The endpoint is deprecated.", "The endpoint is now active."),
])
def test_a_word_that_only_dates_a_claim_still_contradicts(left, right):
    assert conflicts_between(left, right)


def test_an_auxiliary_verb_is_not_a_differing_subject():
    # "does not require" carries an auxiliary its affirmative twin lacks.
    # Counted as a subject word, that alone would hide the contradiction.
    assert conflicts_between(
        "The pipeline requires a rebuild.", "The pipeline does not require a rebuild."
    )


def test_a_short_claim_contradicts_on_identical_wording():
    # Almost nothing here but the number and its noun, so the shared-term
    # count cannot carry it -- being otherwise word-for-word identical does.
    assert conflicts_between("The score floor is 0.6.", "The score floor is 0.55.")


def test_a_source_that_qualifies_itself_is_not_a_conflict():
    # Both sentences are in one note. A note allowed to contradict itself
    # would make every carefully hedged note look like a disagreement.
    assert conflicts_between(
        "The endpoint is active. The endpoint is not active during maintenance."
    ) == []


def test_an_empty_bundle_has_no_conflicts():
    assert detect_conflicts(bundle_of()) == []


def test_a_single_source_has_no_conflicts():
    assert conflicts_between("The build pipeline has 3 steps.") == []


# --- structure ---
def test_a_conflict_carries_every_statement_with_provenance():
    found = conflicts_between(
        "The pipeline requires a rebuild.", "The pipeline does not require a rebuild."
    )
    assert sorted(found[0].provenances) == ["note:1", "note:2"]
    assert all(isinstance(s, ConflictStatement) for s in found[0].statements)


def test_statements_are_ordered_by_score():
    found = conflicts_between(
        "The pipeline requires a rebuild.", "The pipeline does not require a rebuild."
    )
    scores = [statement.score for statement in found[0].statements]
    assert scores == sorted(scores, reverse=True)


def test_a_statement_keeps_its_own_sentence_not_the_whole_snippet():
    found = conflicts_between(
        "The pipeline requires a rebuild. Unrelated trailing sentence here.",
        "The pipeline does not require a rebuild.",
    )
    assert "Unrelated trailing" not in found[0].statements[0].text


def test_a_conflict_has_a_readable_topic():
    found = conflicts_between(
        "The radar endpoint is deprecated.", "The radar endpoint is active."
    )
    assert "radar" in found[0].topic and "endpoint" in found[0].topic


def test_conflicts_span_notes_and_files():
    items = [
        {"type": "note", "id": 1, "text": "The build pipeline has 3 steps.",
         "combined_score": 0.9},
        {"type": "file_chunk", "file_id": 1, "path": "docs/build.md", "section": "Steps",
         "text": "The build pipeline has 4 steps.", "combined_score": 0.7},
    ]
    found = detect_conflicts(build_evidence_bundle("pipeline", items, now="FIXED"))
    assert sorted(found[0].provenances) == ["file:build.md#Steps", "note:1"]


def test_three_sources_on_one_topic_group_into_one_conflict():
    found = conflicts_between(
        "The build pipeline has 3 steps.",
        "The build pipeline has 4 steps.",
        "The build pipeline has 5 steps.",
    )
    assert len(found) == 1
    assert len(found[0].statements) == 3


def test_two_separate_disagreements_stay_separate():
    found = conflicts_between(
        "The build pipeline has 3 steps. The radar endpoint is active.",
        "The build pipeline has 4 steps. The radar endpoint is deprecated.",
    )
    assert len(found) == 2
    assert {conflict.kind for conflict in found} == {KIND_NUMERIC, KIND_POLARITY}


def test_a_truncated_fragment_is_not_judged():
    # A snippet cut mid-sentence cannot be read for what it asserts.
    assert sentences("A full sentence here. a trailing fragment that was cut…") == [
        "A full sentence here."
    ]


def test_detection_makes_no_model_calls(monkeypatch):
    from backend.llm import semantic_embeddings

    def explode(*args, **kwargs):
        raise AssertionError("conflict detection must not call a model")

    monkeypatch.setattr(semantic_embeddings, "embed_texts_semantic", explode)
    monkeypatch.setattr(semantic_embeddings, "embed_text_semantic", explode)
    assert conflicts_between(
        "The pipeline requires a rebuild.", "The pipeline does not require a rebuild."
    )


# ======================================================
# 2. Conflict summarization
# ======================================================
def test_a_polarity_summary_reads_as_a_question_of_fact():
    assert summaries_between(
        "The pipeline requires a rebuild.", "The pipeline does not require a rebuild."
    ) == ["Sources disagree on whether the pipeline requires a rebuild."]


def test_a_numeric_summary_names_both_values():
    assert summaries_between(
        "The build pipeline has 3 steps.", "The build pipeline has 4 steps."
    ) == ["Sources give different values for step: 3 and 4."]


def test_an_antonym_summary_uses_the_affirmative_side():
    # Stated positively whichever order the sources arrived in, so the
    # sentence reads the same way every time.
    assert summaries_between(
        "The radar endpoint is deprecated.", "The radar endpoint is active."
    ) == summaries_between(
        "The radar endpoint is active.", "The radar endpoint is deprecated."
    )


def test_summaries_contain_no_provenance():
    summaries = summaries_between(
        "The build pipeline has 3 steps.", "The build pipeline has 4 steps."
    )
    for summary in summaries:
        assert "note:" not in summary
        assert "file:" not in summary


def test_summaries_are_concise():
    long_claim = "The pipeline " + "very " * 60 + "requires a rebuild."
    summaries = summaries_between(long_claim, long_claim.replace("requires", "does not require"))
    assert all(len(summary) <= conflict_summary.SUMMARY_CHARS + 40 for summary in summaries)


def test_summaries_are_neutral():
    # Nothing that decides the disagreement, or the summary has resolved it
    # here, with no evidence, before the model ever sees the sources.
    summaries = summaries_between(
        "The radar endpoint is deprecated.", "The radar endpoint is active."
    )
    lowered = " ".join(summaries).lower()
    for leading in ("correct", "incorrect", "wrong", "outdated", "actually", "should be"):
        assert leading not in lowered


def test_summaries_are_deterministic():
    texts = ("The build pipeline has 3 steps.", "The build pipeline has 4 steps.")
    assert summaries_between(*texts) == summaries_between(*texts)


def test_no_conflicts_summarize_to_nothing():
    assert summarize_conflicts([]) == []
    assert summarize_conflicts(None) == []


def test_duplicate_summaries_are_collapsed():
    repeated = Conflict(
        topic="pipeline rebuild",
        statements=[ConflictStatement("note:1", "The pipeline requires a rebuild.", 0.9)],
        kind=KIND_POLARITY,
        detail={"affirmative": "The pipeline requires a rebuild."},
    )
    assert summarize_conflicts([repeated, repeated]) == [summarize_conflict(repeated)]


def test_summarization_makes_no_model_calls(monkeypatch):
    from backend.llm import semantic_embeddings

    monkeypatch.setattr(
        semantic_embeddings, "embed_texts_semantic",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model in summarization")),
    )
    assert summaries_between(
        "The build pipeline has 3 steps.", "The build pipeline has 4 steps."
    )


# ======================================================
# 3. Prompt integration
# ======================================================
def prompt_with(*texts, query: str = "pipeline") -> str:
    bundle = bundle_of(*texts, query=query)
    return build_synthesis_prompt(query, bundle, summarize_conflicts(detect_conflicts(bundle)))


def test_conflicts_appear_in_the_prompt():
    prompt = prompt_with(
        "The pipeline requires a rebuild.", "The pipeline does not require a rebuild."
    )
    assert "Conflicts Detected:" in prompt
    assert "Sources disagree on whether the pipeline requires a rebuild." in prompt


def test_the_conflict_section_carries_its_instruction():
    prompt = prompt_with(
        "The pipeline requires a rebuild.", "The pipeline does not require a rebuild."
    )
    assert CONFLICT_INSTRUCTION in prompt


def test_the_section_is_omitted_when_there_are_no_conflicts():
    prompt = prompt_with("The build pipeline has 3 steps.")
    assert "Conflicts Detected" not in prompt


def test_the_section_is_omitted_for_an_empty_summary_list():
    bundle = bundle_of("The build pipeline has 3 steps.")
    assert "Conflicts Detected" not in build_synthesis_prompt("q", bundle, [])


def test_conflicts_come_before_the_synthesis_instructions():
    # The model should know what is disputed while it is still reading the
    # evidence, not after it has settled on the highest-scoring source.
    prompt = prompt_with(
        "The pipeline requires a rebuild.", "The pipeline does not require a rebuild."
    )
    assert prompt.index("Conflicts Detected:") < prompt.index("Synthesis Instructions:")


def test_conflicts_come_after_the_evidence():
    prompt = prompt_with(
        "The pipeline requires a rebuild.", "The pipeline does not require a rebuild."
    )
    assert prompt.index("Notes:") < prompt.index("Conflicts Detected:")


def test_provenance_survives_in_the_evidence_section():
    prompt = prompt_with(
        "The pipeline requires a rebuild.", "The pipeline does not require a rebuild."
    )
    assert "[note:1]" in prompt
    assert "[note:2]" in prompt


def test_both_sides_of_a_conflict_stay_in_the_evidence():
    prompt = prompt_with(
        "The pipeline requires a rebuild.", "The pipeline does not require a rebuild."
    )
    assert "The pipeline requires a rebuild." in prompt
    assert "The pipeline does not require a rebuild." in prompt


def test_the_prompt_without_conflicts_is_unchanged_from_phase_6():
    # Passing no summaries must produce exactly the prompt that existed
    # before conflicts were a concept.
    bundle = bundle_of("The build pipeline has 3 steps.")
    assert build_synthesis_prompt("q", bundle) == build_synthesis_prompt("q", bundle, [])


# ======================================================
# 4. Synthesis behaviour
# ======================================================
# What a model does with a prompt is the model's business and cannot be
# asserted deterministically. What can be asserted is that everything it
# needs in order to behave correctly is in front of it: both sides, both
# citations, the disagreement named, and the instruction not to resolve it.
CONFLICTING = (
    "The pipeline requires a rebuild after every merge.",
    "The pipeline does not require a rebuild after every merge.",
)


def prompt_seen_by_model(*texts, **kwargs) -> str:
    seen = []
    items = [
        {"type": "note", "id": index + 1, "text": text, "combined_score": 0.9 - index * 0.05}
        for index, text in enumerate(texts)
    ]
    answer_with_evidence(
        "does the pipeline need a rebuild", items,
        generate=lambda prompt: (seen.append(prompt), "answer")[1], **kwargs
    )
    return seen[0]


def test_the_model_is_told_about_the_conflict():
    assert "Conflicts Detected:" in prompt_seen_by_model(*CONFLICTING)


def test_the_model_is_told_not_to_resolve_it_unprompted():
    prompt = prompt_seen_by_model(*CONFLICTING)
    assert "Do not resolve them by choosing a side" in prompt
    assert "Do not average them or silently pick one." in prompt


def test_the_model_can_cite_both_sides():
    prompt = prompt_seen_by_model(*CONFLICTING)
    assert "[note:1]" in prompt and "[note:2]" in prompt


def test_the_model_is_told_not_to_invent_an_answer():
    assert "Never invent an answer." in prompt_seen_by_model(*CONFLICTING)


def test_a_clean_bundle_does_not_mention_conflicts_to_the_model():
    prompt = prompt_seen_by_model("The build pipeline has 3 steps.")
    assert "Conflicts Detected" not in prompt


def test_the_engine_returns_the_answer_unchanged():
    answer = answer_with_evidence(
        "q",
        [{"type": "note", "id": 1, "text": CONFLICTING[0], "combined_score": 0.9},
         {"type": "note", "id": 2, "text": CONFLICTING[1], "combined_score": 0.8}],
        generate=lambda prompt: "both sides noted",
    )
    assert answer == "both sides noted"


def test_synthesize_detects_conflicts_from_a_prebuilt_bundle():
    seen = []
    synthesize(
        "q", bundle_of(*CONFLICTING),
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert "Conflicts Detected:" in seen[0]


def test_an_empty_bundle_never_reaches_detection(monkeypatch):
    monkeypatch.setattr(
        conflict_detection, "detect_conflicts",
        lambda bundle: (_ for _ in ()).throw(AssertionError("nothing to compare")),
    )
    from backend.aria_synthesis.synthesis_engine import NO_EVIDENCE_ANSWER

    assert answer_with_evidence("q", [], generate=lambda p: "x") == NO_EVIDENCE_ANSWER


# ======================================================
# 5. Determinism
# ======================================================
def test_the_same_bundle_gives_the_same_conflicts():
    bundle = bundle_of(*CONFLICTING)
    assert detect_conflicts(bundle) == detect_conflicts(bundle)


def test_the_same_conflicts_give_the_same_summaries():
    bundle = bundle_of(*CONFLICTING)
    assert summarize_conflicts(detect_conflicts(bundle)) == summarize_conflicts(
        detect_conflicts(bundle)
    )


def test_the_whole_chain_is_deterministic():
    prompts = [prompt_with(*CONFLICTING) for _ in range(5)]
    assert all(prompt == prompts[0] for prompt in prompts)


def test_conflicts_are_ordered_deterministically():
    found = conflicts_between(
        "The build pipeline has 3 steps. The radar endpoint is active.",
        "The build pipeline has 4 steps. The radar endpoint is deprecated.",
    )
    assert [conflict.kind for conflict in found] == sorted(c.kind for c in found)


def test_source_order_does_not_change_the_summaries():
    forward = summaries_between(*CONFLICTING)
    backward = summaries_between(*reversed(CONFLICTING))
    assert forward == backward


def test_detection_does_not_mutate_the_bundle():
    bundle = bundle_of(*CONFLICTING)
    before = (list(bundle.notes), list(bundle.files), dict(bundle.meta))
    detect_conflicts(bundle)
    assert (bundle.notes, bundle.files, bundle.meta) == before


# ======================================================
# 6. No retrieval drift
# ======================================================
def test_ranking_weights_are_unchanged():
    assert memory_ranking.WEIGHT_SEMANTIC == 0.45
    assert memory_ranking.WEIGHT_KEYWORD == 0.25
    assert memory_ranking.WEIGHT_RECENCY == 0.20
    assert memory_ranking.WEIGHT_TYPE == 0.10


def test_score_floors_are_unchanged():
    assert semantic.DEFAULT_MIN_SCORE == 0.6
    assert ingestion.DEFAULT_MIN_SCORE == 0.55


def test_scores_reach_the_statements_unchanged():
    found = conflicts_between(*CONFLICTING)
    assert {statement.score for statement in found[0].statements} == {0.9, 0.85}


def test_hybrid_search_output_is_unchanged_by_conflict_detection(db):
    for text in ("the release pipeline requires a rebuild",
                 "the release pipeline does not require a rebuild"):
        semantic.index_note(notes_store.save_note(text))

    before = aria_memory.search_hybrid("release pipeline", min_score=0.0)
    bundle = build_evidence_bundle("release pipeline", before, now="FIXED")
    detect_conflicts(bundle)
    after = aria_memory.search_hybrid("release pipeline", min_score=0.0)

    assert [i["id"] for i in before] == [i["id"] for i in after]
    assert [i["keyword_score"] for i in before] == [i["keyword_score"] for i in after]
    assert [i["semantic_score"] for i in before] == [i["semantic_score"] for i in after]


def test_a_real_retrieval_conflict_is_found_end_to_end(db):
    # Real notes, real retrieval, real ranking -- the shapes line up all the
    # way from the database to the conflict section of the prompt.
    for text in ("The pipeline requires a rebuild after every merge.",
                 "The pipeline does not require a rebuild after every merge."):
        semantic.index_note(notes_store.save_note(text))

    seen = []
    answer_with_evidence(
        "does the pipeline require a rebuild",
        aria_memory.search_ranked("pipeline rebuild"),
        generate=lambda prompt: (seen.append(prompt), "answer")[1],
    )
    assert "Conflicts Detected:" in seen[0]
    assert "Sources disagree on whether the pipeline" in seen[0]
