# backend/tests/test_upgrade_06_self_versioning.py
#
# Upgrade 6: versioned self-knowledge -- a version column on aria_self, an
# aria_self_history archive, rollback, and the migration for a database
# without the column.

from __future__ import annotations

from phase3_upgrade_helpers import (  # noqa: F401
    ConnectionRecorder,
    assert_iso_utc,
    columns_of,
    db,
    legacy_db,
    raw_query,
)

from backend import aria_memory_self as self_store

LEGACY_SCHEMA = """
CREATE TABLE aria_self (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


# ---------------- schema ----------------
def test_aria_self_has_a_version_column(db):
    assert "version" in columns_of(db, "aria_self")


def test_history_table_exists_with_its_columns(db):
    assert columns_of(db, "aria_self_history") == [
        "id", "key", "value", "version", "updated_at", "superseded_at",
    ]


# ---------------- versioning ----------------
def test_a_new_key_starts_at_version_one(db):
    assert self_store.set_self("identity.name", "ARIA") == 1
    assert self_store.get_self_version("identity.name") == 1


def test_each_change_increments_the_version(db):
    self_store.set_self("key", "v1")
    assert self_store.set_self("key", "v2") == 2
    assert self_store.set_self("key", "v3") == 3
    assert self_store.get_self_version("key") == 3


def test_rewriting_the_same_value_is_a_no_op(db):
    self_store.set_self("key", "stable")
    assert self_store.set_self("key", "stable") == 1
    assert self_store.get_self_version("key") == 1
    assert self_store.get_self_history("key") == []


def test_version_is_per_key(db):
    self_store.set_self("a", "1")
    self_store.set_self("a", "2")
    self_store.set_self("b", "1")
    assert self_store.get_self_version("a") == 2
    assert self_store.get_self_version("b") == 1


def test_get_self_version_is_none_when_unset(db):
    assert self_store.get_self_version("never.set") is None


def test_get_self_still_returns_the_current_value(db):
    self_store.set_self("key", "old")
    self_store.set_self("key", "new")
    assert self_store.get_self("key") == "new"


def test_only_one_live_row_per_key(db):
    for value in ("v1", "v2", "v3"):
        self_store.set_self("key", value)
    assert raw_query(db, "SELECT COUNT(*) FROM aria_self")[0][0] == 1


# ---------------- history ----------------
def test_history_records_superseded_values(db):
    self_store.set_self("key", "v1")
    self_store.set_self("key", "v2")
    self_store.set_self("key", "v3")
    history = self_store.get_self_history("key")
    assert [row["value"] for row in history] == ["v1", "v2"]
    assert [row["version"] for row in history] == [1, 2]


def test_history_excludes_the_current_value(db):
    self_store.set_self("key", "v1")
    self_store.set_self("key", "v2")
    assert all(row["value"] != "v2" for row in self_store.get_self_history("key"))


def test_history_is_empty_for_an_unrevised_key(db):
    self_store.set_self("key", "only")
    assert self_store.get_self_history("key") == []


def test_history_is_empty_for_an_unknown_key(db):
    assert self_store.get_self_history("never.set") == []


def test_history_keeps_the_original_write_time(db):
    self_store.set_self("key", "v1")
    original = raw_query(db, "SELECT updated_at FROM aria_self WHERE key='key'")[0][0]
    self_store.set_self("key", "v2")
    archived = self_store.get_self_history("key")[0]
    assert archived["updated_at"] == original
    assert_iso_utc(archived["superseded_at"])


def test_history_is_per_key(db):
    self_store.set_self("a", "1")
    self_store.set_self("a", "2")
    self_store.set_self("b", "1")
    assert self_store.get_self_history("b") == []
    assert len(self_store.get_self_history("a")) == 1


def test_history_is_ordered_by_version(db):
    for value in ("v1", "v2", "v3", "v4"):
        self_store.set_self("key", value)
    versions = [row["version"] for row in self_store.get_self_history("key")]
    assert versions == sorted(versions)


# ---------------- rollback ----------------
def test_rollback_restores_an_older_value(db):
    self_store.set_self("key", "v1")
    self_store.set_self("key", "v2")
    self_store.rollback_self("key", 1)
    assert self_store.get_self("key") == "v1"


def test_rollback_moves_the_version_forward(db):
    self_store.set_self("key", "v1")
    self_store.set_self("key", "v2")
    assert self_store.rollback_self("key", 1) == 3
    assert self_store.get_self_version("key") == 3


def test_rollback_archives_the_value_it_replaced(db):
    self_store.set_self("key", "v1")
    self_store.set_self("key", "v2")
    self_store.rollback_self("key", 1)
    assert [row["value"] for row in self_store.get_self_history("key")] == ["v1", "v2"]


def test_rollback_is_itself_reversible(db):
    self_store.set_self("key", "v1")
    self_store.set_self("key", "v2")
    self_store.rollback_self("key", 1)
    self_store.rollback_self("key", 2)
    assert self_store.get_self("key") == "v2"


def test_rollback_to_an_unknown_version_returns_none(db):
    self_store.set_self("key", "v1")
    self_store.set_self("key", "v2")
    assert self_store.rollback_self("key", 99) is None
    assert self_store.get_self("key") == "v2"


def test_rollback_on_an_unknown_key_returns_none(db):
    assert self_store.rollback_self("never.set", 1) is None


# ---------------- migration ----------------
def test_legacy_database_gains_the_version_column(legacy_db):
    path = legacy_db(LEGACY_SCHEMA)
    assert "version" in columns_of(path, "aria_self")


def test_legacy_rows_default_to_version_one(legacy_db):
    legacy_db(
        LEGACY_SCHEMA,
        [("INSERT INTO aria_self VALUES (?, ?, ?)", ("old", "kept", "2020-01-01T00:00:00+00:00"))],
    )
    assert self_store.get_self("old") == "kept"
    assert self_store.get_self_version("old") == 1


def test_legacy_rows_version_forward_normally(legacy_db):
    legacy_db(
        LEGACY_SCHEMA,
        [("INSERT INTO aria_self VALUES (?, ?, ?)", ("old", "kept", "2020-01-01T00:00:00+00:00"))],
    )
    assert self_store.set_self("old", "changed") == 2
    assert [row["value"] for row in self_store.get_self_history("old")] == ["kept"]


# ---------------- cleanup ----------------
def test_connections_are_closed(db, monkeypatch):
    recorder = ConnectionRecorder()
    monkeypatch.setattr(self_store, "get_connection", recorder)
    self_store.set_self("key", "v1")
    self_store.set_self("key", "v2")
    self_store.get_self("key")
    self_store.get_self_version("key")
    self_store.get_self_history("key")
    self_store.rollback_self("key", 1)
    self_store.list_self()
    assert recorder.connections
    assert recorder.all_closed
