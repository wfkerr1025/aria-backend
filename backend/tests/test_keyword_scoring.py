# backend/tests/test_keyword_scoring.py
#
# Per-term keyword scoring inside hybrid retrieval.
#
# Coverage used to be substring containment over the whole query; it is now
# the share of distinct, de-framed, non-stopword terms that appear as whole
# words in an item's text or tags. These tests pin the term model, the
# whole-word rule, and that only the keyword signal moved.

from __future__ import annotations

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory
from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.aria_memory import memory_ranking
from backend.aria_memory import tokenize
from backend.llm import semantic_embeddings

relevance = aria_memory._keyword_relevance

semantic_only = pytest.mark.skipif(
    not semantic_embeddings.is_semantic(),
    reason="active embedding backend is the lexical fallback, which cannot encode meaning",
)

# "login" and "credentials" appear, but nowhere near each other.
SPLIT_TERMS = "your login is stored separately from your account credentials"
PIPELINE = "the release pipeline runs nightly"
FRAMING_NOISE = "I need to search the documents and explain the results to my manager"


@pytest.fixture
def notes(db):
    ids = {}
    for name, text, tags in [
        ("split", SPLIT_TERMS, ["auth"]),
        ("pipeline", PIPELINE, ["ops"]),
        ("noise", FRAMING_NOISE, None),
    ]:
        note_id = notes_store.save_note(text, tags)
        semantic.index_note(note_id)
        ids[name] = note_id
    return ids


def notes_by_id(items) -> dict:
    return {item["id"]: item for item in items}


# ======================================================
# tokenize
# ======================================================
def test_terms_are_lowercased_and_split():
    assert tokenize("Login Credentials") == ["login", "credentials"]


def test_stopwords_are_removed():
    assert tokenize("the pipeline and the architecture of a system") == [
        "pipeline", "architecture", "system",
    ]


@pytest.mark.parametrize("word", ["the", "a", "an", "and", "or", "of", "for", "to"])
def test_each_documented_stopword_is_dropped(word):
    assert tokenize(f"{word} pipeline") == ["pipeline"]


def test_edge_punctuation_is_trimmed():
    assert tokenize("credentials?") == ["credentials"]
    assert tokenize("(pipeline), architecture.") == ["pipeline", "architecture"]


def test_interior_punctuation_is_kept():
    # "self-service" is one term, not two.
    assert tokenize("self-service portal") == ["self-service", "portal"]
    assert tokenize("user.name setting") == ["user.name", "setting"]


def test_duplicate_terms_are_dropped():
    assert tokenize("pipeline pipeline architecture") == ["pipeline", "architecture"]


def test_ordering_is_first_appearance():
    assert tokenize("zebra alpha zebra beta") == ["zebra", "alpha", "beta"]


def test_tokenizing_is_deterministic():
    query = "the login credentials for a pipeline"
    assert tokenize(query) == tokenize(query)


def test_an_all_stopword_query_has_no_terms():
    assert tokenize("the and or of") == []


def test_an_empty_query_has_no_terms():
    assert tokenize("") == []
    assert tokenize(None) == []


# ======================================================
# 1 & 2. Per-term and partial coverage
# ======================================================
def test_non_adjacent_terms_both_count():
    # The headline change: adjacency is phrasing, not relevance.
    assert relevance("login credentials", {"text": SPLIT_TERMS}) == pytest.approx(1.0)


def test_partial_coverage_is_a_fraction():
    assert relevance("pipeline architecture", {"text": PIPELINE}) == pytest.approx(0.5)


def test_one_of_three_terms():
    assert relevance("pipeline zebra giraffe", {"text": PIPELINE}) == pytest.approx(1 / 3)


def test_no_terms_present_scores_zero():
    assert relevance("zebra giraffe", {"text": PIPELINE}) == 0.0


def test_full_coverage_scores_one():
    assert relevance("release pipeline", {"text": PIPELINE}) == pytest.approx(1.0)


def test_stopwords_do_not_dilute_the_denominator():
    # "the pipeline" is one scoring term, so a perfect match is 1.0.
    assert relevance("the pipeline", {"text": PIPELINE}) == pytest.approx(1.0)


def test_an_all_stopword_query_scores_zero():
    assert relevance("the and of", {"text": PIPELINE}) == 0.0


def test_an_empty_query_scores_zero():
    assert relevance("", {"text": PIPELINE}) == 0.0


# ======================================================
# 3. Whole-word matching, no false positives
# ======================================================
def test_a_term_does_not_match_inside_a_longer_word():
    assert relevance("build", {"text": "rebuild the project"}) == 0.0
    assert relevance("log", {"text": "the login page"}) == 0.0
    assert relevance("port", {"text": "the portal is down"}) == 0.0


def test_a_whole_word_still_matches():
    assert relevance("build", {"text": "build the project"}) == pytest.approx(1.0)


