# backend/tests/test_file_sections.py
#
# Section headings on file chunks: detected at chunk time, stored on the
# chunk, prepended to the text before embedding, and surfaced in the File
# Context display.
#
# The point of the feature is the embedding: a chunk whose body never says
# "password" should still retrieve for a password question when it sits under
# a "## Passwords" heading. That is what test_a_chunk_is_findable_by_its_
# section_alone pins down; everything else supports it.

from __future__ import annotations

import pytest

from phase3_upgrade_helpers import columns_of, db, raw_query  # noqa: F401

from backend.files import file_ingestion as ingestion
from backend.llm import llm_engine, memory_prompting, semantic_embeddings

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
def sample_file(tmp_path):
    path = tmp_path / "handbook.md"
    path.write_text(HANDBOOK, encoding="utf-8")
    return path


@pytest.fixture
def indexed(db, sample_file):
    return ingestion.ingest_and_index(str(sample_file), max_chars=90)


def chunk_under(file_id: int, section: str) -> dict:
    matches = [c for c in ingestion.get_file_chunks(file_id) if c["section"] == section]
    assert matches, f"no chunk under {section!r}"
    return matches[0]


# ======================================================
# Heading detection
# ======================================================
def test_headings_are_found_with_their_offsets():
    headings = ingestion.find_headings(HANDBOOK)
    assert [title for _offset, title in headings] == [
        "Deployment handbook", "Passwords", "Catering",
    ]
    assert all(HANDBOOK[offset] == "#" for offset, _title in headings)


def test_hashes_are_stripped_from_the_title():
    assert ingestion.find_headings("## Passwords\n")[0][1] == "Passwords"


def test_every_atx_level_is_matched():
    text = "\n".join(f"{'#' * n} Level {n}" for n in range(1, 7))
    assert [t for _o, t in ingestion.find_headings(text)] == [f"Level {n}" for n in range(1, 7)]


def test_closing_hashes_are_stripped():
    assert ingestion.find_headings("## Passwords ##\n")[0][1] == "Passwords"


def test_a_hash_without_a_space_is_not_a_heading():
    # "#hashtag" is a word, not a section.
    assert ingestion.find_headings("#hashtag in a sentence\n") == []


def test_a_hash_mid_line_is_not_a_heading():
    assert ingestion.find_headings("see issue # 42 for details\n") == []


def test_a_document_without_headings_has_none():
    assert ingestion.find_headings("just prose\nacross two lines\n") == []


def test_section_at_returns_the_heading_in_force():
    headings = ingestion.find_headings(HANDBOOK)
    passwords_offset = HANDBOOK.index("## Passwords")
    assert ingestion.section_at(headings, passwords_offset) == "Passwords"
    assert ingestion.section_at(headings, passwords_offset + 30) == "Passwords"
    assert ingestion.section_at(headings, HANDBOOK.index("## Catering")) == "Catering"


def test_text_above_the_first_heading_has_no_section():
    headings = ingestion.find_headings("intro line\n\n## First\nbody\n")
    assert ingestion.section_at(headings, 0) is None


def test_section_at_on_a_document_without_headings():
    assert ingestion.section_at([], 100) is None


# ======================================================
# Storage
# ======================================================
def test_the_chunk_table_has_a_section_column(db):
    assert "section" in columns_of(db, "file_chunks")


def test_chunks_record_their_section(db, indexed):
    sections = [c["section"] for c in ingestion.get_file_chunks(indexed)]
    assert "Passwords" in sections
    assert "Catering" in sections


def test_the_section_is_stored_without_hashes(db, indexed):
    for section in raw_query(db, "SELECT DISTINCT section FROM file_chunks"):
        assert section[0] is None or not section[0].startswith("#")


def test_a_chunk_reports_the_section_it_starts_in(db, indexed):
    chunk = chunk_under(indexed, "Passwords")
    assert "self-service" in chunk["text"]


def test_a_file_without_headings_stores_no_section(db, tmp_path):
    path = tmp_path / "plain.txt"
    path.write_text("just prose, no headings anywhere in this file at all.\n", encoding="utf-8")
    file_id = ingestion.ingest_and_index(str(path))
    assert all(c["section"] is None for c in ingestion.get_file_chunks(file_id))


