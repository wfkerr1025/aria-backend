# backend/tests/test_memory_ranking.py
#
# backend/aria_memory/memory_ranking.py -- scoring and ordering retrieved
# memory items.
#
# Most of these tests build item dicts by hand rather than going through
# retrieval. That is deliberate: ranking's contract is "given these signals,
# produce this order", and constructing the signals directly is the only way
# to isolate one of them while holding the others equal. The end-to-end
# tests at the bottom cover the real pipeline.
#
# Every test that involves recency passes an explicit now_ts, so the results
# do not depend on how long the suite took to run.

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory
from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.aria_memory import memory_ranking as ranking
from backend.llm import semantic_embeddings

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

semantic_only = pytest.mark.skipif(
    not semantic_embeddings.is_semantic(),
    reason="active embedding backend is the lexical fallback, which cannot encode meaning",
)


def at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def item(**overrides) -> dict:
    """An item with every signal neutral, so a test can vary exactly one."""
    base = {
        "id": overrides.pop("id", 1),
        "type": "note",
        "text": "text",
        "semantic_score": 0.5,
        "keyword_score": 0.5,
        "created_at": at(0),
    }
    base.update(overrides)
    return base


def order(items, query="query", **kwargs):
    return [row["id"] for row in ranking.rank_items(items, query, now_ts=NOW, **kwargs)]


# ======================================================
# normalize_scores
# ======================================================
def test_combined_score_is_within_zero_and_one():
    assert ranking.normalize_scores(1.0, 1.0, 1.0, 1.1) == pytest.approx(1.0)
    assert ranking.normalize_scores(0.0, 0.0, 0.0, 0.0) == 0.0


def test_every_signal_raises_the_combined_score():
    baseline = ranking.normalize_scores(0.0, 0.0, 0.0, 1.0)
    assert ranking.normalize_scores(1.0, 0.0, 0.0, 1.0) > baseline
    assert ranking.normalize_scores(0.0, 1.0, 0.0, 1.0) > baseline
    assert ranking.normalize_scores(0.0, 0.0, 1.0, 1.0) > baseline
    assert ranking.normalize_scores(0.0, 0.0, 0.0, 1.1) > baseline


def test_signals_are_weighted_in_the_documented_order():
    # semantic > keyword > recency, per the 0.45 / 0.25 / 0.20 split.
    semantic_only_score = ranking.normalize_scores(0.0, 1.0, 0.0, 1.0)
    keyword_only_score = ranking.normalize_scores(1.0, 0.0, 0.0, 1.0)
    recency_only_score = ranking.normalize_scores(0.0, 0.0, 1.0, 1.0)
    assert semantic_only_score > keyword_only_score > recency_only_score


def test_out_of_range_signals_are_clamped():
    # A negative cosine should not drag a combined score below what an
    # unrelated item gets.
    assert ranking.normalize_scores(0.0, -0.9, 0.0, 1.0) == ranking.normalize_scores(
        0.0, 0.0, 0.0, 1.0
    )
    assert ranking.normalize_scores(5.0, 5.0, 5.0, 1.1) == pytest.approx(1.0)


# ======================================================
# recency_score
# ======================================================
def test_recency_is_one_for_something_just_written():
    assert ranking.recency_score({"created_at": at(0)}, NOW) == pytest.approx(1.0)


def test_recency_decays_with_age():
    fresh = ranking.recency_score({"created_at": at(1)}, NOW)
    middling = ranking.recency_score({"created_at": at(30)}, NOW)
    ancient = ranking.recency_score({"created_at": at(365)}, NOW)
    assert fresh > middling > ancient


def test_recency_matches_the_documented_decay():
    # exp(-720/720) at thirty days.
    assert ranking.recency_score({"created_at": at(30)}, NOW) == pytest.approx(0.3679, abs=1e-3)


def test_missing_timestamp_scores_mid_scale():
    assert ranking.recency_score({}, NOW) == ranking.DEFAULT_RECENCY_SCORE


def test_unparseable_timestamp_scores_mid_scale():
    assert ranking.recency_score({"created_at": "not a date"}, NOW) == ranking.DEFAULT_RECENCY_SCORE


def test_updated_at_wins_over_created_at():
    edited = {"created_at": at(365), "updated_at": at(0)}
    assert ranking.recency_score(edited, NOW) == pytest.approx(1.0)


def test_a_future_timestamp_is_treated_as_now():
    assert ranking.recency_score({"created_at": at(-5)}, NOW) == pytest.approx(1.0)


# ======================================================
# type_score
# ======================================================
def test_type_weights_follow_the_documented_priority():
    assert ranking.type_score({"type": "self"}) > ranking.type_score({"type": "note"})
    assert ranking.type_score({"type": "note"}) > ranking.type_score({"type": "chunk"})
    assert ranking.type_score({"type": "chunk"}) > ranking.type_score({"type": "context"})


