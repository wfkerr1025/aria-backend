# backend/tests/test_phase6_synthesis.py
#
# Phase 6: multi-source answer synthesis.
#
# The bundle and the prompt are pure, so almost everything here runs on
# plain dicts with no database, no embedding backend and no model -- which
# is the point of the seam. The few tests that go through real retrieval
# exist to prove the shapes actually line up, and the ones that reach the
# synthesis engine pass their own generate() so nothing loads a model.
#
# The invariant this suite guards hardest is that Phase 6 is downstream:
# building a bundle must not change, reorder or mutate anything retrieval
# returned.

from __future__ import annotations

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory
from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.aria_memory import memory_ranking
from backend.aria_synthesis import bundle_builder, synthesis_engine, synthesis_prompt
from backend.aria_synthesis.bundle_builder import (
    DEFAULT_FILE_LIMIT,
    DEFAULT_NOTE_LIMIT,
    build_evidence_bundle,
)
from backend.aria_synthesis.evidence_bundle import (
    SNIPPET_CHARS,
    EvidenceBundle,
    EvidenceFile,
    EvidenceNote,
    snippet,
)
from backend.aria_synthesis.synthesis_engine import (
    NO_EVIDENCE_ANSWER,
    answer_with_evidence,
    synthesize,
)
from backend.aria_synthesis.synthesis_prompt import (
    NOT_FOUND_PHRASE,
    SYNTHESIS_RULES,
    build_synthesis_prompt,
)
from backend.files import file_ingestion as ingestion
from backend.llm.query_expansion import ExpansionDomain

D = ExpansionDomain

# Two notes that disagree about the same fact, which is the case synthesis
# exists to handle rather than smooth over.
CONFLICT_A = "The release pipeline runs nightly at 02:00 UTC."
CONFLICT_B = "The release pipeline was moved to 06:00 UTC in March."


def note_item(note_id: int, text: str, score: float = 0.5, **extra) -> dict:
    return {"type": "note", "id": note_id, "text": text, "combined_score": score, **extra}


def file_item(file_id: int, text: str, score: float = 0.5, **extra) -> dict:
    item = {
        "type": "file_chunk",
        "file_id": file_id,
        "text": text,
        "combined_score": score,
        "path": f"docs/file{file_id}.md",
    }
    item.update(extra)
    return item


def bundle_of(*items, query: str = "release pipeline", **kwargs) -> EvidenceBundle:
    kwargs.setdefault("now", "FIXED")
    return build_evidence_bundle(query, list(items), **kwargs)


# ======================================================
# 1. Bundle construction
# ======================================================
def test_notes_and_files_are_separated_by_type():
    bundle = bundle_of(note_item(1, "a"), file_item(2, "b"), {"type": "chunk", "note_id": 3, "chunk": "c"})
    assert [note.id for note in bundle.notes] == [1, 3]
    assert len(bundle.files) == 1


def test_notes_are_sorted_by_score():
    bundle = bundle_of(
        note_item(1, "low", 0.10), note_item(2, "high", 0.90), note_item(3, "mid", 0.50)
    )
    assert [note.id for note in bundle.notes] == [2, 3, 1]


def test_files_are_sorted_by_score():
    bundle = bundle_of(file_item(1, "low", 0.10), file_item(2, "high", 0.90))
    assert [item.score for item in bundle.files] == [0.90, 0.10]


def test_equal_scores_keep_retrieval_order():
    # Stable sort: a tie is broken by whatever order retrieval produced,
    # never by something arbitrary.
    bundle = bundle_of(*[note_item(n, f"note {n}", 0.5) for n in (4, 2, 9, 1)])
    assert [note.id for note in bundle.notes] == [4, 2, 9, 1]


def test_notes_are_capped():
    bundle = bundle_of(*[note_item(n, f"note {n}", n / 100) for n in range(20)])
    assert len(bundle.notes) == DEFAULT_NOTE_LIMIT


def test_files_are_capped():
    bundle = bundle_of(*[file_item(n, f"chunk {n}", n / 100) for n in range(20)])
    assert len(bundle.files) == DEFAULT_FILE_LIMIT


def test_the_cap_keeps_the_best_not_the_first():
    bundle = bundle_of(*[note_item(n, f"note {n}", n / 100) for n in range(20)])
    assert [note.id for note in bundle.notes] == [19, 18, 17, 16, 15]


