# backend/tests/phase3_upgrade_helpers.py
#
# Shared fixtures and helpers for the per-upgrade Phase 3 test files.
#
# Deliberately NOT named test_*.py so pytest does not collect it. It is
# imported by test_upgrade_01_*.py through
# test_upgrade_10_*.py, which each own one upgrade. Keeping the isolation
# machinery here means every upgrade suite isolates the database the same
# way, and a change to how isolation works is a change in one place.
#
# Real sqlite3 throughout: nothing in these suites is mocked. Isolation is
# a tmp_path DB_PATH, which reaches every module because they all call
# get_connection() and it reads DB_PATH at call time.

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import aria_memory_db as memory_db  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A throwaway database with the current Phase 3 schema."""
    path = tmp_path / "upgrade_test.db"
    monkeypatch.setattr(memory_db, "DB_PATH", path)
    memory_db.init_db()
    return path


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """Build a pre-upgrade database, then hand back a factory that runs the
    migration over it.

    Used by the upgrades that had to migrate an existing database rather
    than only define a new schema. The caller supplies the old CREATE/INSERT
    script, so each suite pins the exact historical shape it cares about.
    """
    path = tmp_path / "legacy.db"

    def build(script: str, rows: list[tuple[str, tuple]] | None = None):
        conn = sqlite3.connect(str(path))
        try:
            conn.executescript(script)
            for sql, params in rows or []:
                conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setattr(memory_db, "DB_PATH", path)
        memory_db.init_db()
        return path

    return build


def raw_query(db_path, sql, params=()):
    """Read the database over a connection the test owns, so a module bug
    can never hide behind the module's own handle."""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def columns_of(db_path, table):
    return [row[1] for row in raw_query(db_path, f"PRAGMA table_info({table})")]


def table_sql(db_path, table):
    rows = raw_query(
        db_path,
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return rows[0][0] if rows else None


def is_closed(conn):
    """True when a real sqlite3.Connection has already been closed."""
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


class ConnectionRecorder:
    """Hands out real connections and remembers them, so a test can assert
    afterwards that every one was closed."""

    def __init__(self):
        self.connections = []

    def __call__(self):
        conn = memory_db.get_connection()
        self.connections.append(conn)
        return conn

    @property
    def all_closed(self):
        return all(is_closed(conn) for conn in self.connections)


def assert_iso_utc(value):
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
    return parsed
