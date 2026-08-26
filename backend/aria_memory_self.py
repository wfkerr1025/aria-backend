"""ARIA Lite Phase 3 - Self-knowledge store.

What ARIA knows about itself: capabilities, preferences, operating state.
Same key/value shape as the user context store, kept in the aria_self table.

Every key is versioned. A key starts at version 1 and each overwrite
increments it, with the value being replaced copied into aria_self_history
first. That makes a self-description auditable -- what ARIA believed about
itself last week is still readable -- and makes a bad write reversible via
rollback_self(). Only changes are versioned: rewriting a key with the value
it already holds is a no-op, so the version number counts real revisions
rather than the number of times something was saved.
"""

from datetime import datetime, timezone

try:
    from backend.aria_memory_db import get_connection
except ImportError:  # running from inside the backend directory
    from aria_memory_db import get_connection


def _utc_now() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def set_self(key: str, value: str) -> int:
    """Create or overwrite one self-knowledge entry.

    Returns the version now in force. Writing the value a key already
    holds leaves the version untouched and records no history.
    """
    now = _utc_now()
    conn = get_connection()
    try:
        current = conn.execute(
            "SELECT value, version, updated_at FROM aria_self WHERE key = ?",
            (key,),
        ).fetchone()

        if current is None:
            conn.execute(
                """
                INSERT INTO aria_self (key, value, updated_at, version)
                VALUES (?, ?, ?, 1)
                """,
                (key, value, now),
            )
            conn.commit()
            return 1

        if current["value"] == value:
            return int(current["version"])

        version = int(current["version"]) + 1
        conn.execute(
            """
            INSERT INTO aria_self_history
                (key, value, version, updated_at, superseded_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (key, current["value"], current["version"], current["updated_at"], now),
        )
        conn.execute(
            "UPDATE aria_self SET value = ?, updated_at = ?, version = ? WHERE key = ?",
            (value, now, version, key),
        )
        conn.commit()
        return version
    finally:
        conn.close()


def get_self(key: str) -> str | None:
    """Return one self-knowledge value, or None when the key is unset."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM aria_self WHERE key = ?",
            (key,),
        ).fetchone()
        return row["value"] if row is not None else None
    finally:
        conn.close()


def get_self_version(key: str) -> int | None:
    """Return the version currently in force, or None when unset."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT version FROM aria_self WHERE key = ?",
            (key,),
        ).fetchone()
        return int(row["version"]) if row is not None else None
    finally:
        conn.close()


def get_self_history(key: str) -> list[dict]:
    """Return superseded revisions of a key, oldest version first.

    The value in force is not included -- get_self() already returns it,
    and history is what a key used to say.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT key, value, version, updated_at, superseded_at
            FROM aria_self_history
            WHERE key = ?
            ORDER BY version, id
            """,
            (key,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def rollback_self(key: str, version: int) -> int | None:
    """Restore a superseded revision, as a new version.

    Returns the new version number, or None when that revision does not
    exist. The rollback moves history forward rather than rewriting it:
    the value being replaced is archived like any other overwrite, so an
    undone rollback is itself recoverable.
    """
    conn = get_connection()
    try:
        target = conn.execute(
            "SELECT value FROM aria_self_history WHERE key = ? AND version = ?",
            (key, version),
        ).fetchone()
    finally:
        conn.close()

    if target is None:
        return None
    return set_self(key, target["value"])


def list_self() -> dict:
    """Return every self-knowledge entry as a key -> value mapping, sorted by key."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT key, value FROM aria_self ORDER BY key"
        ).fetchall()
        return {row["key"]: row["value"] for row in rows}
    finally:
        conn.close()
