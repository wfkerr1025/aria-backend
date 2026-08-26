# backend/tests/test_keyword_deframing.py
#
# De-framing on the keyword half of hybrid retrieval.
#
# The semantic half already de-framed internally; the keyword half was still
# running a SQL LIKE against the user's whole sentence, which matched nothing
# for any framed question, and scoring coverage over framing terms the notes
# would never contain. These tests pin that the keyword side now sees the
# same cleaned query, that nothing else moved, and that the raw query is
# still what gets displayed.

from __future__ import annotations

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory
from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.aria_memory import memory_ranking
from backend.llm import memory_prompting, semantic_embeddings
from backend.llm.query_deframing import deframe_query

semantic_only = pytest.mark.skipif(
    not semantic_embeddings.is_semantic(),
    reason="active embedding backend is the lexical fallback, which cannot encode meaning",
)

PIPELINE = "the release pipeline runs nightly and publishes to staging"
COFFEE = "the office coffee machine is serviced on Fridays"
# Deliberately stuffed with framing vocabulary: "search", "documents",
# "explain", "tell". Before de-framing this note picked up keyword coverage
# from queries it had nothing to do with.
FRAMING_NOISE = "I need to search the documents and explain the results, then tell my manager"


@pytest.fixture
def notes(db):
    ids = {}
    for name, text in [("pipeline", PIPELINE), ("coffee", COFFEE), ("noise", FRAMING_NOISE)]:
        note_id = notes_store.save_note(text)
        semantic.index_note(note_id)
        ids[name] = note_id
    return ids


def by_id(items) -> dict:
    return {item["id"]: item for item in items if item.get("type") == "note"}


# ======================================================
# 1. Correct de-framing
# ======================================================
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("search my documents for login credentials", "login credentials"),
        ("tell me about the pipeline", "pipeline"),
        ("please explain the coffee machine", "coffee machine"),
    ],
)
def test_the_documented_cases(raw, expected):
    assert deframe_query(raw) == expected


# ======================================================
# 2. Keyword search uses the cleaned query
# ======================================================
def test_the_raw_query_matches_nothing(notes):
    # A LIKE against the whole sentence is a search for that literal phrase.
    assert notes_store.search_notes_keyword("tell me about the pipeline") == []


def test_the_cleaned_query_matches(notes):
    hits = notes_store.search_notes_keyword("pipeline")
    assert [row["id"] for row in hits] == [notes["pipeline"]]


def test_hybrid_now_finds_the_note_by_keyword(notes):
    items = by_id(aria_memory.search_hybrid("tell me about the pipeline", min_score=0.0))
    assert notes["pipeline"] in items
    assert items[notes["pipeline"]]["keyword_score"] > 0


def test_raw_and_cleaned_hybrid_agree(notes):
    framed = aria_memory.search_hybrid("tell me about the pipeline", min_score=0.0)
    bare = aria_memory.search_hybrid("pipeline", min_score=0.0)
    assert by_id(framed).keys() == by_id(bare).keys()


def test_keyword_coverage_is_no_longer_diluted(notes):
    # "pipeline" is one of five terms in the framed query; scoring the
    # framed form would cap a perfect match at 0.2.
    items = by_id(aria_memory.search_hybrid("tell me about the pipeline", min_score=0.0))
    assert items[notes["pipeline"]]["keyword_score"] == pytest.approx(1.0)


def test_framing_words_no_longer_match_prose(notes):
    # The noise note contains "search", "documents", "explain" and "tell";
    # a framed query must not pick up coverage from them.
    items = by_id(aria_memory.search_hybrid("tell me about the coffee machine", min_score=0.0))
    if notes["noise"] in items:
        assert items[notes["noise"]]["keyword_score"] == 0.0


def test_the_relevant_note_outscores_the_noise_note(notes):
    items = by_id(aria_memory.search_hybrid("please explain the coffee machine", min_score=0.0))
    assert items[notes["coffee"]]["keyword_score"] > items.get(
        notes["noise"], {"keyword_score": 0.0}
    )["keyword_score"]


def test_relevance_scoring_is_unchanged_for_a_bare_query(notes):
    # The scoring logic itself did not change; only what is fed to it.
    assert aria_memory._keyword_relevance("coffee machine", {"text": COFFEE}) == pytest.approx(1.0)
    assert aria_memory._keyword_relevance("coffee zebra", {"text": COFFEE}) == pytest.approx(0.5)
    assert aria_memory._keyword_relevance("zebra giraffe", {"text": COFFEE}) == 0.0


def test_an_empty_query_still_scores_zero():
    assert aria_memory._keyword_relevance("", {"text": COFFEE}) == 0.0


# ======================================================
# 3. Only the keyword signal moved
# ======================================================
def test_semantic_scores_are_untouched(notes):
    framed = aria_memory.search_hybrid("tell me about the pipeline", min_score=0.0)
    bare = aria_memory.search_hybrid("pipeline", min_score=0.0)
    framed_scores = {i["id"]: i.get("semantic_score") for i in framed}
    bare_scores = {i["id"]: i.get("semantic_score") for i in bare}
    for note_id, score in framed_scores.items():
        if score is not None and bare_scores.get(note_id) is not None:
            assert score == pytest.approx(bare_scores[note_id])


