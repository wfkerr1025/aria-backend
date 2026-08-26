# backend/tests/test_file_ingestion.py
#
# backend/files/file_ingestion.py -- reading a file, recording it, chunking
# it, embedding the chunks and searching them.
#
# Files are written into pytest's tmp_path and the database is the usual
# throwaway, so nothing here touches the user's disk or the real store.

from __future__ import annotations

import sqlite3

import pytest

from phase3_upgrade_helpers import columns_of, db, raw_query  # noqa: F401

from backend.files import file_ingestion as ingestion
from backend.llm import semantic_embeddings

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
def sample_file(tmp_path):
    path = tmp_path / "handbook.md"
    path.write_text(HANDBOOK, encoding="utf-8")
    return path


@pytest.fixture
def indexed(db, sample_file):
    """An ingested, chunked and embedded file."""
    file_id = ingestion.ingest_and_index(str(sample_file), max_chars=120)
    return file_id


# ======================================================
# Schema
# ======================================================
def test_files_table_has_the_phase4_columns(db):
    for column in ["path", "name", "mime", "size_bytes", "created_at", "updated_at"]:
        assert column in columns_of(db, "files")


def test_file_chunks_table_has_the_phase4_columns(db):
    for column in [
        "file_id", "chunk_index", "text", "start_offset", "end_offset",
        "created_at", "updated_at", "model", "dim",
    ]:
        assert column in columns_of(db, "file_chunks")


def test_existing_columns_are_untouched(db):
    # The migration only adds; nothing that was there before is removed.
    assert columns_of(db, "files")[:5] == ["id", "path", "created_at", "last_indexed", "tags"]
    assert columns_of(db, "file_chunks")[:4] == ["id", "file_id", "chunk", "vector"]


# ======================================================
# ingest_file
# ======================================================
def test_ingest_returns_a_file_id(db, sample_file):
    assert isinstance(ingestion.ingest_file(str(sample_file)), int)


def test_ingest_records_the_file(db, sample_file):
    file_id = ingestion.ingest_file(str(sample_file))
    record = ingestion.get_file(file_id)
    assert record["name"] == "handbook.md"
    assert record["mime"] == "text/markdown"
    # The on-disk size, not the length of the source string: writing text
    # translates newlines on Windows, and size_bytes describes the file.
    assert record["size_bytes"] == sample_file.stat().st_size
    assert record["created_at"] and record["updated_at"]


def test_ingest_writes_exactly_one_row(db, sample_file):
    ingestion.ingest_file(str(sample_file))
    assert raw_query(db, "SELECT COUNT(*) FROM files")[0][0] == 1


def test_name_comes_from_the_basename(db, tmp_path):
    path = tmp_path / "nested" / "report.txt"
    path.parent.mkdir()
    path.write_text("content", encoding="utf-8")
    assert ingestion.get_file(ingestion.ingest_file(str(path)))["name"] == "report.txt"


@pytest.mark.parametrize(
    "suffix, expected",
    [(".txt", "text/plain"), (".md", "text/markdown"), (".log", "text/plain"),
     (".csv", "text/csv"), (".unknown", "text/plain")],
)
def test_mime_is_inferred_from_the_suffix(db, tmp_path, suffix, expected):
    path = tmp_path / f"file{suffix}"
    path.write_text("content", encoding="utf-8")
    assert ingestion.get_file(ingestion.ingest_file(str(path)))["mime"] == expected


def test_an_explicit_mime_overrides_inference(db, sample_file):
    file_id = ingestion.ingest_file(str(sample_file), mime="application/custom")
    assert ingestion.get_file(file_id)["mime"] == "application/custom"


def test_re_ingesting_updates_rather_than_duplicates(db, sample_file):
    first = ingestion.ingest_file(str(sample_file))
    sample_file.write_text(HANDBOOK + "\nA new closing line.\n", encoding="utf-8")
    second = ingestion.ingest_file(str(sample_file))
    assert first == second
    assert raw_query(db, "SELECT COUNT(*) FROM files")[0][0] == 1
    assert ingestion.get_file(first)["size_bytes"] == sample_file.stat().st_size


def test_a_missing_file_is_refused(db, tmp_path):
    with pytest.raises(ingestion.FileIngestionError):
        ingestion.ingest_file(str(tmp_path / "nope.txt"))


def test_a_directory_is_refused(db, tmp_path):
    with pytest.raises(ingestion.FileIngestionError):
        ingestion.ingest_file(str(tmp_path))