def test_the_caps_are_configurable():
    items = [note_item(n, f"note {n}", n / 100) for n in range(20)]
    assert len(bundle_of(*items, note_limit=2).notes) == 2


def test_dropped_candidates_are_recorded():
    bundle = bundle_of(*[note_item(n, f"note {n}", n / 100) for n in range(8)])
    assert bundle.meta["candidates"]["notes"] == 8
    assert bundle.meta["dropped"]["notes"] == 3


def test_an_unknown_item_type_is_dropped():
    bundle = bundle_of(note_item(1, "a"), {"type": "context", "value": "x"})
    assert bundle.item_count == 1


def test_an_empty_retrieval_gives_an_empty_bundle():
    bundle = bundle_of()
    assert bundle.is_empty
    assert not bundle
    assert bundle.item_count == 0


def test_none_retrieval_is_tolerated():
    assert build_evidence_bundle("q", None, now="FIXED").is_empty


# --- snippets ---
def test_a_short_snippet_is_the_whole_text():
    assert snippet("short note") == "short note"


def test_a_long_snippet_is_truncated_into_the_band():
    long_text = " ".join(["word"] * 200)
    result = snippet(long_text)
    assert 200 <= len(result) <= SNIPPET_CHARS + 1
    assert result.endswith("…")


def test_a_snippet_collapses_whitespace():
    # Stability, not just determinism: re-wrapping a note must not change
    # its snippet.
    assert snippet("two   lines\n\there") == "two lines here"


def test_a_snippet_is_deterministic():
    text = " ".join(["alpha beta"] * 60)
    assert snippet(text) == snippet(text)


def test_a_snippet_breaks_on_a_word_boundary():
    result = snippet(" ".join(["word"] * 200))
    assert not result.rstrip("…").endswith("wor")


def test_a_snippet_survives_text_with_no_spaces():
    # The floor stops the word-boundary search from eating the whole thing.
    result = snippet("x" * 400)
    assert len(result) == SNIPPET_CHARS + 1


def test_an_empty_text_gives_an_empty_snippet():
    assert snippet("") == ""
    assert snippet(None) == ""


def test_the_bundles_snippets_are_deterministic():
    items = [note_item(1, " ".join(["alpha"] * 200))]
    assert bundle_of(*items).notes[0].snippet == bundle_of(*items).notes[0].snippet


def test_a_title_is_derived_from_the_text():
    bundle = bundle_of(note_item(1, "The release pipeline runs nightly at 02:00 UTC."))
    assert bundle.notes[0].title == "The release pipeline runs nightly at 02:00 UTC."


def test_a_long_title_is_shortened():
    bundle = bundle_of(note_item(1, " ".join(["word"] * 100)))
    assert len(bundle.notes[0].title) <= 61


# --- provenance ---
def test_note_provenance_is_the_note_id():
    assert bundle_of(note_item(12, "x")).notes[0].provenance == "note:12"


def test_a_note_chunk_uses_its_note_id():
    bundle = bundle_of({"type": "chunk", "note_id": 7, "chunk": "text", "combined_score": 0.5})
    assert bundle.notes[0].provenance == "note:7"


def test_file_provenance_carries_path_and_section():
    bundle = bundle_of(file_item(1, "x", path="docs/build.md", section="Pipeline"))
    assert bundle.files[0].provenance == "file:build.md#Pipeline"


def test_file_provenance_without_a_section_omits_the_anchor():
    bundle = bundle_of(file_item(1, "x", path="docs/build.md", section=None))
    assert bundle.files[0].provenance == "file:build.md"


def test_markdown_hashes_are_stripped_from_a_section():
    bundle = bundle_of(file_item(1, "x", path="b.md", section="## Build pipeline"))
    assert bundle.files[0].provenance == "file:b.md#Build pipeline"


def test_a_windows_path_is_reduced_to_its_basename():
    bundle = bundle_of(file_item(1, "x", path=r"C:\docs\build.md", section=""))
    assert bundle.files[0].provenance == "file:build.md"


def test_the_full_path_is_kept_on_the_struct():
    bundle = bundle_of(file_item(1, "x", path="docs/deep/build.md"))
    assert bundle.files[0].path == "docs/deep/build.md"