def test_semantic_search_was_not_changed(notes):
    # search_semantic de-frames internally and still takes the raw query.
    framed = semantic.search_semantic("tell me about the pipeline", min_score=0.0)
    bare = semantic.search_semantic("pipeline", min_score=0.0)
    assert [h["note_id"] for h in framed] == [h["note_id"] for h in bare]
    assert [h["score"] for h in framed] == pytest.approx([h["score"] for h in bare])


def test_ranking_weights_are_unchanged():
    assert memory_ranking.WEIGHT_SEMANTIC == 0.45
    assert memory_ranking.WEIGHT_KEYWORD == 0.25
    assert memory_ranking.WEIGHT_RECENCY == 0.20
    assert memory_ranking.WEIGHT_TYPE == 0.10


def test_score_floors_are_unchanged():
    from backend.files.file_ingestion import DEFAULT_MIN_SCORE as FILE_FLOOR

    assert semantic.DEFAULT_MIN_SCORE == 0.6
    assert FILE_FLOOR == 0.55


def test_combined_score_moves_only_through_the_keyword_term(notes):
    # Recompute the same item with the old and new keyword score; the
    # difference must be exactly the keyword weight times the delta.
    item = {
        "id": 1, "type": "note", "text": PIPELINE,
        "semantic_score": 0.7, "recency_score": 0.9,
    }
    old = memory_ranking.normalize_scores(0.2, 0.7, 0.9, 1.0)
    new = memory_ranking.normalize_scores(1.0, 0.7, 0.9, 1.0)
    expected_delta = memory_ranking.WEIGHT_KEYWORD * (1.0 - 0.2) / memory_ranking.MAX_COMBINED
    assert new - old == pytest.approx(expected_delta)
    assert item["semantic_score"] == 0.7


def test_ordering_is_deterministic(notes):
    runs = [
        [i["id"] for i in aria_memory.search_ranked("tell me about the pipeline")]
        for _ in range(5)
    ]
    assert all(run == runs[0] for run in runs)


def test_hybrid_results_are_deterministic(notes):
    first = aria_memory.search_hybrid("please explain the coffee machine", min_score=0.0)
    second = aria_memory.search_hybrid("please explain the coffee machine", min_score=0.0)
    assert [i["id"] for i in first] == [i["id"] for i in second]
    assert [i["keyword_score"] for i in first] == [i["keyword_score"] for i in second]


# ======================================================
# 4. Integration
# ======================================================
def test_the_relevant_note_ranks_first(notes):
    ranked = aria_memory.search_ranked("tell me about the pipeline")
    assert ranked
    assert ranked[0]["id"] == notes["pipeline"]


def test_a_framed_query_ranks_like_a_bare_one(notes):
    framed = [i["id"] for i in aria_memory.search_ranked("please explain the coffee machine")]
    bare = [i["id"] for i in aria_memory.search_ranked("coffee machine")]
    assert framed[0] == bare[0] == notes["coffee"]


def test_the_noise_note_does_not_win_on_framing_alone(notes):
    ranked = aria_memory.search_ranked("search my documents for the coffee machine")
    assert ranked[0]["id"] != notes["noise"]


def test_de_framing_raises_the_winner_combined_score(notes):
    items = by_id(aria_memory.search_hybrid("tell me about the pipeline", min_score=0.0))
    winner = items[notes["pipeline"]]
    # The same item scored with the framing left in.
    diluted = dict(winner, keyword_score=aria_memory._keyword_relevance(
        "tell me about the pipeline", winner
    ))
    ranked_clean = memory_ranking.rank_items([winner], "pipeline", now_ts="2026-06-01T12:00:00+00:00")
    ranked_framed = memory_ranking.rank_items([diluted], "pipeline", now_ts="2026-06-01T12:00:00+00:00")
    assert ranked_clean[0]["combined_score"] > ranked_framed[0]["combined_score"]


def test_display_still_shows_the_raw_query(db, notes):
    raw = "tell me about the pipeline"
    assert memory_prompting.build_memory_context(raw)["query"] == raw


def test_the_system_prompt_shows_the_raw_query(db, notes):
    raw = "please explain the coffee machine"
    prompt = memory_prompting.build_system_prompt(
        "base", memory_prompting.build_memory_context(raw)
    )
    assert f"Query: {raw}" in prompt


def test_the_search_notes_tool_is_untouched(db, notes):
    # De-framing happens at the hybrid call site, not inside
    # search_notes_keyword, so the public tool still does exactly what a
    # caller asked for.
    from backend.core.tool_registry import execute_tool

    framed = execute_tool("search_notes", {"query": "tell me about the pipeline"})
    assert framed.ok
    assert framed.value["count"] == 0

    bare = execute_tool("search_notes", {"query": "pipeline"})
    assert bare.value["count"] == 1


# ======================================================
# 5. Nothing else moved
# ======================================================
def test_unframed_queries_behave_exactly_as_before(notes):
    items = by_id(aria_memory.search_hybrid("pipeline", min_score=0.0))
    assert items[notes["pipeline"]]["keyword_score"] == pytest.approx(1.0)


def test_a_query_that_is_only_framing_still_searches(notes):
    # deframe_query falls back to the original, so the keyword half gets a
    # real string rather than an empty one.
    assert aria_memory.search_hybrid("explain", min_score=0.0) is not None


def test_empty_store_returns_nothing(db):
    assert aria_memory.search_hybrid("tell me about anything", min_score=0.0) == []