def test_an_unknown_type_gets_the_default_weight():
    assert ranking.type_score({"type": "something new"}) == ranking.DEFAULT_TYPE_WEIGHT
    assert ranking.type_score({}) == ranking.DEFAULT_TYPE_WEIGHT


def test_type_weights_are_extendable(monkeypatch):
    monkeypatch.setitem(ranking.TYPE_WEIGHTS, "reminder", 1.5)
    assert ranking.type_score({"type": "reminder"}) == 1.5


# ======================================================
# compute_scores
# ======================================================
def test_compute_scores_returns_every_signal():
    scores = ranking.compute_scores(item(), None, NOW)
    assert sorted(scores) == [
        "combined_score", "keyword_score", "recency_score", "semantic_score", "type_score",
    ]


def test_compute_scores_uses_the_supplied_scores_as_is():
    scores = ranking.compute_scores(item(semantic_score=0.77, keyword_score=0.33), None, NOW)
    assert scores["semantic_score"] == 0.77
    assert scores["keyword_score"] == 0.33


def test_a_missing_keyword_score_counts_as_zero():
    candidate = item()
    candidate.pop("keyword_score")
    assert ranking.compute_scores(candidate, None, NOW)["keyword_score"] == 0.0


def test_a_semantic_search_score_key_is_accepted():
    # search_semantic calls it "score"; ranking should not need a rename.
    candidate = item()
    candidate.pop("semantic_score")
    candidate["score"] = 0.9
    assert ranking.compute_scores(candidate, None, NOW)["semantic_score"] == 0.9


@semantic_only
def test_a_missing_semantic_score_is_computed_from_the_query():
    # A keyword-only hit still gets ranked on meaning.
    candidate = {"id": 1, "type": "note", "text": "the automobile needs a service"}
    query_embedding = semantic_embeddings.embed_text_semantic("car maintenance")
    scores = ranking.compute_scores(candidate, query_embedding, NOW)
    assert scores["semantic_score"] > 0.5


def test_without_a_query_embedding_an_unscored_item_is_semantically_neutral():
    candidate = {"id": 1, "type": "note", "text": "anything"}
    assert ranking.compute_scores(candidate, None, NOW)["semantic_score"] == 0.0


# ======================================================
# Signal interaction -- which signal wins when
# ======================================================
def test_recency_breaks_a_tie_on_every_other_signal():
    items = [item(id=1, created_at=at(365)), item(id=2, created_at=at(0))]
    assert order(items) == [2, 1]


def test_semantic_beats_keyword_when_meaning_is_stronger():
    meaningful = item(id=1, semantic_score=0.9, keyword_score=0.1)
    wordy = item(id=2, semantic_score=0.1, keyword_score=0.9)
    assert order([meaningful, wordy]) == [1, 2]
    assert order([wordy, meaningful]) == [1, 2]


def test_keyword_beats_semantic_when_the_exact_phrase_matches():
    # A perfect keyword hit against a merely-similar one: the keyword
    # advantage has to be large enough to overcome the semantic weight,
    # which is what an exact phrase match looks like.
    exact = item(id=1, semantic_score=0.55, keyword_score=1.0)
    similar = item(id=2, semantic_score=0.75, keyword_score=0.0)
    assert order([similar, exact]) == [1, 2]


def test_type_weighting_orders_self_above_note_above_chunk():
    items = [
        item(id=1, type="chunk"),
        item(id=2, type="note"),
        item(id=3, type="self"),
    ]
    assert order(items) == [3, 2, 1]


def test_type_alone_does_not_outweigh_a_real_relevance_gap():
    # The type weight is a nudge: a far more relevant chunk still beats a
    # barely relevant self-knowledge entry.
    relevant_chunk = item(id=1, type="chunk", semantic_score=0.95, keyword_score=0.95)
    weak_self = item(id=2, type="self", semantic_score=0.05, keyword_score=0.05)
    assert order([weak_self, relevant_chunk]) == [1, 2]


def test_a_strong_recent_item_beats_a_stale_stronger_one():
    stale = item(id=1, semantic_score=0.75, created_at=at(3650))
    recent = item(id=2, semantic_score=0.62, created_at=at(0))
    assert order([stale, recent]) == [2, 1]


# ======================================================
# Determinism and purity
# ======================================================
def test_ties_keep_their_input_order():
    items = [item(id=1), item(id=2), item(id=3)]
    assert order(items) == [1, 2, 3]
    assert order(list(reversed(items))) == [3, 2, 1]


def test_ranking_is_stable_across_repeated_runs():
    items = [item(id=n, semantic_score=0.5 + n / 100, created_at=at(n)) for n in range(6)]
    runs = [order(items) for _ in range(5)]
    assert all(run == runs[0] for run in runs)


