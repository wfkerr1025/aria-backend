# backend/tests/test_file_search.py
#
# backend/files/file_search.py -- ranked file search, and the file context
# helper that memory_prompting exposes for a later phase to consume.

from __future__ import annotations

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend.aria_memory import memory_ranking
from backend.files import file_ingestion as ingestion
from backend.files import file_search
from backend.llm import memory_prompting, semantic_embeddings, semantic_routing

HANDBOOK = """# Deployment handbook

The release pipeline runs every night at 2am and publishes to staging.
If a build fails, the on-call engineer is paged automatically.

## Passwords
To reset your account credentials, use the self-service portal.
Never share your login details with anyone.

## Catering
The office coffee machine is serviced on Fridays.
"""

semantic_only = pytest.mark.skipif(
    not semantic_embeddings.is_semantic(),
    reason="active embedding backend is the lexical fallback, which cannot encode meaning",
)


@pytest.fixture
def indexed(db, tmp_path):
    path = tmp_path / "handbook.md"
    path.write_text(HANDBOOK, encoding="utf-8")
    return ingestion.ingest_and_index(str(path), max_chars=120)


# ======================================================
# Ranking
# ======================================================
def test_ranked_search_returns_scored_items(db, indexed):
    ranked = file_search.search_files_ranked("credentials", min_score=0.0)
    assert ranked
    assert all("combined_score" in row for row in ranked)


def test_ranked_search_orders_by_combined_score(db, indexed):
    scores = [r["combined_score"] for r in file_search.search_files_ranked("the", min_score=0.0)]
    assert scores == sorted(scores, reverse=True)


def test_ranked_items_keep_the_file_chunk_type(db, indexed):
    ranked = file_search.search_files_ranked("credentials", min_score=0.0)
    assert all(row["type"] == "file_chunk" for row in ranked)


def test_ranking_applies_the_file_chunk_weight(db, indexed):
    ranked = file_search.search_files_ranked("credentials", min_score=0.0)
    assert ranked[0]["type_score"] == memory_ranking.TYPE_WEIGHTS["file_chunk"]


def test_the_file_chunk_weight_sits_with_note_chunks():
    weights = memory_ranking.TYPE_WEIGHTS
    assert weights["file_chunk"] == weights["chunk"]
    assert weights["file_chunk"] < weights["note"]


def test_ranked_items_keep_their_retrieval_fields(db, indexed):
    row = file_search.search_files_ranked("credentials", min_score=0.0)[0]
    for key in ["file_id", "chunk_id", "chunk_index", "text", "semantic_score", "created_at"]:
        assert key in row


def test_ranking_uses_the_retrieval_semantic_score(db, indexed):
    retrieved = ingestion.search_files_semantic("credentials", min_score=0.0)
    ranked = file_search.search_files_ranked("credentials", min_score=0.0)
    by_chunk = {row["chunk_id"]: row["semantic_score"] for row in retrieved}
    for row in ranked:
        assert row["semantic_score"] == by_chunk[row["chunk_id"]]


def test_chunks_get_a_real_recency_score(db, indexed):
    # created_at is carried through retrieval, so freshly ingested chunks
    # score at the top of the decay curve rather than the undated default.
    ranked = file_search.search_files_ranked("credentials", min_score=0.0)
    assert ranked[0]["recency_score"] > memory_ranking.DEFAULT_RECENCY_SCORE


def test_ranked_search_respects_the_limit(db, indexed):
    assert len(file_search.search_files_ranked("the", limit=1, min_score=0.0)) == 1


def test_ranked_search_respects_min_score(db, indexed):
    assert file_search.search_files_ranked("credentials", min_score=0.99) == []


def test_ranked_search_on_an_empty_store(db):
    assert file_search.search_files_ranked("anything") == []


def test_ranked_search_is_deterministic(db, indexed):
    first = file_search.search_files_ranked("credentials", min_score=0.0)
    second = file_search.search_files_ranked("credentials", min_score=0.0)
    assert [r["chunk_id"] for r in first] == [r["chunk_id"] for r in second]
    # Scores agree to well within any margin that could reorder them, but not
    # bit-for-bit: recency decays against the wall clock, so two calls a
    # microsecond apart differ in the far decimals. Ranking with a fixed
    # now_ts is exactly reproducible -- see test_memory_ranking.
    assert [r["combined_score"] for r in first] == pytest.approx(
        [r["combined_score"] for r in second], abs=1e-6
    )