def test_rechunking_recomputes_the_sections(db, sample_file):
    file_id = ingestion.ingest_file(str(sample_file))
    ingestion.chunk_file(file_id, max_chars=90)
    first = [c["section"] for c in ingestion.get_file_chunks(file_id)]
    ingestion.chunk_file(file_id, max_chars=90)
    assert [c["section"] for c in ingestion.get_file_chunks(file_id)] == first


# ======================================================
# Embedding text
# ======================================================
def test_the_section_is_prepended_before_embedding():
    text = ingestion.embedding_text({"section": "Passwords", "text": "Use the portal."})
    assert text.startswith("Passwords")
    assert "Use the portal." in text


def test_a_chunk_without_a_section_embeds_its_body_alone():
    assert ingestion.embedding_text({"section": None, "text": "body"}) == "body"
    assert ingestion.embedding_text({"text": "body"}) == "body"


def test_a_blank_section_is_ignored():
    assert ingestion.embedding_text({"section": "   ", "text": "body"}) == "body"


def test_a_heading_already_in_the_body_is_not_repeated():
    # The first chunk of a section usually contains its own heading.
    text = ingestion.embedding_text(
        {"section": "Passwords", "text": "## Passwords\n\nUse the portal."}
    )
    assert text.count("Passwords") == 1


def test_embedding_text_is_deterministic():
    chunk = {"section": "Passwords", "text": "Use the portal."}
    assert ingestion.embedding_text(chunk) == ingestion.embedding_text(chunk)


# ======================================================
# Retrieval -- the point of the feature
# ======================================================
@semantic_only
def test_a_chunk_is_findable_by_its_section_alone(db, indexed):
    # The body of the Passwords chunk contains no form of the word
    # "password"; only its heading does.
    chunk = chunk_under(indexed, "Passwords")
    assert "password" not in (chunk["text"] or "").lower()

    hits = ingestion.search_files_semantic("how do I change my password")
    assert hits
    assert hits[0]["section"] == "Passwords"


@semantic_only
def test_the_section_header_raises_the_score(db, indexed):
    chunk = chunk_under(indexed, "Passwords")
    query = semantic_embeddings.embed_text_semantic("how do I change my password")
    without = semantic_embeddings.cosine_similarity(
        query, semantic_embeddings.embed_text_semantic(chunk["text"])
    )
    with_header = semantic_embeddings.cosine_similarity(
        query, semantic_embeddings.embed_text_semantic(ingestion.embedding_text(chunk))
    )
    assert with_header > without


def test_search_results_carry_the_section(db, indexed):
    hits = ingestion.search_files_semantic("coffee machine", min_score=0.0)
    assert hits
    assert "section" in hits[0]


def test_unrelated_queries_still_score_below_the_floor(db, indexed):
    # The header must not drag everything above the threshold.
    assert ingestion.search_files_semantic("photosynthesis in tropical plants") == []


def test_search_is_deterministic_with_sections(db, indexed):
    first = ingestion.search_files_semantic("password", min_score=0.0)
    second = ingestion.search_files_semantic("password", min_score=0.0)
    assert [h["chunk_id"] for h in first] == [h["chunk_id"] for h in second]
    assert [h["section"] for h in first] == [h["section"] for h in second]


# ======================================================
# Display
# ======================================================
def test_the_section_appears_in_the_rendered_line():
    line = memory_prompting._file_item_lines(
        {"items": [{"type": "file_chunk", "text": "body", "section": "Passwords"}]}
    )[0]
    assert "section=Passwords" in line


def test_a_missing_section_is_omitted_from_the_line():
    line = memory_prompting._file_item_lines(
        {"items": [{"type": "file_chunk", "text": "body", "section": None}]}
    )[0]
    assert "section=" not in line
    assert "None" not in line


def test_the_section_sits_between_chunk_and_semantic():
    line = memory_prompting._file_item_lines(
        {"items": [{"type": "file_chunk", "text": "b", "chunk_index": 1,
                    "section": "Passwords", "semantic_score": 0.5}]}
    )[0]
    assert line.index("chunk=") < line.index("section=") < line.index("semantic=")


def test_the_injected_section_shows_the_heading(db, indexed):
    prompt = llm_engine.build_system_prompt(
        "base", query="how do I change my password", use_memory=False, use_files=True
    )
    assert "section=Passwords" in prompt


def test_memory_items_never_show_a_section(db):
    line = memory_prompting._item_lines(
        {"items": [{"type": "note", "text": "a note", "section": "ignored"}]}
    )[0]
    assert "section=" not in line