def test_matching_is_case_insensitive():
    assert relevance("PIPELINE", {"text": "The Release Pipeline"}) == pytest.approx(1.0)


def test_punctuation_around_a_word_does_not_block_a_match():
    assert relevance("pipeline", {"text": "the (pipeline) failed."}) == pytest.approx(1.0)


def test_framing_words_do_not_match_prose(notes):
    # The noise note contains "search", "documents" and "explain"; a cleaned
    # query about something else must score nothing against it.
    items = notes_by_id(
        aria_memory.search_hybrid("search my documents for login credentials", min_score=0.0)
    )
    assert items[notes["noise"]]["keyword_score"] == 0.0


def test_the_relevant_note_beats_the_noise_note(notes):
    items = notes_by_id(
        aria_memory.search_hybrid("please explain the release pipeline", min_score=0.0)
    )
    assert items[notes["pipeline"]]["keyword_score"] > items[notes["noise"]]["keyword_score"]


# ======================================================
# Tags
# ======================================================
def test_a_term_in_the_tags_counts():
    assert relevance("unity", {"text": "nothing relevant", "tags": "unity,build"}) == 1.0


def test_text_and_tags_are_both_searched():
    item = {"text": "the release pipeline", "tags": "ops,nightly"}
    assert relevance("pipeline nightly", item) == pytest.approx(1.0)


def test_a_tag_matches_as_a_whole_word():
    assert relevance("build", {"text": "x", "tags": "rebuild,deploy"}) == 0.0


# ======================================================
# 4 & 5. Semantic-only and keyword-only hits
# ======================================================
def test_semantic_only_hits_are_scored_on_term_presence(notes):
    # The split-terms note is not returned by a LIKE for the whole phrase,
    # so it arrives through the semantic half -- and is still scored.
    assert notes_store.search_notes_keyword("login credentials") == []
    items = notes_by_id(aria_memory.search_hybrid("login credentials", min_score=0.0))
    assert items[notes["split"]]["keyword_score"] == pytest.approx(1.0)


def test_semantic_only_hits_that_share_no_terms_score_zero(notes):
    items = notes_by_id(aria_memory.search_hybrid("login credentials", min_score=0.0))
    assert items[notes["noise"]]["keyword_score"] == 0.0


def test_a_keyword_only_hit_has_a_zero_semantic_score(db, monkeypatch):
    note_id = notes_store.save_note("the release pipeline runs nightly")
    # No semantic index for this note, so only the keyword half finds it.
    items = notes_by_id(aria_memory.search_hybrid("pipeline", min_score=0.0))
    assert note_id in items
    assert items[note_id]["semantic_score"] == 0.0
    assert items[note_id]["keyword_score"] == pytest.approx(1.0)


def test_semantic_score_is_never_none(notes):
    for item in aria_memory.search_hybrid("login credentials", min_score=0.0):
        assert item["semantic_score"] is not None


def test_a_semantic_chunk_is_scored_against_its_whole_note(db):
    # A chunk is a 40-word window. This note says "login" in its first
    # window and "credentials" well past the end of it, so scoring the
    # returned chunk alone would report half coverage for a note that
    # contains both terms.
    filler = " ".join(f"word{n}" for n in range(80))
    note_id = notes_store.save_note(f"login {filler} credentials")
    semantic.index_note(note_id)

    # The whole phrase is not an adjacent substring, so the keyword half
    # cannot return it -- it arrives as a chunk from the semantic half.
    assert notes_store.search_notes_keyword("login credentials") == []
    items = notes_by_id(aria_memory.search_hybrid("login credentials", min_score=0.0))
    assert items[note_id]["type"] == "chunk"
    assert items[note_id]["keyword_score"] == pytest.approx(1.0)


def test_a_semantic_hit_counts_terms_in_the_notes_tags(db):
    # Chunks carry no tags of their own, so a tag term would score zero for
    # a semantic-only hit while counting for a keyword one.
    note_id = notes_store.save_note("the release runs nightly", ["pipeline"])
    semantic.index_note(note_id)

    assert notes_store.search_notes_keyword("nightly pipeline") == []
    items = notes_by_id(aria_memory.search_hybrid("nightly pipeline", min_score=0.0))
    assert items[note_id]["keyword_score"] == pytest.approx(1.0)


def test_a_chunk_row_still_carries_its_own_chunk_text(db):
    # Widening the keyword denominator must not smuggle the whole note onto
    # the row: which passage matched is what the caller displays.
    filler = " ".join(f"word{n}" for n in range(80))
    note_id = notes_store.save_note(f"login {filler} credentials")
    semantic.index_note(note_id)

    items = notes_by_id(aria_memory.search_hybrid("login credentials", min_score=0.0))
    hit = items[note_id]
    assert "text" not in hit
    assert hit["chunk"] in f"login {filler} credentials"
    assert hit["chunk"] != f"login {filler} credentials"


