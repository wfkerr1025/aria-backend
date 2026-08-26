# backend/tests/test_semantic_embeddings.py
#
# backend/llm/semantic_embeddings.py -- the encoder itself.
#
# Two kinds of test live here. The structural ones (width, unit norm,
# determinism, blob round-trip, cosine edge cases, backend selection) hold
# for every backend including the lexical fallback, so they always run. The
# meaning ones -- synonyms, paraphrase, unrelated topics -- are the whole
# point of the upgrade and are marked semantic_only: the lexical fallback
# genuinely cannot satisfy them, so on a machine with no embedding model
# they skip rather than fail or, worse, pass vacuously.

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.llm import semantic_embeddings as se

ROOT = Path(__file__).resolve().parents[2]

semantic_only = pytest.mark.skipif(
    not se.is_semantic(),
    reason="active embedding backend is the lexical fallback, which cannot encode meaning",
)


def sim(left: str, right: str) -> float:
    return se.cosine_similarity(se.embed_text_semantic(left), se.embed_text_semantic(right))


# ======================================================
# Structure -- true of any backend
# ======================================================
def test_backend_reports_an_identity():
    assert isinstance(se.backend_id(), str)
    assert se.backend_id()


def test_dimension_is_positive():
    assert se.dimension() > 0


def test_vector_has_the_backend_dimension():
    assert len(se.embed_text_semantic("hello")) == se.dimension()


def test_vector_is_unit_length():
    vector = se.embed_text_semantic("a sentence of several words")
    assert sum(value * value for value in vector) == pytest.approx(1.0, abs=1e-5)


def test_encoding_is_deterministic():
    assert se.embed_text_semantic("same text") == se.embed_text_semantic("same text")


def test_encoding_is_stable_across_processes():
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "from backend.llm.semantic_embeddings import embed_to_blob;"
        "print(embed_to_blob('cross process').hex())" % ROOT
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == se.embed_to_blob("cross process").hex()


def test_different_text_gives_different_vectors():
    assert se.embed_text_semantic("alpha") != se.embed_text_semantic("beta")


def test_batch_matches_single():
    texts = ["first text", "second text", "third text"]
    assert se.embed_texts_semantic(texts) == [se.embed_text_semantic(t) for t in texts]


def test_batch_of_nothing_is_empty():
    assert se.embed_texts_semantic([]) == []


def test_batch_handles_repeats():
    assert se.embed_texts_semantic(["dup", "dup"]) == [se.embed_text_semantic("dup")] * 2


def test_empty_text_still_produces_a_valid_vector():
    # The lexical encoder returned all zeros here. A semantic backend has no
    # such special case, so the contract is only "a well-formed vector".
    vector = se.embed_text_semantic("")
    assert len(vector) == se.dimension()


# ======================================================
# Storage format
# ======================================================
def test_blob_round_trips_exactly():
    vector = se.embed_text_semantic("round trip")
    assert se.decode_vector(se.encode_vector(vector)) == vector


def test_embed_to_blob_matches_encode_of_embed():
    assert se.embed_to_blob("same") == se.encode_vector(se.embed_text_semantic("same"))


def test_blob_is_four_bytes_per_value():
    assert len(se.embed_to_blob("width")) == se.dimension() * 4


def test_decode_rejects_a_ragged_blob():
    with pytest.raises(ValueError):
        se.decode_vector(b"12345")


def test_decode_rejects_the_wrong_dimension():
    with pytest.raises(ValueError):
        se.decode_vector(se.encode_vector([0.0] * (se.dimension() + 1)))


def test_decode_accepts_an_explicit_dimension():
    foreign = se.encode_vector([0.5, 0.5, 0.5, 0.5])
    assert se.decode_vector(foreign, 4) == [0.5, 0.5, 0.5, 0.5]


# ======================================================
# Cosine edge cases
# ======================================================
def test_cosine_of_identical_vectors_is_one():
    assert se.cosine_similarity([1.0, 2.0], [1.0, 2.0]) == pytest.approx(1.0)


def test_cosine_of_orthogonal_vectors_is_zero():
    assert se.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_of_a_zero_vector_is_zero():
    assert se.cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        se.cosine_similarity([1.0], [1.0, 2.0])


def test_a_text_is_maximally_similar_to_itself():
    assert sim("identical text", "identical text") == pytest.approx(1.0, abs=1e-5)


# ======================================================
# Meaning -- the point of the upgrade
# ======================================================
@semantic_only
@pytest.mark.parametrize(
    "left, right",
    [
        ("car", "automobile"),
        ("doctor", "physician"),
        ("happy", "joyful"),
        ("begin", "commence"),
    ],
)
def test_synonyms_are_similar(left, right):
    assert sim(left, right) > 0.7


