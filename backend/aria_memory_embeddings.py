"""ARIA Lite Phase 3 - Embedding storage.

Raw vector blobs for notes. Storage only: encoding the vectors and searching
them are handled by the layers above this one.

Each blob is tagged with the encoder that produced it (model) and its width
(dim). Vectors from different encoders are not comparable -- a lexical
256-value vector and a semantic 384-value one are different spaces, not
different opinions -- so readers filter on model rather than assuming every
row in the table belongs to the same space.
"""

import sqlite3

try:
    from backend.aria_memory_db import get_connection
except ImportError:  # running from inside the backend directory
    from aria_memory_db import get_connection


def store_embedding(
    note_id: int,
    vector_bytes: bytes,
    model: str | None = None,
    dim: int | None = None,
) -> int:
    """Attach a vector blob to a note and return the new embedding id.

    model/dim record which encoder produced the blob. They default to NULL
    for a caller passing raw bytes it encoded itself; anything going through
    aria_memory_note_embeddings supplies them.
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO embeddings (note_id, vector, model, dim) VALUES (?, ?, ?, ?)",
            (note_id, sqlite3.Binary(vector_bytes), model, dim),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def get_embeddings_for_note(note_id: int, model: str | None = None) -> list[bytes]:
    """Return stored vectors for a note, oldest first.

    model=None returns everything attached to the note, whatever encoded it.
    Pass a model to get only the vectors that are comparable with it.
    """
    conn = get_connection()
    try:
        if model is None:
            rows = conn.execute(
                "SELECT vector FROM embeddings WHERE note_id = ? ORDER BY id",
                (note_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT vector FROM embeddings WHERE note_id = ? AND model = ? ORDER BY id",
                (note_id, model),
            ).fetchall()
        return [bytes(row["vector"]) for row in rows]
    finally:
        conn.close()
