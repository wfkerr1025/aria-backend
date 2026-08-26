# backend/tests/test_upgrade_01_note_embeddings.py
#
# Upgrade 1 (as revised by the semantic upgrade): backend/aria_memory_note_
# embeddings.py -- embedding note text and storing the vector through
# aria_memory_embeddings.
#
# The encoder itself now lives in backend/llm/semantic_embeddings.py and has
# its own suite (test_semantic_embeddings.py). What is pinned here is the
# note-shaped behaviour: a note goes in, a tagged vector of the active
# backend's width comes out, and vectors from a foreign backend stay out.
#
# Structural properties (width, unit norm, determinism, cross-process
# stability, malformed blob rejection) are kept from the lexical era because
# they still hold. The assertions that were specifically lexical -- word
# order not mattering, punctuation collapsing, empty text giving a zero
# vector -- are gone: they described the hashing encoder, and asserting them
# now would be asserting that the embeddings are NOT semantic.

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from phase3_upgrade_helpers import ConnectionRecorder, db, raw_query  # noqa: F401

from backend import aria_memory_embeddings as embeddings_store
from backend import aria_memory_note_embeddings as note_embeddings
from backend import aria_memory_notes as notes_store
from backend.llm import semantic_embeddings

ROOT = Path(__file__).resolve().parents[2]

semantic_only = pytest.mark.skipif(
    not semantic_embeddings.is_semantic(),
    reason="active embedding backend is the lexical fallback, which cannot encode meaning",
)


# ---------------- structure ----------------
def test_vector_has_the_backend_width(db):
    blob = note_embeddings.embed_text("hello world")
    assert len(blob) == note_embeddings.vector_bytes()
    assert len(note_embeddings.decode_vector(blob)) == note_embeddings.embedding_dim()


def test_vector_is_l2_normalized(db):
    vector = note_embeddings.embed_vector("unity shader build")
    assert sum(value * value for value in vector) == pytest.approx(1.0, abs=1e-5)


def test_encoding_is_deterministic(db):
    assert note_embeddings.embed_text("same input") == note_embeddings.embed_text("same input")


def test_encoding_is_stable_across_processes(db):
    # A vector written by one process has to stay comparable in the next.
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "from backend.aria_memory_note_embeddings import embed_text;"
        "print(embed_text('stability check').hex())" % ROOT
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == note_embeddings.embed_text("stability check").hex()


def test_different_text_gives_a_different_vector(db):
    assert note_embeddings.embed_text("alpha") != note_embeddings.embed_text("beta")


def test_decode_rejects_a_wrong_sized_blob(db):
    with pytest.raises(ValueError):
        note_embeddings.decode_vector(b"too short")


def test_decode_rejects_a_blob_of_the_wrong_dimension(db):
    # Right shape, wrong width: a vector from another backend must not be
    # silently reinterpreted in this one's space.
    foreign = semantic_embeddings.encode_vector([0.1] * (note_embeddings.embedding_dim() + 8))
    with pytest.raises(ValueError):
        note_embeddings.decode_vector(foreign)


# ---------------- meaning ----------------
@semantic_only
def test_word_order_changes_the_vector(db):
    # The lexical encoder bucketed tokens, so these were byte-identical.
    # A semantic encoder must tell them apart, because they mean different
    # things.
    assert note_embeddings.embed_text("the dog bit the man") != note_embeddings.embed_text(
        "the man bit the dog"
    )


@semantic_only
def test_synonyms_are_closer_than_unrelated_words(db):
    car = note_embeddings.embed_vector("car")
    similar = semantic_embeddings.cosine_similarity(car, note_embeddings.embed_vector("automobile"))
    unrelated = semantic_embeddings.cosine_similarity(car, note_embeddings.embed_vector("banana"))
    assert similar > unrelated


@semantic_only
def test_text_with_no_shared_words_can_still_match(db):
    # The property the lexical encoder could not have: zero token overlap,
    # high similarity.
    left = note_embeddings.embed_vector("How do I reset my password?")
    right = note_embeddings.embed_vector("I forgot my login credentials")
    assert semantic_embeddings.cosine_similarity(left, right) > 0.6


# ---------------- storage ----------------
def test_embed_note_stores_a_row(db):
    note_id = notes_store.save_note("unity shader compilation")
    embedding_id = note_embeddings.embed_note(note_id)
    assert isinstance(embedding_id, int)
    assert raw_query(db, "SELECT note_id FROM embeddings WHERE id = ?", (embedding_id,))[0][0] == note_id


def test_embed_note_tags_the_vector_with_its_encoder(db):
    note_id = notes_store.save_note("tagged vector")
    note_embeddings.embed_note(note_id)
    model, dim = raw_query(db, "SELECT model, dim FROM embeddings")[0]
    assert model == semantic_embeddings.backend_id()
    assert dim == semantic_embeddings.dimension()


def test_embed_note_round_trips_the_text(db):
    note_id = notes_store.save_note("unity shader compilation")
    note_embeddings.embed_note(note_id)
    assert note_embeddings.get_note_vectors(note_id) == [
        note_embeddings.embed_vector("unity shader compilation")
    ]


def test_embed_note_returns_none_for_a_missing_note(db):
    assert note_embeddings.embed_note(4242) is None
    assert raw_query(db, "SELECT COUNT(*) FROM embeddings")[0][0] == 0


def test_get_note_vectors_is_empty_without_embeddings(db):
    note_id = notes_store.save_note("not embedded yet")
    assert note_embeddings.get_note_vectors(note_id) == []


def test_multiple_embeddings_decode_in_insertion_order(db):
    note_id = notes_store.save_note("first text")
    note_embeddings.embed_note(note_id)
    embeddings_store.store_embedding(
        note_id,
        semantic_embeddings.embed_to_blob("second text"),
        model=semantic_embeddings.backend_id(),
        dim=semantic_embeddings.dimension(),
    )
    vectors = note_embeddings.get_note_vectors(note_id)
    assert len(vectors) == 2
    assert vectors[1] == note_embeddings.embed_vector("second text")


def test_vectors_from_another_backend_are_ignored(db):
    # A note embedded before a backend change keeps its old vector, but it
    # is never handed back as if it belonged to the current space.
    note_id = notes_store.save_note("mixed vectors")
    note_embeddings.embed_note(note_id)
    embeddings_store.store_embedding(note_id, b"\x00\x00\x80?", model="some-old-encoder", dim=1)
    assert len(note_embeddings.get_note_vectors(note_id)) == 1
    assert raw_query(db, "SELECT COUNT(*) FROM embeddings")[0][0] == 2


def test_untagged_vectors_are_ignored(db):
    # Rows written straight through the storage layer carry no model, so
    # they are outside the active space too.
    note_id = notes_store.save_note("untagged")
    embeddings_store.store_embedding(note_id, semantic_embeddings.embed_to_blob("untagged"))
    assert note_embeddings.get_note_vectors(note_id) == []


def test_connections_are_closed(db, monkeypatch):
    recorder = ConnectionRecorder()
    monkeypatch.setattr(embeddings_store, "get_connection", recorder)
    monkeypatch.setattr(notes_store, "get_connection", recorder)
    note_id = notes_store.save_note("cleanup check")
    note_embeddings.embed_note(note_id)
    note_embeddings.get_note_vectors(note_id)
    assert recorder.connections
    assert recorder.all_closed
