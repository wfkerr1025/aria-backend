# backend/tests/test_upgrade_05_context_expiration.py
#
# Upgrade 5: TTLs on user_context -- expires_at, lazy filtering on read,
# purge_expired(), and the migration for a database without the column.

from __future__ import annotations

import time

from phase3_upgrade_helpers import (  # noqa: F401
    ConnectionRecorder,
    assert_iso_utc,
    columns_of,
    db,
    legacy_db,
    raw_query,
)

from backend import aria_memory_context as context_store

LEGACY_SCHEMA = """
CREATE TABLE user_context (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

SHORT_TTL = 0.3


def wait_out(ttl=SHORT_TTL):
    time.sleep(ttl + 0.05)


# ---------------- schema ----------------
def test_user_context_has_expires_at(db):
    assert "expires_at" in columns_of(db, "user_context")


def test_a_write_without_a_ttl_stores_null(db):
    context_store.set_context("permanent", "forever")
    assert raw_query(db, "SELECT expires_at FROM user_context WHERE key='permanent'")[0][0] is None


def test_a_write_with_a_ttl_stores_an_iso_expiry(db):
    context_store.set_context("temporary", "soon", ttl_seconds=60)
    stored = raw_query(db, "SELECT expires_at FROM user_context WHERE key='temporary'")[0][0]
    assert_iso_utc(stored)


def test_expiry_is_later_than_updated_at(db):
    context_store.set_context("temporary", "soon", ttl_seconds=60)
    row = raw_query(db, "SELECT updated_at, expires_at FROM user_context")[0]
    assert assert_iso_utc(row[1]) > assert_iso_utc(row[0])


# ---------------- reads before expiry ----------------
def test_a_live_ttl_entry_reads_normally(db):
    context_store.set_context("temporary", "still here", ttl_seconds=60)
    assert context_store.get_context("temporary") == "still here"
    assert context_store.list_context() == {"temporary": "still here"}


def test_get_context_entry_exposes_the_timestamps(db):
    context_store.set_context("temporary", "value", ttl_seconds=60)
    entry = context_store.get_context_entry("temporary")
    assert sorted(entry) == ["expires_at", "key", "updated_at", "value"]
    assert entry["value"] == "value"


def test_get_context_entry_is_none_for_a_missing_key(db):
    assert context_store.get_context_entry("never.set") is None


# ---------------- reads after expiry ----------------
def test_get_context_hides_an_expired_entry(db):
    context_store.set_context("fleeting", "gone", ttl_seconds=SHORT_TTL)
    wait_out()
    assert context_store.get_context("fleeting") is None


def test_list_context_hides_an_expired_entry(db):
    context_store.set_context("fleeting", "gone", ttl_seconds=SHORT_TTL)
    context_store.set_context("permanent", "stays")
    wait_out()
    assert context_store.list_context() == {"permanent": "stays"}


def test_get_context_entry_hides_an_expired_entry(db):
    context_store.set_context("fleeting", "gone", ttl_seconds=SHORT_TTL)
    wait_out()
    assert context_store.get_context_entry("fleeting") is None


def test_expiry_is_lazy_the_row_survives_until_purged(db):
    context_store.set_context("fleeting", "gone", ttl_seconds=SHORT_TTL)
    wait_out()
    assert context_store.get_context("fleeting") is None
    assert raw_query(db, "SELECT COUNT(*) FROM user_context")[0][0] == 1


def test_a_zero_ttl_expires_immediately(db):
    context_store.set_context("instant", "gone", ttl_seconds=0)
    assert context_store.get_context("instant") is None


def test_a_negative_ttl_expires_immediately(db):
    context_store.set_context("past", "gone", ttl_seconds=-60)
    assert context_store.get_context("past") is None


# ---------------- overwrite semantics ----------------
def test_overwriting_without_a_ttl_clears_the_expiry(db):
    context_store.set_context("key", "first", ttl_seconds=60)
    context_store.set_context("key", "second")
    assert context_store.get_context_entry("key")["expires_at"] is None


def test_overwriting_with_a_ttl_replaces_the_expiry(db):
    context_store.set_context("key", "first", ttl_seconds=1)
    first = raw_query(db, "SELECT expires_at FROM user_context")[0][0]
    context_store.set_context("key", "second", ttl_seconds=600)
    second = raw_query(db, "SELECT expires_at FROM user_context")[0][0]
    assert assert_iso_utc(second) > assert_iso_utc(first)


def test_overwriting_still_keeps_one_row(db):
    context_store.set_context("key", "first", ttl_seconds=60)
    context_store.set_context("key", "second", ttl_seconds=60)
    assert raw_query(db, "SELECT COUNT(*) FROM user_context")[0][0] == 1


def test_rewriting_an_expired_key_revives_it(db):
    context_store.set_context("key", "old", ttl_seconds=SHORT_TTL)
    wait_out()
    context_store.set_context("key", "new")
    assert context_store.get_context("key") == "new"


# ---------------- purge ----------------
def test_purge_removes_only_expired_rows(db):
    context_store.set_context("permanent", "stays")
    context_store.set_context("live", "stays too", ttl_seconds=600)
    context_store.set_context("fleeting", "goes", ttl_seconds=SHORT_TTL)
    wait_out()
    assert context_store.purge_expired() == 1
    assert sorted(context_store.list_context()) == ["live", "permanent"]
    assert raw_query(db, "SELECT COUNT(*) FROM user_context")[0][0] == 2


def test_purge_on_a_clean_store_removes_nothing(db):
    context_store.set_context("permanent", "stays")
    assert context_store.purge_expired() == 0


def test_purge_never_removes_null_expiry_rows(db):
    context_store.set_context("permanent", "stays")
    context_store.purge_expired()
    assert context_store.get_context("permanent") == "stays"


def test_purge_is_repeatable(db):
    context_store.set_context("fleeting", "goes", ttl_seconds=SHORT_TTL)
    wait_out()
    assert context_store.purge_expired() == 1
    assert context_store.purge_expired() == 0


# ---------------- migration ----------------
def test_legacy_database_gains_expires_at(legacy_db):
    path = legacy_db(LEGACY_SCHEMA)
    assert "expires_at" in columns_of(path, "user_context")


def test_legacy_rows_are_readable_and_never_expire(legacy_db):
    legacy_db(
        LEGACY_SCHEMA,
        [("INSERT INTO user_context VALUES (?, ?, ?)", ("old", "kept", "2020-01-01T00:00:00+00:00"))],
    )
    assert context_store.get_context("old") == "kept"
    assert context_store.get_context_entry("old")["expires_at"] is None


def test_migrated_database_accepts_ttls(legacy_db):
    legacy_db(LEGACY_SCHEMA)
    context_store.set_context("fleeting", "goes", ttl_seconds=SHORT_TTL)
    wait_out()
    assert context_store.get_context("fleeting") is None
    assert context_store.purge_expired() == 1


# ---------------- cleanup ----------------
def test_connections_are_closed(db, monkeypatch):
    recorder = ConnectionRecorder()
    monkeypatch.setattr(context_store, "get_connection", recorder)
    context_store.set_context("key", "value", ttl_seconds=1)
    context_store.get_context("key")
    context_store.get_context_entry("key")
    context_store.list_context()
    context_store.purge_expired()
    assert recorder.connections
    assert recorder.all_closed
