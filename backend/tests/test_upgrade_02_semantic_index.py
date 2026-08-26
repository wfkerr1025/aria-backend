# backend/tests/test_upgrade_02_semantic_index.py
#
# Upgrade 2: backend/aria_memory_semantic_index.py -- chunking, indexing
# and cosine search over note chunks.

from __future__ import annotations

import pytest

from phase3_upgrade_helpers import ConnectionRecorder, db, raw_query  # noqa: F401

from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.llm import semantic_embeddings

semantic_only = pytest.mark.skipif(
    not semantic_embeddings.is_semantic(),
    reason="active embedding backend is the lexical fallback, which cannot encode meaning",
)


def long_text(words=100):
    return " ".join(f"word{n}" for n in range(words))


# ---------------- chunking ----------------
def test_short_text_is_one_chunk(db):
    assert semantic.chunk_text("three little words") == ["three little words"]


def test_empty_text_yields_no_chunks(db):
    assert semantic.chunk_text("") == []
    assert semantic.chunk_text("   ") == []


def test_long_text_is_split(db):
    chunks = semantic.chunk_text(long_text(100))
    assert len(chunks) > 1
    assert all(len(chunk.split()) <= semantic.DEFAULT_CHUNK_WORDS for chunk in chunks)


def test_chunks_overlap(db):
    chunks = semantic.chunk_text(long_text(100))
    overlap = semantic.DEFAULT_CHUNK_OVERLAP
    assert chunks[0].split()[-overlap:] == chunks[1].split()[:overlap]


def test_every_word_survives_chunking(db):
    words = long_text(100).split()
    seen = {word for chunk in semantic.chunk_text(long_text(100)) for word in chunk.split()}
    assert seen == set(words)


def test_chunking_rejects_impossible_settings(db):
    with pytest.raises(ValueError):
        semantic.chunk_text("text", chunk_words=0)
    with pytest.raises(ValueError):
        semantic.chunk_text("text", chunk_words=5, overlap=5)


# ---------------- indexing ----------------
def test_index_note_stores_rows(db):
    note_id = notes_store.save_note(long_text(100))
    stored = semantic.index_note(note_id)
    assert stored == len(semantic.chunk_text(long_text(100)))
    assert raw_query(db, "SELECT COUNT(*) FROM semantic_index")[0][0] == stored


def test_index_note_stores_a_vector_per_chunk(db):
    note_id = notes_store.save_note(long_text(100))
    semantic.index_note(note_id)
    rows = raw_query(db, "SELECT vector FROM semantic_index WHERE note_id = ?", (note_id,))
    assert all(len(row[0]) > 0 for row in rows)


def test_reindexing_replaces_rather_than_duplicates(db):
    note_id = notes_store.save_note(long_text(100))
    first = semantic.index_note(note_id)
    second = semantic.index_note(note_id)
    assert first == second
    assert raw_query(db, "SELECT COUNT(*) FROM semantic_index")[0][0] == second


def test_index_note_ignores_a_missing_note(db):
    assert semantic.index_note(4242) == 0
    assert raw_query(db, "SELECT COUNT(*) FROM semantic_index")[0][0] == 0


def test_index_note_on_whitespace_text_stores_nothing(db):
    note_id = notes_store.save_note("   ")
    assert semantic.index_note(note_id) == 0


def test_clear_note_index_removes_only_that_note(db):
    kept = notes_store.save_note(long_text(60))
    dropped = notes_store.save_note(long_text(60))
    semantic.index_note(kept)
    semantic.index_note(dropped)
    removed = semantic.clear_note_index(dropped)
    assert removed > 0
    assert semantic.get_note_chunks(dropped) == []
    assert semantic.get_note_chunks(kept) != []


# ---------------- similarity ----------------
def test_cosine_of_identical_vectors_is_one(db):
    assert semantic.cosine_similarity([1.0, 2.0], [1.0, 2.0]) == pytest.approx(1.0)


def test_cosine_of_orthogonal_vectors_is_zero(db):
    assert semantic.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_of_a_zero_vector_is_zero(db):
    assert semantic.cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_rejects_mismatched_lengths(db):
    with pytest.raises(ValueError):
        semantic.cosine_similarity([1.0], [1.0, 2.0])


# ---------------- search ----------------
def test_search_finds_the_matching_note(db):
    wanted = notes_store.save_note("unity shader compilation is slow")
    other = notes_store.save_note("the kitchen coffee machine is broken")
    semantic.index_note(wanted)
    semantic.index_note(other)
    results = semantic.search_semantic("shader compilation")
    assert results
    assert results[0]["note_id"] == wanted


def test_search_results_are_ordered_by_score(db):
    strong = notes_store.save_note("shader shader shader")
    weak = notes_store.save_note("shader among many other unrelated words here")
    semantic.index_note(strong)
    semantic.index_note(weak)
    results = semantic.search_semantic("shader")
    scores = [row["score"] for row in results]
    assert scores == sorted(scores, reverse=True)