def test_the_note_is_read_once_however_many_chunks_it_returns(db, monkeypatch):
    filler = " ".join(f"word{n}" for n in range(200))
    note_id = notes_store.save_note(f"login {filler} credentials")
    assert semantic.index_note(note_id) > 1

    reads = []
    real_get_note = notes_store.get_note

    def counted(nid):
        reads.append(nid)
        return real_get_note(nid)

    monkeypatch.setattr(aria_memory, "get_note", counted)
    aria_memory.search_hybrid("login credentials", min_score=0.0)
    assert reads.count(note_id) <= 1


def test_a_deleted_note_falls_back_to_the_chunk(db, monkeypatch):
    note_id = notes_store.save_note("the release pipeline runs nightly")
    semantic.index_note(note_id)
    # An index row whose note is gone: scoring must not crash on the None.
    monkeypatch.setattr(aria_memory, "get_note", lambda nid: None)

    items = notes_by_id(aria_memory.search_hybrid("pipeline", min_score=0.0))
    if note_id in items and items[note_id]["type"] == "chunk":
        assert items[note_id]["keyword_score"] == pytest.approx(1.0)


def test_a_note_found_both_ways_keeps_both_scores(notes):
    items = notes_by_id(aria_memory.search_hybrid("pipeline", min_score=0.0))
    hit = items[notes["pipeline"]]
    assert hit["keyword_score"] > 0
    assert hit["semantic_score"] > 0


# ======================================================
# 6. Only the keyword term moved
# ======================================================
def test_ranking_weights_are_unchanged():
    assert memory_ranking.WEIGHT_SEMANTIC == 0.45
    assert memory_ranking.WEIGHT_KEYWORD == 0.25
    assert memory_ranking.WEIGHT_RECENCY == 0.20
    assert memory_ranking.WEIGHT_TYPE == 0.10


def test_score_floors_are_unchanged():
    from backend.files.file_ingestion import DEFAULT_MIN_SCORE as FILE_FLOOR

    assert semantic.DEFAULT_MIN_SCORE == 0.6
    assert FILE_FLOOR == 0.55


def test_combined_moves_only_by_the_keyword_term():
    old_coverage, new_coverage = 0.5, 1.0
    old = memory_ranking.normalize_scores(old_coverage, 0.7, 0.9, 1.0)
    new = memory_ranking.normalize_scores(new_coverage, 0.7, 0.9, 1.0)
    expected = memory_ranking.WEIGHT_KEYWORD * (new_coverage - old_coverage) / memory_ranking.MAX_COMBINED
    assert new - old == pytest.approx(expected)


def test_semantic_scores_are_untouched_by_the_change(notes):
    hybrid = {i["id"]: i["semantic_score"] for i in
              aria_memory.search_hybrid("login credentials", min_score=0.0)
              if i["type"] == "chunk"}
    direct = {h["note_id"]: h["score"] for h in
              semantic.search_semantic("login credentials", min_score=0.0)}
    for note_id, score in hybrid.items():
        if note_id in direct:
            assert score == pytest.approx(direct[note_id])


# ======================================================
# 7. Determinism
# ======================================================
def test_coverage_is_deterministic():
    item = {"text": SPLIT_TERMS}
    assert relevance("login credentials", item) == relevance("login credentials", item)


def test_hybrid_scores_are_deterministic(notes):
    first = aria_memory.search_hybrid("login credentials", min_score=0.0)
    second = aria_memory.search_hybrid("login credentials", min_score=0.0)
    assert [i["keyword_score"] for i in first] == [i["keyword_score"] for i in second]


def test_ranked_ordering_is_deterministic(notes):
    runs = [[i["id"] for i in aria_memory.search_ranked("login credentials")] for _ in range(5)]
    assert all(run == runs[0] for run in runs)


def test_term_order_does_not_change_coverage():
    item = {"text": SPLIT_TERMS}
    assert relevance("login credentials", item) == relevance("credentials login", item)


def test_relevance_does_not_mutate_the_item():
    item = {"text": PIPELINE, "tags": "ops"}
    before = dict(item)
    relevance("pipeline", item)
    assert item == before


# ======================================================
# Integration
# ======================================================
@semantic_only
def test_the_split_term_note_ranks_first(notes):
    ranked = aria_memory.search_ranked("search my documents for login credentials")
    assert ranked[0]["id"] == notes["split"]


def test_the_search_notes_tool_still_uses_a_substring(db, notes):
    # The public tool was explicitly left alone: it still requires the whole
    # query as an adjacent substring.
    from backend.core.tool_registry import execute_tool

    assert execute_tool("search_notes", {"query": "login credentials"}).value["count"] == 0
    assert execute_tool("search_notes", {"query": "login"}).value["count"] == 1
