# backend/tests/test_query_deframing.py
#
# backend/llm/query_deframing.py -- stripping conversational framing off a
# query before it is embedded.
#
# Two halves: the pure string rules, and the integration checks that the
# cleaned query is what reaches the embedder while the raw query is what
# reaches the display.

from __future__ import annotations

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory
from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.aria_memory import memory_ranking
from backend.files import file_ingestion as ingestion
from backend.files import file_search
from backend.llm import llm_engine, memory_prompting, semantic_embeddings
from backend.llm.query_deframing import deframe_query

HANDBOOK = """# Deployment handbook

The release pipeline runs every night at 2am and publishes to staging.

## Passwords

Use the self-service portal.
Never share the details with anyone.

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
    return ingestion.ingest_and_index(str(path), max_chars=90)


# ======================================================
# The documented examples
# ======================================================
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("search my documents for login credentials", "login credentials"),
        ("look up the pipeline architecture", "pipeline architecture"),
        ("tell me about the password policy", "password policy"),
    ],
)
def test_the_documented_examples(raw, expected):
    assert deframe_query(raw) == expected


# ======================================================
# Every framing phrase
# ======================================================
@pytest.mark.parametrize("phrase", list(__import__(
    "backend.llm.query_deframing", fromlist=["FRAMING_PHRASES"]).FRAMING_PHRASES))
def test_every_listed_phrase_is_stripped(phrase):
    assert deframe_query(f"{phrase} widget calibration") == "widget calibration"


def test_stripping_is_case_insensitive():
    assert deframe_query("Search My Documents For login credentials") == "login credentials"
    assert deframe_query("TELL ME ABOUT the pipeline") == "pipeline"


def test_the_longest_phrase_wins():
    # "what can you tell me about" must beat "can you tell me".
    assert deframe_query("what can you tell me about the outage") == "outage"


def test_multiple_prefixes_are_stripped():
    assert deframe_query("please explain tell me about the pipeline") == "pipeline"


def test_prefixes_are_stripped_repeatedly():
    assert deframe_query("can you tell me look up the invoices") == "invoices"


# ======================================================
# Articles and whitespace
# ======================================================
def test_a_leading_article_is_dropped():
    assert deframe_query("look up the sky") == "sky"
    assert deframe_query("tell me about a laptop") == "laptop"
    assert deframe_query("tell me about an outage") == "outage"


def test_an_article_is_only_dropped_after_framing():
    # An unframed query is left exactly as the user typed it.
    assert deframe_query("the pipeline architecture") == "the pipeline architecture"


def test_whitespace_is_collapsed():
    assert deframe_query("  search my files for   the   invoices  ") == "invoices"


def test_internal_whitespace_is_normalized():
    assert deframe_query("tell me about  the   coffee\tmachine") == "coffee machine"


def test_punctuation_between_framing_and_topic_is_handled():
    assert deframe_query("tell me about: the pipeline") == "pipeline"
    assert deframe_query("please explain, the release process") == "release process"


# ======================================================
# Word boundaries -- what must NOT be stripped
# ======================================================
@pytest.mark.parametrize(
    "raw",
    [
        "explaining the pipeline in detail",
        "look upward at the sky",
        "explainer videos about docker",
        "searching my documents by hand",
    ],
)
def test_a_phrase_inside_a_word_is_not_stripped(raw):
    assert deframe_query(raw) == raw


def test_framing_in_the_middle_is_kept():
    # Only leading phrases are framing; mid-sentence they are content.
    raw = "the docs explain the pipeline"
    assert deframe_query(raw) == raw


def test_an_ordinary_question_is_untouched():
    assert deframe_query("what is the capital of France?") == "what is the capital of France?"


# ======================================================
# Fallbacks
# ======================================================
def test_a_query_that_is_only_framing_is_returned_whole():
    # Stripping to nothing would retrieve nothing, which is worse than the
    # framed query that at least carries a topic.
    assert deframe_query("explain") == "explain"
    assert deframe_query("search my documents for") == "search my documents for"
    assert deframe_query("tell me about") == "tell me about"


def test_an_empty_query_is_returned_unchanged():
    assert deframe_query("") == ""
    assert deframe_query("   ") == "   "


def test_none_is_tolerated():
    assert deframe_query(None) is None


# ======================================================
# Determinism
# ======================================================
def test_output_is_deterministic():
    raw = "what can you tell me about the deployment schedule"
    assert deframe_query(raw) == deframe_query(raw)


def test_output_is_stable_across_repeated_calls():
    raw = "search my documents for login credentials"
    assert len({deframe_query(raw) for _ in range(10)}) == 1


def test_de_framing_is_idempotent():
    once = deframe_query("tell me about the password policy")
    assert deframe_query(once) == once


def test_the_input_is_not_mutated():
    raw = "tell me about the pipeline"
    deframe_query(raw)
    assert raw == "tell me about the pipeline"


# ======================================================
# Integration: retrieval uses the cleaned query
# ======================================================
def embedded_texts(monkeypatch) -> list:
    """Record every text the query side embeds, in order.

    Both encoding entry points are watched rather than just the singular
    one. A query the expansion table recognises is embedded as a batch --
    the cleaned query first, then its synonyms -- and a query it does not
    recognise still goes through embed_text_semantic alone. Which of the two
    carries it is plumbing; that the first text embedded is the cleaned
    query is the behaviour these tests are about.
    """
    seen = []
    real_one = semantic_embeddings.embed_text_semantic
    real_many = semantic_embeddings.embed_texts_semantic

    def one(text):
        seen.append(text)
        return real_one(text)

    def many(texts):
        seen.extend(texts)
        return real_many(texts)

    monkeypatch.setattr(semantic_embeddings, "embed_text_semantic", one)
    monkeypatch.setattr(semantic_embeddings, "embed_texts_semantic", many)
    return seen


def test_file_search_embeds_the_cleaned_query(db, indexed, monkeypatch):
    seen = embedded_texts(monkeypatch)
    ingestion.search_files_semantic("search my documents for login credentials", min_score=0.0)
    assert seen[0] == "login credentials"


def test_note_search_embeds_the_cleaned_query(db, monkeypatch):
    note_id = notes_store.save_note("the release pipeline runs nightly")
    semantic.index_note(note_id)

    seen = embedded_texts(monkeypatch)
    semantic.search_semantic("tell me about the release pipeline", min_score=0.0)
    assert seen[0] == "release pipeline"


def test_the_framed_query_is_never_embedded(db, monkeypatch):
    # The point of de-framing, stated directly: whatever expansion adds, no
    # embedded text may carry the wrapper the user typed.
    note_id = notes_store.save_note("the release pipeline runs nightly")
    semantic.index_note(note_id)

    seen = embedded_texts(monkeypatch)
    semantic.search_semantic("tell me about the release pipeline", min_score=0.0)
    assert not any("tell me about" in text for text in seen)


def test_ranking_embeds_the_cleaned_query(db, monkeypatch):
    seen = []
    real = semantic_embeddings.embed_text_semantic
    monkeypatch.setattr(
        semantic_embeddings, "embed_text_semantic",
        lambda text: (seen.append(text), real(text))[1],
    )
    memory_ranking.rank_items([{"id": 1, "type": "note", "text": "x"}], "look up the pipeline")
    assert "pipeline" in seen[0]
    assert "look up" not in seen[0]


# ======================================================
# Integration: display keeps the raw query
# ======================================================
def test_memory_context_displays_the_raw_query(db):
    note_id = notes_store.save_note("the release pipeline runs nightly")
    semantic.index_note(note_id)
    raw = "tell me about the release pipeline"
    assert memory_prompting.build_memory_context(raw)["query"] == raw


def test_file_context_displays_the_raw_query(db, indexed):
    raw = "search my documents for login credentials"
    assert memory_prompting.load_file_context(raw)["query"] == raw


def test_the_system_prompt_shows_the_raw_query(db, indexed):
    raw = "search my documents for login credentials"
    prompt = llm_engine.build_system_prompt(
        "base", query=raw, use_memory=False, use_files=True
    )
    assert f"Query: {raw}" in prompt


# ======================================================
# Integration: it actually helps
# ======================================================
@semantic_only
def test_the_cleaned_query_scores_higher_on_a_known_chunk(db, indexed):
    raw = "search my documents for login credentials"
    cleaned = deframe_query(raw)
    chunks = ingestion.get_file_chunks(indexed)

    def best(query):
        vector = semantic_embeddings.embed_text_semantic(query)
        return max(
            semantic_embeddings.cosine_similarity(
                vector, semantic_embeddings.embed_text_semantic(ingestion.embedding_text(chunk))
            )
            for chunk in chunks
        )

    assert best(cleaned) > best(raw)


@semantic_only
def test_a_framed_query_now_clears_the_score_floor(db, indexed):
    # This is the case that motivated the change: the framed form scored
    # 0.597 against a 0.6 floor before file search was recalibrated, and
    # de-framing lifts it clear rather than relying on the threshold.
    hits = ingestion.search_files_semantic("search my documents for login credentials")
    assert hits
    assert hits[0]["section"] == "Passwords"


@semantic_only
def test_framing_does_not_rescue_an_unrelated_query(db, indexed):
    # De-framing must not lift everything above the floor -- "search my
    # documents for" used to match document-ish prose on its own.
    assert ingestion.search_files_semantic("search my documents for volcano eruptions") == []


@semantic_only
def test_a_framed_and_bare_query_retrieve_the_same_chunk(db, indexed):
    framed = ingestion.search_files_semantic("tell me about the coffee machine", min_score=0.0)
    bare = ingestion.search_files_semantic("coffee machine", min_score=0.0)
    assert framed[0]["chunk_id"] == bare[0]["chunk_id"]
    assert framed[0]["semantic_score"] == pytest.approx(bare[0]["semantic_score"])


def test_ranked_file_search_still_works_with_framing(db, indexed):
    ranked = file_search.search_files_ranked("search my documents for the coffee machine")
    assert ranked
    assert all("combined_score" in row for row in ranked)


def test_hybrid_memory_search_still_works_with_framing(db):
    note_id = notes_store.save_note("the release pipeline runs nightly")
    semantic.index_note(note_id)
    assert aria_memory.search_ranked("tell me about the release pipeline")