def test_undecodable_bytes_do_not_stop_ingestion(db, tmp_path):
    path = tmp_path / "binary.log"
    path.write_bytes(b"good text \xff\xfe more text")
    assert isinstance(ingestion.ingest_file(str(path)), int)


def test_get_file_returns_none_for_an_unknown_id(db):
    assert ingestion.get_file(4242) is None


def test_list_files_reports_everything_ingested(db, tmp_path):
    for name in ("a.txt", "b.txt"):
        path = tmp_path / name
        path.write_text("content", encoding="utf-8")
        ingestion.ingest_file(str(path))
    assert len(ingestion.list_files()) == 2


# ======================================================
# split_text / chunk_file
# ======================================================
def test_split_respects_max_chars():
    for chunk, _s, _e in ingestion.split_text(HANDBOOK, max_chars=100):
        assert len(chunk) <= 100


def test_split_prefers_line_breaks():
    chunks = ingestion.split_text(HANDBOOK, max_chars=120)
    # No chunk should start mid-word if a line break was available.
    assert all(not chunk.startswith(" ") for chunk, _s, _e in chunks)


def test_split_handles_a_line_longer_than_the_window():
    long_line = "x" * 500
    chunks = ingestion.split_text(long_line, max_chars=100)
    assert len(chunks) == 5
    assert all(len(chunk) <= 100 for chunk, _s, _e in chunks)


def test_split_of_empty_text_is_empty():
    assert ingestion.split_text("") == []
    assert ingestion.split_text("   \n  \n") == []


def test_split_rejects_a_nonsense_window():
    with pytest.raises(ValueError):
        ingestion.split_text("text", max_chars=0)


def test_chunk_file_stores_chunks(db, sample_file):
    file_id = ingestion.ingest_file(str(sample_file))
    chunks = ingestion.chunk_file(file_id, max_chars=120)
    assert chunks
    assert raw_query(db, "SELECT COUNT(*) FROM file_chunks")[0][0] == len(chunks)


def test_chunk_index_is_a_zero_based_sequence(db, sample_file):
    file_id = ingestion.ingest_file(str(sample_file))
    chunks = ingestion.chunk_file(file_id, max_chars=120)
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))


def test_offsets_are_monotonic(db, sample_file):
    file_id = ingestion.ingest_file(str(sample_file))
    chunks = ingestion.chunk_file(file_id, max_chars=120)
    for earlier, later in zip(chunks, chunks[1:]):
        assert earlier["start_offset"] < later["start_offset"]
        assert earlier["end_offset"] <= later["start_offset"]


def test_offsets_locate_the_chunk_in_the_file(db, sample_file):
    file_id = ingestion.ingest_file(str(sample_file))
    for chunk in ingestion.chunk_file(file_id, max_chars=120):
        span = HANDBOOK[chunk["start_offset"]:chunk["end_offset"]]
        assert chunk["text"] in span


def test_chunk_text_respects_max_chars(db, sample_file):
    file_id = ingestion.ingest_file(str(sample_file))
    assert all(len(c["text"]) <= 120 for c in ingestion.chunk_file(file_id, max_chars=120))


def test_rechunking_replaces_rather_than_appends(db, sample_file):
    file_id = ingestion.ingest_file(str(sample_file))
    first = ingestion.chunk_file(file_id, max_chars=120)
    second = ingestion.chunk_file(file_id, max_chars=120)
    assert len(first) == len(second)
    assert raw_query(db, "SELECT COUNT(*) FROM file_chunks")[0][0] == len(second)


def test_chunking_an_unknown_file_is_empty(db):
    assert ingestion.chunk_file(4242) == []


def test_the_legacy_chunk_column_mirrors_text(db, sample_file):
    # file_chunks.chunk predates this module and is NOT NULL, so it is
    # written with the same value rather than left to diverge.
    file_id = ingestion.ingest_file(str(sample_file))
    ingestion.chunk_file(file_id, max_chars=120)
    rows = raw_query(db, "SELECT text, chunk FROM file_chunks")
    assert all(text == chunk for text, chunk in rows)


# ======================================================
# embed_file_chunks
# ======================================================
def test_embedding_tags_every_chunk(db, indexed):
    rows = raw_query(db, "SELECT model, dim FROM file_chunks WHERE file_id = ?", (indexed,))
    assert rows
    for model, dim in rows:
        assert model == semantic_embeddings.backend_id()
        assert dim == semantic_embeddings.dimension()


