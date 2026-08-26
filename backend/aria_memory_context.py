"""ARIA Lite Phase 3 - Personal context store.

Durable key/value facts about the user, kept in the user_context table.
Every write stamps updated_at so staleness is visible later.

An entry may also carry an expiry. Expiry is lazy: a stale row is
filtered out of every read the moment it is past its expires_at, but it
stays on disk until purge_expired() removes it. That keeps reads honest
without making every lookup a write, and it means a caller that wants the
disk actually reclaimed has to ask for it.
"""

from datetime import datetime, timedelta, timezone

try:
    from backend.aria_memory_db import get_connection
except ImportError:  # running from inside the backend directory
    from aria_memory_db import get_connection


def _utc_now() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _expiry_from_ttl(ttl_seconds: float | None) -> str | None:
    """Turn a TTL in seconds into an absolute ISO UTC expiry.

    None means the entry never expires. A non-positive TTL is allowed and
    expires the entry immediately, which is a legitimate way to retire a
    key without deleting it.
    """
    if ttl_seconds is None:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()


# ISO-8601 UTC strings sharing one offset compare correctly as plain text,
# which is what lets the liveness filter live in SQL rather than in Python.
_LIVE = "(expires_at IS NULL OR expires_at > ?)"


def set_context(key: str, value: str, ttl_seconds: float | None = None) -> None:
    """Create or overwrite one context entry.

    ttl_seconds=None (the default) stores an entry that never expires, and
    overwriting an entry that had a TTL clears that TTL -- a write with no
    expiry means exactly what it says.
    """
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO user_context (key, value, updated_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at,
                expires_at = excluded.expires_at
            """,
            (key, value, _utc_now(), _expiry_from_ttl(ttl_seconds)),
        )
        conn.commit()
    finally:
        conn.close()


def get_context(key: str) -> str | None:
    """Return one context value, or None when the key is unset or expired."""
    conn = get_connection()
    try:
        row = conn.execute(
            f"SELECT value FROM user_context WHERE key = ? AND {_LIVE}",
            (key, _utc_now()),
        ).fetchone()
        return row["value"] if row is not None else None
    finally:
        conn.close()


def get_context_entry(key: str) -> dict | None:
    """Return a live entry with its timestamps, or None.

    Use this rather than get_context() when the caller needs to know when
    the value was written or when it will lapse.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            f"""
            SELECT key, value, updated_at, expires_at
            FROM user_context
            WHERE key = ? AND {_LIVE}
            """,
            (key, _utc_now()),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def list_context() -> dict:
    """Return every live context entry as a key -> value mapping.

    Sorted by key. Expired entries are omitted whether or not they have
    been purged from disk yet.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT key, value FROM user_context WHERE {_LIVE} ORDER BY key",
            (_utc_now(),),
        ).fetchall()
        return {row["key"]: row["value"] for row in rows}
    finally:
        conn.close()


def purge_expired() -> int:
    """Delete every entry that is past its expiry. Returns the count."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM user_context "
            "WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (_utc_now(),),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()