def test_ranked_scores_are_exact_against_a_fixed_clock(db, indexed):
    items = ingestion.search_files_semantic("credentials", min_score=0.0)
    now = "2026-06-01T12:00:00+00:00"
    first = memory_ranking.rank_items(items, "credentials", now_ts=now)
    second = memory_ranking.rank_items(items, "credentials", now_ts=now)
    assert [r["combined_score"] for r in first] == [r["combined_score"] for r in second]


def test_ranked_ordering_is_stable_across_runs(db, indexed):
    runs = [
        [r["chunk_id"] for r in file_search.search_files_ranked("pipeline", min_score=0.0)]
        for _ in range(5)
    ]
    assert all(run == runs[0] for run in runs)


@semantic_only
def test_the_most_relevant_chunk_ranks_first(db, indexed):
    ranked = file_search.search_files_ranked("I forgot my sign-in details")
    assert ranked
    assert "credentials" in ranked[0]["text"] or "login" in ranked[0]["text"]


# ======================================================
# Isolation from the note store
# ======================================================
def test_file_search_does_not_return_notes(db, indexed):
    from backend import aria_memory_notes as notes_store
    from backend import aria_memory_semantic_index as semantic

    note_id = notes_store.save_note("a note about account credentials")
    semantic.index_note(note_id)

    ranked = file_search.search_files_ranked("credentials", min_score=0.0)
    assert all(row["type"] == "file_chunk" for row in ranked)
    assert all(row.get("note_id") is None for row in ranked)


def test_note_search_does_not_return_file_chunks(db, indexed):
    from backend import aria_memory_semantic_index as semantic

    # Existing note behaviour is unchanged: the note index never saw the
    # file, so a note query returns nothing from it.
    assert semantic.search_semantic("credentials", min_score=0.0) == []


# ======================================================
# memory_prompting.load_file_context
# ======================================================
def test_file_context_has_the_documented_shape(db, indexed):
    context = memory_prompting.load_file_context("credentials")
    assert sorted(context) == ["items", "query", "routing", "summary"]


def test_file_context_echoes_the_query(db, indexed):
    assert memory_prompting.load_file_context("credentials")["query"] == "credentials"


def test_file_context_reports_the_backend(db, indexed):
    routing = memory_prompting.load_file_context("credentials")["routing"]
    assert routing["semantic_used"] is True
    assert routing["backend"] == semantic_embeddings.backend_id()
    assert routing["embedding_dim"] == semantic_embeddings.dimension()


def test_file_context_summarizes_its_items(db, indexed):
    context = memory_prompting.load_file_context("account credentials")
    if context["items"]:
        assert "file chunk" in context["summary"]


def test_file_context_on_an_empty_store(db):
    context = memory_prompting.load_file_context("anything")
    assert context["items"] == []
    assert context["summary"] == ""


def test_file_context_respects_the_limit(db, indexed):
    assert len(memory_prompting.load_file_context("the", limit=1)["items"]) <= 1


def test_file_context_is_not_injected_into_ordinary_turns(db, indexed):
    # Deliberate: folding file chunks into every turn would double retrieval
    # cost and let a document outrank something the user wrote.
    context = memory_prompting.build_memory_context("credentials")
    assert all(row.get("type") != "file_chunk" for row in context["items"])
    assert "file_context" not in context


# ======================================================
# Routing
# ======================================================
def test_files_query_routes_to_files_only():
    flags = semantic_routing.INTENT_FLAGS[semantic_routing.FILES_QUERY]
    assert flags["use_files"] is True
    assert flags["use_memory"] is False
    assert flags["use_tools"] is False


def test_a_file_question_is_routed_to_files():
    result = semantic_routing.route("search my documents for the deployment handbook")
    assert result["intent"] == semantic_routing.FILES_QUERY
    assert result["use_files"] is True