def test_scores_are_stable_across_repeated_runs():
    items = [item(id=1, semantic_score=0.42)]
    first = ranking.rank_items(items, "query", now_ts=NOW)[0]["combined_score"]
    second = ranking.rank_items(items, "query", now_ts=NOW)[0]["combined_score"]
    assert first == second


def test_ranking_does_not_mutate_the_input():
    items = [item(id=1)]
    ranking.rank_items(items, "query", now_ts=NOW)
    assert "combined_score" not in items[0]
    assert items[0]["semantic_score"] == 0.5


def test_ranked_items_keep_their_original_fields():
    ranked = ranking.rank_items([item(id=1, text="original text")], "query", now_ts=NOW)[0]
    assert ranked["id"] == 1
    assert ranked["text"] == "original text"
    assert "combined_score" in ranked


def test_scores_descend():
    items = [item(id=n, semantic_score=n / 10) for n in range(1, 6)]
    scores = [row["combined_score"] for row in ranking.rank_items(items, "q", now_ts=NOW)]
    assert scores == sorted(scores, reverse=True)


# ======================================================
# Shape of the call
# ======================================================
def test_empty_input_gives_empty_output():
    assert ranking.rank_items([], "query") == []


def test_limit_truncates_the_result():
    items = [item(id=n, semantic_score=n / 10) for n in range(1, 6)]
    assert len(ranking.rank_items(items, "q", limit=2, now_ts=NOW)) == 2


def test_limit_keeps_the_best_items():
    items = [item(id=1, semantic_score=0.1), item(id=2, semantic_score=0.9)]
    assert order(items, limit=1) == [2]


def test_limit_larger_than_the_list_is_harmless():
    assert len(ranking.rank_items([item(id=1)], "q", limit=99, now_ts=NOW)) == 1


def test_an_empty_query_still_ranks_on_the_other_signals():
    items = [item(id=1, created_at=at(365)), item(id=2, created_at=at(0))]
    assert [row["id"] for row in ranking.rank_items(items, "", now_ts=NOW)] == [2, 1]


def test_mixed_item_kinds_rank_together():
    items = [
        item(id=1, type="note"),
        item(id=2, type="chunk"),
        item(id=3, type="self"),
        item(id=4, type="context"),
    ]
    ranked = ranking.rank_items(items, "query", now_ts=NOW)
    assert len(ranked) == 4
    assert [row["type"] for row in ranked] == ["self", "note", "chunk", "context"]


def test_items_missing_optional_fields_do_not_break_ranking():
    sparse = [{"id": 1}, {"id": 2, "type": "note"}, {"id": 3, "text": "words"}]
    assert len(ranking.rank_items(sparse, "query", now_ts=NOW)) == 3


# ======================================================
# End to end through the real pipeline
# ======================================================
def test_search_hybrid_merges_both_searches(db):
    note_id = notes_store.save_note("unity shader compilation is slow")
    semantic.index_note(note_id)
    items = aria_memory.search_hybrid("shader compilation")
    assert items
    assert all("type" in row and "keyword_score" in row for row in items)


def test_search_hybrid_does_not_duplicate_a_note_found_both_ways(db):
    note_id = notes_store.save_note("unity shader compilation is slow")
    semantic.index_note(note_id)
    items = aria_memory.search_hybrid("unity shader compilation")
    assert [row["id"] for row in items].count(note_id) == 1


def test_search_ranked_returns_scored_items(db):
    note_id = notes_store.save_note("unity shader compilation is slow")
    semantic.index_note(note_id)
    ranked = aria_memory.search_ranked("shader compilation")
    assert ranked
    assert all("combined_score" in row for row in ranked)
    scores = [row["combined_score"] for row in ranked]
    assert scores == sorted(scores, reverse=True)


def test_search_ranked_respects_the_limit(db):
    for n in range(5):
        note_id = notes_store.save_note(f"shader note number {n}")
        semantic.index_note(note_id)
    assert len(aria_memory.search_ranked("shader note", limit=2)) <= 2


def test_search_ranked_on_an_empty_store(db):
    assert aria_memory.search_ranked("anything") == []


def test_chunks_carry_a_real_timestamp_through_the_pipeline(db):
    # Without created_at on the semantic hit every chunk would score the
    # default recency, making the signal inert for exactly the item type
    # semantic search produces.
    note_id = notes_store.save_note("unity shader compilation is slow")
    semantic.index_note(note_id)
    hits = semantic.search_semantic("shader", min_score=0.0)
    assert hits
    assert hits[0]["created_at"] is not None


@semantic_only
def test_search_ranked_puts_the_meaningful_note_first(db):
    wanted = notes_store.save_note("How do I reset my password? I forget it often.")
    other = notes_store.save_note("Volcanic ash covered the valley last century.")
    semantic.index_note(wanted)
    semantic.index_note(other)
    ranked = aria_memory.search_ranked("I lost my login credentials")
    assert ranked[0]["id"] == wanted
