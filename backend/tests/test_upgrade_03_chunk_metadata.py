# backend/tests/test_upgrade_03_chunk_metadata.py
#
# Upgrade 3: chunk_index / start_word / end_word / created_at on the chunk
# tables, plus the ALTER TABLE migration that adds them to a database
# created before they existed.

from __future__ import annotations

from phase3_upgrade_helpers import (  # noqa: F401
    assert_iso_utc,
    columns_of,
    db,
    legacy_db,
    raw_query,
)

from backend import aria_memory_db as memory_db
from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic

METADATA_COLUMNS = ["chunk_index", "start_word", "end_word", "created_at"]

LEGACY_SCHEMA = """
CREATE TABLE notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    text TEXT NOT NULL,
    tags TEXT,
    source TEXT
);
CREATE TABLE semantic_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL,
    chunk TEXT NOT NULL,
    vector BLOB NOT NULL
);
CREATE TABLE file_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    chunk TEXT NOT NULL,
    vector BLOB NOT NULL
);
"""


def long_text(words=100):
    return " ".join(f"word{n}" for n in range(words))


# ---------------- schema ----------------
def test_semantic_index_has_the_metadata_columns(db):
    for column in METADATA_COLUMNS:
        assert column in columns_of(db, "semantic_index")


def test_file_chunks_has_the_metadata_columns(db):
    for column in METADATA_COLUMNS:
        assert column in columns_of(db, "file_chunks")


def test_metadata_columns_are_nullable(db):
    # SQLite cannot ALTER IN a NOT NULL column without a default, so these
    # have to be nullable -- and a NULL honestly marks a pre-migration row.
    info = {row[1]: row[3] for row in raw_query(db, "PRAGMA table_info(semantic_index)")}
    assert all(info[column] == 0 for column in METADATA_COLUMNS)


# ---------------- spans ----------------
def test_chunk_spans_reports_positions(db):
    spans = semantic.chunk_spans(long_text(100))
    assert spans[0][1] == 0
    assert all(end > start for _chunk, start, end in spans)


def test_spans_advance_by_the_step_size(db):
    spans = semantic.chunk_spans(long_text(100))
    step = semantic.DEFAULT_CHUNK_WORDS - semantic.DEFAULT_CHUNK_OVERLAP
    assert spans[1][1] - spans[0][1] == step


def test_span_matches_the_chunk_length(db):
    for chunk, start, end in semantic.chunk_spans(long_text(100)):
        assert len(chunk.split()) == end - start


def test_final_span_ends_at_the_last_word(db):
    words = long_text(100).split()
    assert semantic.chunk_spans(long_text(100))[-1][2] == len(words)


def test_chunk_text_still_returns_plain_strings(db):
    assert semantic.chunk_text(long_text(100)) == [
        chunk for chunk, _s, _e in semantic.chunk_spans(long_text(100))
    ]


# ---------------- population ----------------
def test_indexing_writes_the_metadata(db):
    note_id = notes_store.save_note(long_text(100))
    semantic.index_note(note_id)
    rows = raw_query(
        db,
        "SELECT chunk_index, start_word, end_word, created_at "
        "FROM semantic_index WHERE note_id = ? ORDER BY chunk_index",
        (note_id,),
    )
    assert [row[0] for row in rows] == list(range(len(rows)))
    assert all(row[1] is not None and row[2] is not None for row in rows)
    for row in rows:
        assert_iso_utc(row[3])


def test_chunk_index_starts_at_zero(db):
    note_id = notes_store.save_note(long_text(100))
    semantic.index_note(note_id)
    assert semantic.get_note_chunks(note_id)[0]["chunk_index"] == 0


def test_get_note_chunks_exposes_the_metadata(db):
    note_id = notes_store.save_note(long_text(100))
    semantic.index_note(note_id)
    chunk = semantic.get_note_chunks(note_id)[0]
    for key in METADATA_COLUMNS:
        assert key in chunk


def test_get_note_chunks_is_ordered_by_chunk_index(db):
    note_id = notes_store.save_note(long_text(100))
    semantic.index_note(note_id)
    indexes = [chunk["chunk_index"] for chunk in semantic.get_note_chunks(note_id)]
    assert indexes == sorted(indexes)


def test_stored_span_locates_the_chunk_in_the_note(db):
    text = long_text(100)
    note_id = notes_store.save_note(text)
    semantic.index_note(note_id)
    words = text.split()
    for chunk in semantic.get_note_chunks(note_id):
        span = words[chunk["start_word"] : chunk["end_word"]]
        assert " ".join(span) == chunk["chunk"]


def test_search_results_carry_the_metadata(db):
    note_id = notes_store.save_note(long_text(100))
    semantic.index_note(note_id)
    hit = semantic.search_semantic("word1")[0]
    for key in ["chunk_index", "start_word", "end_word"]:
        assert key in hit


def test_reindexing_renumbers_from_zero(db):
    note_id = notes_store.save_note(long_text(100))
    semantic.index_note(note_id)
    semantic.index_note(note_id)
    indexes = [chunk["chunk_index"] for chunk in semantic.get_note_chunks(note_id)]
    assert indexes == list(range(len(indexes)))


# ---------------- migration ----------------
def test_legacy_database_gains_the_columns(legacy_db):
    path = legacy_db(LEGACY_SCHEMA)
    for column in METADATA_COLUMNS:
        assert column in columns_of(path, "semantic_index")
        assert column in columns_of(path, "file_chunks")


def test_migration_preserves_existing_rows(legacy_db):
    path = legacy_db(
        LEGACY_SCHEMA,
        [
            ("INSERT INTO notes (created_at, text) VALUES (?, ?)", ("2020-01-01T00:00:00+00:00", "old")),
            ("INSERT INTO semantic_index (note_id, chunk, vector) VALUES (?, ?, ?)", (1, "old chunk", b"\x00")),
        ],
    )
    rows = raw_query(path, "SELECT chunk, chunk_index FROM semantic_index")
    assert rows == [("old chunk", None)]


def test_migration_is_idempotent(legacy_db):
    path = legacy_db(LEGACY_SCHEMA)
    memory_db.init_db()
    memory_db.init_db()
    assert columns_of(path, "semantic_index").count("chunk_index") == 1


def test_migrated_database_accepts_new_metadata_rows(legacy_db):
    path = legacy_db(LEGACY_SCHEMA)
    note_id = notes_store.save_note(long_text(100))
    assert semantic.index_note(note_id) > 0
    assert semantic.get_note_chunks(note_id)[0]["chunk_index"] == 0
    assert raw_query(path, "SELECT COUNT(*) FROM semantic_index")[0][0] > 0