def test_search_returns_nothing_for_an_unrelated_query(db):
    # In a dense space an unrelated chunk scores low rather than zero, so
    # what keeps it out of the results is the default score floor.
    note_id = notes_store.save_note("unity shader compilation")
    semantic.index_note(note_id)
    assert semantic.search_semantic("zebra giraffe") == []


def test_an_unrelated_query_still_scores_below_the_floor(db):
    note_id = notes_store.save_note("unity shader compilation")
    semantic.index_note(note_id)
    ranked = semantic.search_semantic("zebra giraffe", min_score=0.0)
    assert ranked
    assert ranked[0]["score"] < semantic.DEFAULT_MIN_SCORE


def test_empty_query_matches_nothing(db):
    note_id = notes_store.save_note("unity shader compilation")
    semantic.index_note(note_id)
    assert semantic.search_semantic("") == []


def test_search_honours_the_limit(db):
    for n in range(4):
        note_id = notes_store.save_note(f"shared keyword note {n}")
        semantic.index_note(note_id)
    # min_score=0.0 so this measures the limit and nothing else.
    assert len(semantic.search_semantic("shared keyword", limit=2, min_score=0.0)) == 2


def test_search_honours_min_score(db):
    note_id = notes_store.save_note("unity shader compilation")
    semantic.index_note(note_id)
    assert semantic.search_semantic("shader", min_score=0.99) == []


def test_search_on_an_empty_index(db):
    assert semantic.search_semantic("anything") == []


@semantic_only
def test_search_matches_meaning_with_no_shared_words(db):
    # The headline capability: the query and the note share no content word.
    wanted = notes_store.save_note("How do I reset my password? I keep forgetting it.")
    other = notes_store.save_note("The volcano erupted late last century, covering the valley.")
    semantic.index_note(wanted)
    semantic.index_note(other)

    results = semantic.search_semantic("I lost my login credentials")
    assert [row["note_id"] for row in results] == [wanted]


@semantic_only
def test_search_matches_a_synonym(db):
    wanted = notes_store.save_note("I need to service the automobile before winter")
    semantic.index_note(wanted)
    assert [row["note_id"] for row in semantic.search_semantic("car maintenance")] == [wanted]


@semantic_only
def test_search_ranks_meaning_above_shared_words(db):
    paraphrase = notes_store.save_note("the notebook computer runs out of charge fast")
    distractor = notes_store.save_note("my laptop is blue")
    semantic.index_note(paraphrase)
    semantic.index_note(distractor)

    ranked = semantic.search_semantic("my laptop battery drains quickly", min_score=0.0)
    assert ranked[0]["note_id"] == paraphrase


@semantic_only
def test_a_related_query_outscores_an_unrelated_one(db):
    note_id = notes_store.save_note("How do I reset my password? I keep forgetting it.")
    semantic.index_note(note_id)
    related = semantic.search_semantic("forgotten login credentials", min_score=0.0)[0]["score"]
    unrelated = semantic.search_semantic("volcanic ash deposits", min_score=0.0)[0]["score"]
    assert related > unrelated


def test_chunks_are_tagged_with_their_encoder(db):
    note_id = notes_store.save_note(long_text(60))
    semantic.index_note(note_id)
    chunk = semantic.get_note_chunks(note_id)[0]
    assert chunk["model"] == semantic_embeddings.backend_id()
    assert chunk["dim"] == semantic_embeddings.dimension()


def test_chunks_from_another_backend_are_not_searched(db):
    # A note indexed under a previous encoder must not be ranked against
    # vectors from the current one -- different space, meaningless scores.
    note_id = notes_store.save_note("unity shader compilation")
    semantic.index_note(note_id)
    conn = semantic.get_connection()
    try:
        conn.execute("UPDATE semantic_index SET model = 'some-old-encoder'")
        conn.commit()
    finally:
        conn.close()
    assert semantic.search_semantic("unity shader", min_score=0.0) == []


def test_reindexing_brings_a_note_back_into_the_current_space(db):
    note_id = notes_store.save_note("unity shader compilation")
    semantic.index_note(note_id)
    conn = semantic.get_connection()
    try:
        conn.execute("UPDATE semantic_index SET model = 'some-old-encoder'")
        conn.commit()
    finally:
        conn.close()
    semantic.index_note(note_id)
    assert semantic.search_semantic("unity shader", min_score=0.0)


def test_connections_are_closed(db, monkeypatch):
    recorder = ConnectionRecorder()
    monkeypatch.setattr(semantic, "get_connection", recorder)
    monkeypatch.setattr(notes_store, "get_connection", recorder)
    note_id = notes_store.save_note(long_text(60))
    semantic.index_note(note_id)
    semantic.get_note_chunks(note_id)
    semantic.search_semantic("word1")
    semantic.clear_note_index(note_id)
    assert recorder.connections
    assert recorder.all_closed