def test_note_labels_read_as_prose():
    assert bundle_of(note_item(12, "x")).notes[0].label == "note 12"


# --- domain ---
def test_the_domain_is_detected_from_the_query():
    assert bundle_of(note_item(1, "x"), query="how do I instance a prefab").domain is D.UNITY


def test_the_domain_is_stamped_on_every_item():
    bundle = bundle_of(note_item(1, "x"), file_item(2, "y"), query="prefab workflow")
    assert all(item.domain is D.UNITY for item in (*bundle.notes, *bundle.files))


def test_a_routing_intent_overrides_the_query():
    bundle = bundle_of(note_item(1, "x"), query="prefab", routing_intent="weather.query")
    assert bundle.domain is D.WEATHER


def test_the_cleaned_query_is_recorded():
    bundle = bundle_of(note_item(1, "x"), query="tell me about the release pipeline")
    assert bundle.cleaned_query == "release pipeline"
    assert bundle.query == "tell me about the release pipeline"


def test_meta_records_the_floors():
    bundle = bundle_of(note_item(1, "x"))
    assert bundle.meta["note_floor"] == semantic.DEFAULT_MIN_SCORE
    assert bundle.meta["file_floor"] == ingestion.DEFAULT_MIN_SCORE


# --- scores ---
def test_the_combined_score_is_preferred():
    item = note_item(1, "x", 0.9)
    item["semantic_score"] = 0.1
    assert bundle_of(item).notes[0].score == 0.9


def test_an_unranked_item_falls_back_to_its_retrieval_score():
    item = {"type": "note", "id": 1, "text": "x", "semantic_score": 0.42}
    assert bundle_of(item).notes[0].score == 0.42


def test_an_item_with_no_score_is_zero():
    assert bundle_of({"type": "note", "id": 1, "text": "x"}).notes[0].score == 0.0


# ======================================================
# 2. Prompt construction
# ======================================================
def prompt_of(*items, query: str = "release pipeline", **kwargs) -> str:
    bundle = bundle_of(*items, query=query, **kwargs)
    return build_synthesis_prompt(query, bundle)


def test_the_prompt_includes_the_query():
    assert "User Query: release pipeline" in prompt_of(note_item(1, "x"))


def test_the_prompt_includes_the_cleaned_query_when_it_differs():
    prompt = prompt_of(note_item(1, "x"), query="tell me about the release pipeline")
    assert "Search Topic: release pipeline" in prompt


def test_the_prompt_omits_the_cleaned_query_when_it_is_the_same():
    assert "Search Topic:" not in prompt_of(note_item(1, "x"))


def test_the_prompt_has_every_required_section():
    prompt = prompt_of(note_item(1, "x"), file_item(2, "y"))
    for heading in ("User Query:", "Evidence Summary:", "Notes:", "Files:",
                    "Synthesis Instructions:", "Produce a single coherent answer."):
        assert heading in prompt


def test_the_prompt_includes_the_evidence():
    prompt = prompt_of(note_item(1, "the pipeline runs nightly"))
    assert "the pipeline runs nightly" in prompt


def test_the_prompt_includes_provenance_inline():
    prompt = prompt_of(note_item(12, "x"), file_item(1, "y", path="b.md", section="Build"))
    assert "[note:12]" in prompt
    assert "[file:b.md#Build]" in prompt


def test_the_prompt_shows_scores():
    # "Prefer higher-score evidence" is unactionable if the scores are
    # invisible to the model.
    assert "(score 0.81)" in prompt_of(note_item(1, "x", 0.81))


def test_every_synthesis_rule_is_present():
    prompt = prompt_of(note_item(1, "x"))
    for rule in SYNTHESIS_RULES:
        assert rule in prompt


def test_the_rules_are_numbered():
    prompt = prompt_of(note_item(1, "x"))
    for number in range(1, len(SYNTHESIS_RULES) + 1):
        assert f"\n{number}. " in prompt


def test_there_are_eight_rules():
    assert len(SYNTHESIS_RULES) == 8


@pytest.mark.parametrize("fragment", [
    "Use ONLY the evidence",
    "disagree",
    NOT_FOUND_PHRASE,
    "Never invent",
    "Merge overlapping",
    "Cite lightly",
    "Do not quote the evidence back verbatim",
    "reconcile",
])
def test_each_required_instruction_appears(fragment):
    assert fragment in prompt_of(note_item(1, "x"))


