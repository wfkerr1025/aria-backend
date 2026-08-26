# backend/tests/test_upgrade_04_semantic_index_fk.py
#
# Upgrade 4: foreign keys enforced per connection, semantic_index declared
# ON DELETE CASCADE, and the table rebuild that brings an older database up
# to that constraint.

from __future__ import annotations

import sqlite3

import pytest

from phase3_upgrade_helpers import (  # noqa: F401
    columns_of,
    db,
    legacy_db,
    raw_query,
    table_sql,
)

from backend import aria_memory_db as memory_db
from backend import aria_memory_embeddings as embeddings_store
from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic

LEGACY_NO_CASCADE = """
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
    vector BLOB NOT NULL,
    FOREIGN KEY(note_id) REFERENCES notes(id)
);
"""


def long_text(words=60):
    return " ".join(f"word{n}" for n in range(words))


# ---------------- enforcement ----------------
def test_connections_enforce_foreign_keys(db):
    conn = memory_db.get_connection()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_semantic_index_declares_the_cascade(db):
    assert "ON DELETE CASCADE" in table_sql(db, "semantic_index").upper()


def test_orphan_chunk_is_rejected(db):
    conn = memory_db.get_connection()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO semantic_index (note_id, chunk, vector) VALUES (?, ?, ?)",
                (4242, "orphan", b"\x00"),
            )
            conn.commit()
    finally:
        conn.close()
    assert raw_query(db, "SELECT COUNT(*) FROM semantic_index")[0][0] == 0


def test_orphan_embedding_is_rejected(db):
    with pytest.raises(sqlite3.IntegrityError):
        embeddings_store.store_embedding(4242, b"orphan")
    assert raw_query(db, "SELECT COUNT(*) FROM embeddings")[0][0] == 0


def test_a_real_note_still_accepts_chunks_and_vectors(db):
    note_id = notes_store.save_note(long_text())
    assert semantic.index_note(note_id) > 0
    assert isinstance(embeddings_store.store_embedding(note_id, b"vec"), int)


# ---------------- cascade ----------------
def test_deleting_a_note_cascades_to_its_chunks(db):
    note_id = notes_store.save_note(long_text())
    semantic.index_note(note_id)
    assert raw_query(db, "SELECT COUNT(*) FROM semantic_index")[0][0] > 0

    conn = memory_db.get_connection()
    try:
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        conn.commit()
    finally:
        conn.close()

    assert raw_query(db, "SELECT COUNT(*) FROM semantic_index")[0][0] == 0


def test_cascade_only_touches_the_deleted_note(db):
    doomed = notes_store.save_note(long_text())
    kept = notes_store.save_note(long_text())
    semantic.index_note(doomed)
    semantic.index_note(kept)

    conn = memory_db.get_connection()
    try:
        conn.execute("DELETE FROM notes WHERE id = ?", (doomed,))
        conn.commit()
    finally:
        conn.close()

    assert semantic.get_note_chunks(doomed) == []
    assert semantic.get_note_chunks(kept) != []


def test_embeddings_do_not_cascade(db):
    # Deliberate: embeddings has no ON DELETE CASCADE, so the delete is
    # refused rather than silently orphaning vectors. delete_note()
    # (upgrade 8) is what clears them first.
    note_id = notes_store.save_note("has a vector")
    embeddings_store.store_embedding(note_id, b"vec")
    conn = memory_db.get_connection()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            conn.commit()
    finally:
        conn.close()


# ---------------- migration ----------------
def test_legacy_table_is_rebuilt_with_the_cascade(legacy_db):
    path = legacy_db(LEGACY_NO_CASCADE)
    assert "ON DELETE CASCADE" in table_sql(path, "semantic_index").upper()


def test_rebuild_keeps_valid_rows(legacy_db):
    path = legacy_db(
        LEGACY_NO_CASCADE,
        [
            ("INSERT INTO notes (created_at, text) VALUES (?, ?)", ("2020-01-01T00:00:00+00:00", "kept")),
            ("INSERT INTO semantic_index (note_id, chunk, vector) VALUES (?, ?, ?)", (1, "kept chunk", b"\x01")),
        ],
    )
    assert raw_query(path, "SELECT note_id, chunk FROM semantic_index") == [(1, "kept chunk")]


def test_rebuild_drops_rows_that_cannot_satisfy_the_constraint(legacy_db):
    path = legacy_db(
        LEGACY_NO_CASCADE,
        [
            ("INSERT INTO notes (created_at, text) VALUES (?, ?)", ("2020-01-01T00:00:00+00:00", "kept")),
            ("INSERT INTO semantic_index (note_id, chunk, vector) VALUES (?, ?, ?)", (1, "kept chunk", b"\x01")),
            ("INSERT INTO semantic_index (note_id, chunk, vector) VALUES (?, ?, ?)", (77, "orphan chunk", b"\x02")),
        ],
    )
    chunks = [row[0] for row in raw_query(path, "SELECT chunk FROM semantic_index")]
    assert chunks == ["kept chunk"]


def test_rebuild_keeps_the_columns_and_index(legacy_db):
    path = legacy_db(LEGACY_NO_CASCADE)
    assert columns_of(path, "semantic_index") == [
        "id", "note_id", "chunk", "vector",
        "chunk_index", "start_word", "end_word", "created_at",
        "model", "dim",
    ]
    indexes = raw_query(
        path, "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        ("idx_semantic_index_note_id",),
    )
    assert indexes


def test_rebuilt_database_passes_integrity_checks(legacy_db):
    path = legacy_db(LEGACY_NO_CASCADE)
    assert raw_query(path, "PRAGMA integrity_check")[0][0] == "ok"
    assert raw_query(path, "PRAGMA foreign_key_check") == []


def test_rebuild_is_idempotent(legacy_db):
    path = legacy_db(
        LEGACY_NO_CASCADE,
        [
            ("INSERT INTO notes (created_at, text) VALUES (?, ?)", ("2020-01-01T00:00:00+00:00", "kept")),
            ("INSERT INTO semantic_index (note_id, chunk, vector) VALUES (?, ?, ?)", (1, "kept chunk", b"\x01")),
        ],
    )
    memory_db.init_db()
    memory_db.init_db()
    assert raw_query(path, "SELECT COUNT(*) FROM semantic_index")[0][0] == 1

    conn = memory_db.get_connection()
    try:
        assert memory_db._migrate_semantic_index_fk(conn) is False
    finally:
        conn.close()


def test_cascade_works_after_migration(legacy_db):
    legacy_db(
        LEGACY_NO_CASCADE,
        [
            ("INSERT INTO notes (created_at, text) VALUES (?, ?)", ("2020-01-01T00:00:00+00:00", "kept")),
            ("INSERT INTO semantic_index (note_id, chunk, vector) VALUES (?, ?, ?)", (1, "kept chunk", b"\x01")),
        ],
    )
    conn = memory_db.get_connection()
    try:
        conn.execute("DELETE FROM notes WHERE id = 1")
        conn.commit()
    finally:
        conn.close()
    assert semantic.get_note_chunks(1) == []
