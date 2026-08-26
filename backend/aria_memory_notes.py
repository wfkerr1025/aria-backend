"""ARIA Lite Phase 3 - Notes API (keyword only).

Create, fetch and keyword-search notes stored in the memory database.
Semantic search and embeddings live elsewhere; this module never touches them.
"""

from datetime import datetime, timezone

try:
    from backend.aria_memory_db import get_connection
except ImportError:  # running from inside the backend directory
    from aria_memory_db import get_connection

# LIKE wildcards are escaped with '!' rather than a backslash so the SQL and
# the Python patterns stay readable.
LIKE_ESCAPE = "!"


def _utc_now() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _encode_tags(tags):
    """Join a tag list into a comma-separated string, or None when empty."""
    if not tags:
        return None
    cleaned = [str(tag).strip() for tag in tags if str(tag).strip()]
    return ",".join(cleaned) if cleaned else None


def _escape_like(value: str) -> str:
    """Neutralize LIKE wildcards so a query for '100%' is not a prefix match."""
    out = value.replace(LIKE_ESCAPE, LIKE_ESCAPE + LIKE_ESCAPE)
    out = out.replace("%", LIKE_ESCAPE + "%")
    return out.replace("_", LIKE_ESCAPE + "_")


def save_note(text: str, tags: list[str] | None = None, source: str = "chat") -> int:
    """Insert a note and return its new id."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO notes (created_at, text, tags, source) VALUES (?, ?, ?, ?)",
            (_utc_now(), text, _encode_tags(tags), source),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def get_note(note_id: int) -> dict | None:
    """Return one note as a dict, or None when the id does not exist."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, created_at, text, tags, source FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def delete_note(note_id: int) -> dict:
    """Delete a note and everything that hangs off it.

    Returns {"deleted", "embeddings_deleted", "chunks_deleted"}, with
    deleted False when the note did not exist.

    semantic_index rows go by ON DELETE CASCADE, but the embeddings
    table has no cascade -- with foreign keys enforced, deleting a note
    that still has vectors would raise IntegrityError. They are removed
    explicitly, in the same transaction as the note, so a note and its
    vectors can never end up half-deleted.
    """
    conn = get_connection()
    try:
        exists = conn.execute(
            "SELECT 1 FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        if exists is None:
            return {"deleted": False, "embeddings_deleted": 0, "chunks_deleted": 0}

        chunks = conn.execute(
            "SELECT COUNT(*) AS n FROM semantic_index WHERE note_id = ?", (note_id,)
        ).fetchone()["n"]
        embeddings = conn.execute(
            "DELETE FROM embeddings WHERE note_id = ?", (note_id,)
        ).rowcount
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        conn.commit()
        return {
            "deleted": True,
            "embeddings_deleted": embeddings,
            "chunks_deleted": chunks,
        }
    finally:
        conn.close()


def list_notes(limit: int = 20, offset: int = 0) -> list[dict]:
    """Return stored notes, newest first.

    offset pages through the same ordering, so a caller walking a long
    store sees each note once. Ordering ties break on id, which keeps
    paging stable when several notes share a created_at.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, created_at, text, tags, source
            FROM notes
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def search_notes_keyword(query: str, limit: int = 20) -> list[dict]:
    """Return notes whose text or tags contain `query`, newest first."""
    pattern = "%" + _escape_like(query) + "%"
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, created_at, text, tags, source
            FROM notes
            WHERE text LIKE ? ESCAPE '!'
               OR tags LIKE ? ESCAPE '!'
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (pattern, pattern, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