def test_an_empty_section_says_none_rather_than_vanishing():
    # The model should see that files were searched and found nothing, not
    # be left to infer it from a missing heading.
    prompt = prompt_of(note_item(1, "x"))
    assert "Files:\n(none)" in prompt


def test_the_summary_counts_the_evidence():
    prompt = prompt_of(note_item(1, "a"), note_item(2, "b"), file_item(3, "c"))
    assert "2 notes and 1 file excerpt retrieved" in prompt


def test_the_summary_names_the_domain():
    prompt = prompt_of(note_item(1, "x"), query="prefab workflow")
    assert "domain: unity" in prompt


def test_the_summary_admits_what_was_held_back():
    prompt = prompt_of(*[note_item(n, f"note {n}", n / 100) for n in range(8)])
    assert "3 lower-scoring item(s) were not included." in prompt


def test_an_empty_bundle_still_builds_a_prompt():
    prompt = build_synthesis_prompt("q", bundle_of())
    assert "No evidence was retrieved" in prompt
    assert NOT_FOUND_PHRASE in prompt


def test_the_prompt_never_leaks_the_timestamp():
    # meta is provenance for humans, not evidence for the model -- and a
    # clock reading in the prompt would break determinism outright.
    prompt = prompt_of(note_item(1, "x"))
    assert "FIXED" not in prompt
    assert "built_at" not in prompt


# ======================================================
# 3. Conflict handling and missing evidence
# ======================================================
def test_conflicting_notes_both_reach_the_prompt():
    # Synthesis cannot report a disagreement it was never shown; the higher
    # scorer must not silently displace the other.
    prompt = prompt_of(note_item(1, CONFLICT_A, 0.9), note_item(2, CONFLICT_B, 0.8))
    assert "02:00 UTC" in prompt
    assert "06:00 UTC" in prompt


def test_the_prompt_tells_the_model_to_name_the_disagreement():
    prompt = prompt_of(note_item(1, CONFLICT_A, 0.9), note_item(2, CONFLICT_B, 0.8))
    assert "If sources disagree, say so explicitly" in prompt
    assert "Do not average them" in prompt


def test_conflicting_sources_keep_distinct_provenance():
    bundle = bundle_of(note_item(1, CONFLICT_A, 0.9), note_item(2, CONFLICT_B, 0.8))
    assert bundle.notes[0].provenance != bundle.notes[1].provenance


def test_missing_evidence_answers_not_found_without_a_model():
    def explode(prompt):
        raise AssertionError("an empty bundle must never reach a model")

    assert answer_with_evidence("anything", [], generate=explode) == NO_EVIDENCE_ANSWER
    assert NOT_FOUND_PHRASE in NO_EVIDENCE_ANSWER


def test_the_not_found_phrase_is_shared_between_engine_and_prompt():
    # One phrase, so "not found" means the same thing whether the model said
    # it or the engine short-circuited.
    assert NOT_FOUND_PHRASE in NO_EVIDENCE_ANSWER
    assert NOT_FOUND_PHRASE in build_synthesis_prompt("q", bundle_of(note_item(1, "x")))


def test_multiple_domains_are_reported_for_reconciliation():
    bundle = EvidenceBundle(
        query="q", cleaned_query="q", domain=D.UNITY,
        notes=[EvidenceNote(1, "t", "s", 0.5, D.UNITY, "note:1")],
        files=[EvidenceFile("b.md", "S", "s", 0.5, D.BACKEND, "file:b.md#S")],
    )
    assert bundle.domains == [D.UNITY, D.BACKEND]
    assert "domain: unity, backend" in build_synthesis_prompt("q", bundle)


# ======================================================
# 4. Determinism
# ======================================================
def test_the_same_retrieval_gives_the_same_bundle():
    items = [note_item(1, CONFLICT_A, 0.9), file_item(2, "chunk text", 0.4)]
    assert bundle_of(*items) == bundle_of(*items)


def test_the_same_bundle_gives_the_same_prompt():
    items = [note_item(1, CONFLICT_A, 0.9), file_item(2, "chunk text", 0.4)]
    assert prompt_of(*items) == prompt_of(*items)