@semantic_only
@pytest.mark.parametrize(
    "left, right",
    [
        ("How do I reset my password?", "I forgot my login credentials"),
        ("The cat sat on the mat", "A feline rested upon the rug"),
        ("unity shader compilation is slow", "the graphics pipeline takes ages to build"),
    ],
)
def test_paraphrases_are_similar(left, right):
    assert sim(left, right) > 0.6


@semantic_only
@pytest.mark.parametrize(
    "left, right",
    [
        ("car", "banana"),
        ("quantum physics lecture", "my dog needs a walk"),
        ("The cat sat on the mat", "Quarterly revenue exceeded forecasts"),
        ("How do I reset my password?", "The volcano erupted last century"),
    ],
)
def test_unrelated_concepts_are_dissimilar(left, right):
    assert sim(left, right) < 0.6


@semantic_only
def test_synonyms_beat_unrelated_words():
    assert sim("car", "automobile") > sim("car", "banana")


@semantic_only
def test_paraphrase_beats_unrelated_sentence():
    question = "How do I reset my password?"
    assert sim(question, "I forgot my login credentials") > sim(
        question, "The volcano erupted last century"
    )


@semantic_only
def test_meaning_beats_shared_words():
    # The case the lexical encoder got backwards: the paraphrase shares no
    # content words with the query, while the distractor repeats one.
    query = "my laptop battery drains quickly"
    paraphrase = "the notebook computer runs out of charge fast"
    distractor = "my laptop is blue"
    assert sim(query, paraphrase) > sim(query, distractor)


@semantic_only
def test_word_order_changes_meaning():
    assert se.embed_text_semantic("the dog bit the man") != se.embed_text_semantic(
        "the man bit the dog"
    )


@semantic_only
def test_related_and_unrelated_bands_do_not_overlap():
    related = [
        sim("car", "automobile"),
        sim("doctor", "physician"),
        sim("How do I reset my password?", "I forgot my login credentials"),
    ]
    unrelated = [
        sim("car", "banana"),
        sim("quantum physics lecture", "my dog needs a walk"),
        sim("The cat sat on the mat", "Quarterly revenue exceeded forecasts"),
    ]
    assert min(related) > max(unrelated)


# ======================================================
# Backend selection
# ======================================================
def test_lexical_fallback_can_be_forced(monkeypatch):
    monkeypatch.setenv(se.ENV_BACKEND, "lexical-hash")
    se.reset_backend()
    try:
        assert se.backend_id() == "lexical-hash-v1"
        assert se.dimension() == se.LEXICAL_DIM
        assert se.is_semantic() is False
    finally:
        monkeypatch.delenv(se.ENV_BACKEND, raising=False)
        se.reset_backend()


def test_fallback_vectors_are_still_well_formed(monkeypatch):
    monkeypatch.setenv(se.ENV_BACKEND, "lexical-hash")
    se.reset_backend()
    try:
        vector = se.embed_text_semantic("shared words only")
        assert len(vector) == se.LEXICAL_DIM
        assert sum(v * v for v in vector) == pytest.approx(1.0, abs=1e-5)
    finally:
        monkeypatch.delenv(se.ENV_BACKEND, raising=False)
        se.reset_backend()


def test_fallback_is_lexical_not_semantic(monkeypatch):
    # Pinning the limitation that motivated this upgrade: with the fallback
    # active, synonyms with no shared tokens score zero.
    monkeypatch.setenv(se.ENV_BACKEND, "lexical-hash")
    se.reset_backend()
    try:
        assert sim("car", "automobile") == pytest.approx(0.0)
    finally:
        monkeypatch.delenv(se.ENV_BACKEND, raising=False)
        se.reset_backend()


def test_an_unknown_forced_backend_is_an_error(monkeypatch):
    monkeypatch.setenv(se.ENV_BACKEND, "not-a-backend")
    se.reset_backend()
    try:
        with pytest.raises(se.EmbeddingError):
            se.active_backend()
    finally:
        monkeypatch.delenv(se.ENV_BACKEND, raising=False)
        se.reset_backend()


def test_backend_id_changes_with_the_backend(monkeypatch):
    original = se.backend_id()
    monkeypatch.setenv(se.ENV_BACKEND, "lexical-hash")
    se.reset_backend()
    try:
        forced = se.backend_id()
    finally:
        monkeypatch.delenv(se.ENV_BACKEND, raising=False)
        se.reset_backend()
    assert se.backend_id() == original
    if original != "lexical-hash-v1":
        assert forced != original


def test_model_discovery_finds_a_model_when_one_is_installed():
    path = se.find_model_path()
    if path is not None:
        assert path.is_file()
        assert path.suffix == ".gguf"


def test_an_explicit_model_path_that_does_not_exist_is_ignored(monkeypatch):
    monkeypatch.setenv(se.ENV_MODEL_PATH, str(ROOT / "no-such-model.gguf"))
    assert se.find_model_path() is None