def test_embedding_stores_a_real_vector(db, indexed):
    rows = raw_query(db, "SELECT vector FROM file_chunks WHERE file_id = ?", (indexed,))
    for (blob,) in rows:
        assert len(blob) == semantic_embeddings.dimension() * 4
        assert len(semantic_embeddings.decode_vector(bytes(blob))) == semantic_embeddings.dimension()


def test_file_id_is_present_on_every_chunk(db, indexed):
    rows = raw_query(db, "SELECT file_id FROM file_chunks")
    assert rows
    assert all(file_id == indexed for (file_id,) in rows)


def test_chunks_are_unembedded_until_embedding_runs(db, sample_file):
    file_id = ingestion.ingest_file(str(sample_file))
    ingestion.chunk_file(file_id, max_chars=120)
    rows = raw_query(db, "SELECT vector, model FROM file_chunks")
    assert all(len(blob) == 0 and model is None for blob, model in rows)


def test_embedding_an_unchunked_file_is_a_no_op(db, sample_file):
    file_id = ingestion.ingest_file(str(sample_file))
    ingestion.embed_file_chunks(file_id)
    assert raw_query(db, "SELECT COUNT(*) FROM file_chunks")[0][0] == 0


def test_embedding_does_not_touch_the_note_index(db, indexed):
    # File vectors live in file_chunks; semantic_index stays a notes-only
    # table so its NOT NULL note_id and cascade keep meaning.
    assert raw_query(db, "SELECT COUNT(*) FROM semantic_index")[0][0] == 0


# ======================================================
# search_files_semantic
# ======================================================
def test_search_returns_only_file_chunks(db, indexed):
    hits = ingestion.search_files_semantic("anything", min_score=0.0)
    assert hits
    assert all(hit["type"] == "file_chunk" for hit in hits)


def test_search_result_shape(db, indexed):
    hit = ingestion.search_files_semantic("credentials", min_score=0.0)[0]
    assert sorted(hit) == [
        "chunk_id", "chunk_index", "created_at", "end_offset", "file_id",
        "section", "semantic_score", "start_offset", "text", "type",
    ]


def test_search_scores_are_positive_for_a_related_query(db, indexed):
    hits = ingestion.search_files_semantic("password reset", min_score=0.0)
    assert hits
    assert hits[0]["semantic_score"] > 0


def test_search_respects_min_score(db, indexed):
    assert ingestion.search_files_semantic("credentials", min_score=0.99) == []


def test_search_respects_the_limit(db, indexed):
    assert len(ingestion.search_files_semantic("the", limit=1, min_score=0.0)) == 1


def test_search_orders_by_score(db, indexed):
    scores = [h["semantic_score"] for h in ingestion.search_files_semantic("x", min_score=0.0)]
    assert scores == sorted(scores, reverse=True)


def test_search_on_an_empty_store(db):
    assert ingestion.search_files_semantic("anything") == []


def test_search_skips_unembedded_chunks(db, sample_file):
    file_id = ingestion.ingest_file(str(sample_file))
    ingestion.chunk_file(file_id, max_chars=120)
    assert ingestion.search_files_semantic("anything", min_score=0.0) == []


def test_search_skips_chunks_from_another_backend(db, indexed):
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE file_chunks SET model = 'some-old-encoder'")
        conn.commit()
    finally:
        conn.close()
    assert ingestion.search_files_semantic("credentials", min_score=0.0) == []


def test_reindexing_brings_chunks_back(db, indexed):
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE file_chunks SET model = 'some-old-encoder'")
        conn.commit()
    finally:
        conn.close()
    ingestion.embed_file_chunks(indexed)
    assert ingestion.search_files_semantic("credentials", min_score=0.0)


@semantic_only
def test_search_matches_meaning_not_words(db, indexed):
    # "I forgot my login credentials" shares no content word with
    # "To reset your account credentials, use the self-service portal."
    hits = ingestion.search_files_semantic("I forgot my sign-in details")
    assert hits
    assert "credentials" in hits[0]["text"] or "login" in hits[0]["text"]


@semantic_only
def test_an_unrelated_query_finds_nothing(db, indexed):
    assert ingestion.search_files_semantic("photosynthesis in tropical plants") == []


def test_search_is_deterministic(db, indexed):
    first = ingestion.search_files_semantic("credentials", min_score=0.0)
    second = ingestion.search_files_semantic("credentials", min_score=0.0)
    assert first == second