def test_the_prompt_is_identical_across_many_builds():
    items = [note_item(n, f"note number {n}", n / 10) for n in range(6)]
    prompts = [prompt_of(*items) for _ in range(5)]
    assert all(prompt == prompts[0] for prompt in prompts)


def test_input_order_does_not_change_the_prompt():
    items = [note_item(1, "a", 0.9), note_item(2, "b", 0.5), note_item(3, "c", 0.1)]
    assert prompt_of(*items) == prompt_of(*reversed(items))


def test_only_the_timestamp_differs_between_two_live_builds():
    items = [note_item(1, "a", 0.9)]
    first = build_evidence_bundle("q", items)
    second = build_evidence_bundle("q", items)
    assert first.notes == second.notes
    assert first.files == second.files
    assert {k: v for k, v in first.meta.items() if k != "built_at"} == {
        k: v for k, v in second.meta.items() if k != "built_at"
    }


# ======================================================
# 5. Retrieval is untouched
# ======================================================
def test_building_a_bundle_does_not_mutate_the_items():
    items = [note_item(1, "a", 0.9), file_item(2, "b", 0.4)]
    before = [dict(item) for item in items]
    build_evidence_bundle("q", items, now="FIXED")
    assert items == before


def test_building_a_bundle_does_not_reorder_the_caller_s_list():
    items = [note_item(1, "a", 0.1), note_item(2, "b", 0.9)]
    build_evidence_bundle("q", items, now="FIXED")
    assert [item["id"] for item in items] == [1, 2]


def test_scores_reach_the_bundle_unchanged():
    bundle = bundle_of(note_item(1, "a", 0.8125))
    assert bundle.notes[0].score == 0.8125


def test_ranking_weights_are_unchanged():
    assert memory_ranking.WEIGHT_SEMANTIC == 0.45
    assert memory_ranking.WEIGHT_KEYWORD == 0.25
    assert memory_ranking.WEIGHT_RECENCY == 0.20
    assert memory_ranking.WEIGHT_TYPE == 0.10


def test_score_floors_are_unchanged():
    assert semantic.DEFAULT_MIN_SCORE == 0.6
    assert ingestion.DEFAULT_MIN_SCORE == 0.55


def test_hybrid_search_output_is_unchanged_by_synthesis(db):
    note_id = notes_store.save_note("the release pipeline runs nightly")
    semantic.index_note(note_id)

    before = aria_memory.search_hybrid("release pipeline", min_score=0.0)
    build_evidence_bundle("release pipeline", before, now="FIXED")
    after = aria_memory.search_hybrid("release pipeline", min_score=0.0)

    assert [i["id"] for i in before] == [i["id"] for i in after]
    assert [i["keyword_score"] for i in before] == [i["keyword_score"] for i in after]
    assert [i["semantic_score"] for i in before] == [i["semantic_score"] for i in after]


def test_a_real_hybrid_result_bundles_cleanly(db):
    # The shapes actually line up: no synthetic dicts, no hand-built rows.
    for text in ("the release pipeline runs nightly", "the pipeline was moved to 06:00"):
        semantic.index_note(notes_store.save_note(text))

    bundle = build_evidence_bundle(
        "release pipeline",
        aria_memory.search_ranked("release pipeline"),
        now="FIXED",
    )
    assert bundle.notes
    assert all(note.id is not None for note in bundle.notes)
    assert all(note.snippet for note in bundle.notes)
    assert all(note.provenance.startswith("note:") for note in bundle.notes)


def test_a_real_file_result_bundles_cleanly(db, tmp_path):
    path = tmp_path / "build.md"
    path.write_text("# Pipeline\n\nNightly builds upload artifacts to staging.", encoding="utf-8")
    ingestion.ingest_and_index(str(path))

    hits = ingestion.search_files_semantic("pipeline", min_score=0.0)
    bundle = build_evidence_bundle("pipeline", hits, now="FIXED")
    assert bundle.files
    assert bundle.files[0].path.endswith("build.md")
    assert bundle.files[0].provenance.startswith("file:build.md")


# ======================================================
# The builder and prompt call no model
# ======================================================
def test_the_builder_calls_no_model(monkeypatch):
    from backend.llm import semantic_embeddings

    def explode(*args, **kwargs):
        raise AssertionError("the bundle builder must not embed or generate")

    monkeypatch.setattr(semantic_embeddings, "embed_texts_semantic", explode)
    monkeypatch.setattr(semantic_embeddings, "embed_text_semantic", explode)
    bundle = bundle_of(note_item(1, "a"), file_item(2, "b"))
    assert bundle.item_count == 2


def test_the_prompt_builder_calls_no_model(monkeypatch):
    from backend.llm import semantic_embeddings

    def explode(*args, **kwargs):
        raise AssertionError("the prompt builder must not embed or generate")

    monkeypatch.setattr(semantic_embeddings, "embed_texts_semantic", explode)
    bundle = bundle_of(note_item(1, "a"))
    assert build_synthesis_prompt("q", bundle)


def test_the_builder_survives_a_missing_embedding_backend(monkeypatch):
    from backend.llm import semantic_embeddings

    monkeypatch.setattr(
        semantic_embeddings, "backend_id",
        lambda: (_ for _ in ()).throw(RuntimeError("no backend")),
    )
    bundle = bundle_of(note_item(1, "a"))
    assert bundle.meta["embedding_backend"] is None


# ======================================================
# Synthesis engine
# ======================================================
def test_the_engine_passes_the_prompt_to_the_model():
    seen = []

    def generate(prompt):
        seen.append(prompt)
        return "an answer"

    answer = answer_with_evidence("release pipeline", [note_item(1, CONFLICT_A, 0.9)],
                                  generate=generate)
    assert answer == "an answer"
    assert "User Query: release pipeline" in seen[0]
    assert CONFLICT_A in seen[0]


def test_the_engine_returns_the_models_answer_stripped():
    assert answer_with_evidence("q", [note_item(1, "a")], generate=lambda p: "  spaced  ") == "spaced"


def test_an_unavailable_model_falls_back_to_the_evidence():
    answer = answer_with_evidence("q", [note_item(12, CONFLICT_A)], generate=lambda p: "")
    assert "could not synthesize" in answer
    assert "note 12" in answer
    assert CONFLICT_A in answer


def test_a_raising_model_falls_back_rather_than_propagating():
    def explode(prompt):
        raise RuntimeError("model died")

    answer = answer_with_evidence("q", [note_item(1, CONFLICT_A)], generate=explode)
    assert "could not synthesize" in answer


def test_synthesize_accepts_a_prebuilt_bundle():
    bundle = bundle_of(note_item(1, CONFLICT_A))
    assert synthesize("q", bundle, generate=lambda p: "answer") == "answer"


def test_the_engine_honours_the_caps():
    seen = []
    answer_with_evidence(
        "q",
        [note_item(n, f"note {n}", n / 100) for n in range(10)],
        note_limit=2,
        generate=lambda p: (seen.append(p), "x")[1],
    )
    assert seen[0].count("[note:") == 2


def test_the_engine_is_deterministic():
    items = [note_item(1, CONFLICT_A, 0.9), note_item(2, CONFLICT_B, 0.8)]
    seen = []

    def generate(prompt):
        seen.append(prompt)
        return "answer"

    for _ in range(3):
        answer_with_evidence("release pipeline", items, generate=generate)
    assert all(prompt == seen[0] for prompt in seen)


def test_default_generation_is_greedy():
    # A synthesis that reworded itself between identical runs would undo the
    # point of a deterministic prompt.
    assert synthesis_engine.TEMPERATURE == 0.0
    assert synthesis_engine.SEED == 0


def test_the_engine_passes_a_routing_intent_through():
    seen = []
    answer_with_evidence(
        "prefab", [note_item(1, "x")], routing_intent="weather.query",
        generate=lambda p: (seen.append(p), "x")[1],
    )
    assert "domain: weather" in seen[0]


def test_the_package_reexports_the_public_api():
    import backend.aria_synthesis as package

    assert package.build_evidence_bundle is build_evidence_bundle
    assert package.build_synthesis_prompt is build_synthesis_prompt
    assert package.answer_with_evidence is answer_with_evidence
    assert package.EvidenceBundle is EvidenceBundle


def test_the_package_rejects_an_unknown_attribute():
    import backend.aria_synthesis as package

    with pytest.raises(AttributeError):
        package.does_not_exist
