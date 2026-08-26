# backend/tests/test_phase3_tools.py
#
# Test suite for the six Phase 3 tool wrappers, exercised through the real
# dispatcher (backend/core/tool_registry.py's execute_tool), not by calling
# the wrapper functions directly:
#
#   backend/tool_save_note.py      save_note
#   backend/tool_search_notes.py   search_notes
#   backend/tool_set_context.py    set_context
#   backend/tool_get_context.py    get_context
#   backend/tool_set_self.py       set_self
#   backend/tool_get_self.py       get_self
#
# Real sqlite3 throughout -- nothing is mocked. Isolation comes from
# redirecting aria_memory_db.DB_PATH at a tmp_path file, which reaches every
# module because they all call get_connection(), and get_connection() reads
# DB_PATH at call time. The project's real backend/aria_memory.db is never
# opened here.
#
# Note that execute_tool() runs each handler inside run_in_sandbox(), i.e. on
# a worker thread -- so these tests also cover the wrappers opening, using and
# closing their sqlite connections off the main thread.
#
#   python -m pytest backend/tests/test_phase3_tools.py

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import aria_memory_context as context_store
from backend import aria_memory_db
from backend import aria_memory_embeddings as embeddings_store
from backend import aria_memory_notes as notes_store
from backend import aria_memory_self as self_store
from backend.core import tool_registry
from backend.core.errors import ErrorCode
from backend.core.tool_registry import (
    PERMISSION_FILESYSTEM,
    PERMISSION_SAFE,
    execute_tool,
    get_tool_schema,
)

MEMORY_TOOL_NAMES = [
    "save_note",
    "search_notes",
    "set_context",
    "get_context",
    "set_self",
    "get_self",
]

# One valid invocation per tool, for the checks that must cover all six.
TOOL_INVOCATIONS = [
    pytest.param("save_note", {"text": "cleanup note", "tags": ["x"]}, id="save_note"),
    pytest.param("search_notes", {"query": "cleanup"}, id="search_notes"),
    pytest.param("set_context", {"key": "k", "value": "v"}, id="set_context"),
    pytest.param("get_context", {"key": "k"}, id="get_context"),
    pytest.param("set_self", {"key": "k", "value": "v"}, id="set_self"),
    pytest.param("get_self", {"key": "k"}, id="get_self"),
]


# ======================================================
# Fixtures and helpers
# ======================================================
@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """A throwaway database file, with the Phase 3 schema already created."""
    tmp_db = tmp_path / "test_memory.db"
    monkeypatch.setattr(aria_memory_db, "DB_PATH", str(tmp_db))
    aria_memory_db.init_db()
    return tmp_db


def raw_query(db_path, sql, params=()):
    """Read the database over a connection this test file owns, so a leaked
    or reused module connection can never mask a missing write."""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def is_closed(conn):
    """True when a real sqlite3.Connection has already been closed."""
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


class ConnectionRecorder:
    """Hands out real connections and remembers them, so a test can assert
    afterwards that the tool closed every one it opened."""

    def __init__(self):
        self.connections = []

    def __call__(self):
        conn = aria_memory_db.get_connection()
        self.connections.append(conn)
        return conn

    @property
    def all_closed(self):
        return all(is_closed(conn) for conn in self.connections)


@pytest.fixture
def recorder(monkeypatch):
    """Wrap get_connection in every module the wrappers reach through."""
    rec = ConnectionRecorder()
    for module in (notes_store, embeddings_store, context_store, self_store):
        monkeypatch.setattr(module, "get_connection", rec)
    return rec


def call(name, args, allowed_permissions=None):
    """Run a tool through the dispatcher and return its ToolResult."""
    return execute_tool(name, args, allowed_permissions=allowed_permissions)


def value_of(name, args, allowed_permissions=None):
    """Run a tool through the dispatcher and return its value, failing loudly
    if the dispatcher rejected the call."""
    result = call(name, args, allowed_permissions=allowed_permissions)
    assert result.ok, f"{name} failed: {result.error_code}: {result.error}"
    return result.value


def assert_iso_utc(value):
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
    return parsed


# ======================================================
# Registration
# ======================================================
def test_all_six_tools_are_registered():
    registered = [tool["name"] for tool in tool_registry.list_tools()]
    for name in MEMORY_TOOL_NAMES:
        assert name in registered


@pytest.mark.parametrize("name", MEMORY_TOOL_NAMES)
def test_memory_tools_require_filesystem_permission(name):
    assert get_tool_schema(name).permission == PERMISSION_FILESYSTEM


def test_registering_memory_tools_left_existing_tools_alone():
    registered = [tool["name"] for tool in tool_registry.list_tools()]
    assert "weather" in registered
    assert "web_search" in registered


# ======================================================
# tool_save_note
# ======================================================
def test_save_note_returns_expected_shape(tmp_db):
    value = value_of("save_note", {"text": "hello world", "tags": ["x"], "source": "chat"})
    assert sorted(value) == ["note_id", "status"]
    assert isinstance(value["note_id"], int)
    assert value["status"] == "saved"


def test_save_note_writes_the_row(tmp_db):
    value = value_of("save_note", {"text": "hello world", "tags": ["x"], "source": "chat"})
    rows = raw_query(
        tmp_db,
        "SELECT id, created_at, text, tags, source FROM notes WHERE id = ?",
        (value["note_id"],),
    )
    assert len(rows) == 1
    note_id, created_at, text, tags, source = rows[0]
    assert note_id == value["note_id"]
    assert text == "hello world"
    assert tags == "x"
    assert source == "chat"
    assert_iso_utc(created_at)


def test_save_note_stores_multiple_tags_comma_separated(tmp_db):
    value = value_of("save_note", {"text": "tagged", "tags": ["unity", "build", "ci"]})
    rows = raw_query(tmp_db, "SELECT tags FROM notes WHERE id = ?", (value["note_id"],))
    assert rows[0][0] == "unity,build,ci"


def test_save_note_defaults_source_to_chat(tmp_db):
    value = value_of("save_note", {"text": "no source supplied"})
    rows = raw_query(tmp_db, "SELECT source, tags FROM notes WHERE id = ?", (value["note_id"],))
    assert rows[0] == ("chat", None)


def test_save_note_ids_increase(tmp_db):
    first = value_of("save_note", {"text": "first"})["note_id"]
    second = value_of("save_note", {"text": "second"})["note_id"]
    assert second > first


def test_save_note_missing_text_is_rejected(tmp_db):
    result = call("save_note", {})
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_INVALID_ARGS
    assert "text" in result.error
    assert raw_query(tmp_db, "SELECT COUNT(*) FROM notes")[0][0] == 0


def test_save_note_rejects_non_string_text(tmp_db):
    result = call("save_note", {"text": 123})
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_INVALID_ARGS
    assert raw_query(tmp_db, "SELECT COUNT(*) FROM notes")[0][0] == 0


def test_save_note_rejects_non_array_tags(tmp_db):
    result = call("save_note", {"text": "bad tags", "tags": "not-a-list"})
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_INVALID_ARGS
    assert raw_query(tmp_db, "SELECT COUNT(*) FROM notes")[0][0] == 0


# ======================================================
# tool_search_notes
# ======================================================
def test_search_notes_returns_expected_shape(tmp_db):
    value_of("save_note", {"text": "unity shader notes", "tags": ["unity"]})
    value = value_of("search_notes", {"query": "shader"})
    assert sorted(value) == ["count", "results"]
    assert isinstance(value["results"], list)
    assert isinstance(value["count"], int)


def test_search_notes_finds_the_right_note(tmp_db):
    wanted = value_of("save_note", {"text": "unity shader notes", "tags": ["unity"]})["note_id"]
    value_of("save_note", {"text": "completely unrelated", "tags": ["other"]})

    value = value_of("search_notes", {"query": "shader"})
    assert value["count"] == 1
    assert len(value["results"]) == 1
    row = value["results"][0]
    assert isinstance(row, dict)
    assert sorted(row) == ["created_at", "id", "source", "tags", "text"]
    assert row["id"] == wanted
    assert row["text"] == "unity shader notes"


def test_search_notes_count_matches_result_length(tmp_db):
    for n in range(3):
        value_of("save_note", {"text": f"matching note {n}"})
    value = value_of("search_notes", {"query": "matching"})
    assert value["count"] == len(value["results"]) == 3


def test_search_notes_matches_tags(tmp_db):
    wanted = value_of("save_note", {"text": "body says nothing", "tags": ["telemetry"]})["note_id"]
    value_of("save_note", {"text": "body says nothing either", "tags": ["unrelated"]})
    value = value_of("search_notes", {"query": "telemetry"})
    assert [row["id"] for row in value["results"]] == [wanted]


def test_search_notes_honours_the_limit(tmp_db):
    for n in range(5):
        value_of("save_note", {"text": f"limited note {n}"})
    limited = value_of("search_notes", {"query": "limited", "limit": 2})
    assert limited["count"] == 2
    assert len(limited["results"]) == 2
    assert value_of("search_notes", {"query": "limited"})["count"] == 5


def test_search_notes_with_no_matches(tmp_db):
    value_of("save_note", {"text": "something entirely different"})
    value = value_of("search_notes", {"query": "absent"})
    assert value == {"results": [], "count": 0}


def test_search_notes_rejects_non_integer_limit(tmp_db):
    result = call("search_notes", {"query": "x", "limit": "not-an-int"})
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_INVALID_ARGS
    assert "limit" in result.error


def test_search_notes_missing_query_is_rejected(tmp_db):
    result = call("search_notes", {})
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_INVALID_ARGS
    assert "query" in result.error


# ======================================================
# tool_set_context
# ======================================================
def test_set_context_returns_expected_shape(tmp_db):
    value = value_of("set_context", {"key": "theme", "value": "dark"})
    assert value == {"key": "theme", "value": "dark", "status": "updated"}


def test_set_context_writes_the_row(tmp_db):
    value_of("set_context", {"key": "theme", "value": "dark"})
    rows = raw_query(tmp_db, "SELECT key, value, updated_at FROM user_context")
    assert len(rows) == 1
    key, value, updated_at = rows[0]
    assert key == "theme"
    assert value == "dark"
    assert_iso_utc(updated_at)


def test_set_context_overwrites_without_duplicating(tmp_db):
    value_of("set_context", {"key": "theme", "value": "dark"})
    before = raw_query(tmp_db, "SELECT updated_at FROM user_context WHERE key='theme'")[0][0]

    value = value_of("set_context", {"key": "theme", "value": "light"})
    assert value == {"key": "theme", "value": "light", "status": "updated"}

    rows = raw_query(tmp_db, "SELECT value, updated_at FROM user_context WHERE key='theme'")
    assert len(rows) == 1
    assert rows[0][0] == "light"
    assert assert_iso_utc(rows[0][1]) >= assert_iso_utc(before)


def test_set_context_leaves_other_keys_alone(tmp_db):
    value_of("set_context", {"key": "a", "value": "1"})
    value_of("set_context", {"key": "b", "value": "2"})
    value_of("set_context", {"key": "a", "value": "changed"})
    rows = dict(raw_query(tmp_db, "SELECT key, value FROM user_context"))
    assert rows == {"a": "changed", "b": "2"}


def test_set_context_rejects_non_string_value(tmp_db):
    result = call("set_context", {"key": "theme", "value": 123})
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_INVALID_ARGS
    assert raw_query(tmp_db, "SELECT COUNT(*) FROM user_context")[0][0] == 0


def test_set_context_missing_value_is_rejected(tmp_db):
    result = call("set_context", {"key": "theme"})
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_INVALID_ARGS
    assert raw_query(tmp_db, "SELECT COUNT(*) FROM user_context")[0][0] == 0


# ======================================================
# tool_get_context
# ======================================================
def test_get_context_returns_existing_value(tmp_db):
    value_of("set_context", {"key": "theme", "value": "dark"})
    value = value_of("get_context", {"key": "theme"})
    assert value == {"key": "theme", "value": "dark"}


def test_get_context_missing_key_returns_none(tmp_db):
    value = value_of("get_context", {"key": "never.set"})
    assert value == {"key": "never.set", "value": None}


def test_get_context_reflects_an_update(tmp_db):
    value_of("set_context", {"key": "theme", "value": "dark"})
    value_of("set_context", {"key": "theme", "value": "light"})
    assert value_of("get_context", {"key": "theme"})["value"] == "light"


def test_get_context_does_not_read_the_self_store(tmp_db):
    value_of("set_self", {"key": "shared", "value": "from-self"})
    assert value_of("get_context", {"key": "shared"})["value"] is None


def test_get_context_missing_key_argument_is_rejected(tmp_db):
    result = call("get_context", {})
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_INVALID_ARGS


# ======================================================
# tool_set_self
# ======================================================
def test_set_self_returns_expected_shape(tmp_db):
    value = value_of("set_self", {"key": "version", "value": "1.0"})
    assert value == {"key": "version", "value": "1.0", "status": "updated"}


def test_set_self_writes_the_row(tmp_db):
    value_of("set_self", {"key": "version", "value": "1.0"})
    rows = raw_query(tmp_db, "SELECT key, value, updated_at FROM aria_self")
    assert len(rows) == 1
    key, value, updated_at = rows[0]
    assert key == "version"
    assert value == "1.0"
    assert_iso_utc(updated_at)


def test_set_self_overwrites_without_duplicating(tmp_db):
    value_of("set_self", {"key": "version", "value": "1.0"})
    value = value_of("set_self", {"key": "version", "value": "2.0"})
    assert value == {"key": "version", "value": "2.0", "status": "updated"}
    rows = raw_query(tmp_db, "SELECT value FROM aria_self WHERE key='version'")
    assert len(rows) == 1
    assert rows[0][0] == "2.0"


def test_set_self_does_not_write_to_the_context_store(tmp_db):
    value_of("set_self", {"key": "version", "value": "1.0"})
    assert raw_query(tmp_db, "SELECT COUNT(*) FROM user_context")[0][0] == 0
    assert raw_query(tmp_db, "SELECT COUNT(*) FROM aria_self")[0][0] == 1


def test_set_self_rejects_non_string_value(tmp_db):
    result = call("set_self", {"key": "version", "value": 1.0})
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_INVALID_ARGS
    assert raw_query(tmp_db, "SELECT COUNT(*) FROM aria_self")[0][0] == 0


# ======================================================
# tool_get_self
# ======================================================
def test_get_self_returns_existing_value(tmp_db):
    value_of("set_self", {"key": "version", "value": "1.0"})
    value = value_of("get_self", {"key": "version"})
    assert value == {"key": "version", "value": "1.0"}


def test_get_self_missing_key_returns_none(tmp_db):
    value = value_of("get_self", {"key": "never.set"})
    assert value == {"key": "never.set", "value": None}


def test_get_self_reflects_an_update(tmp_db):
    value_of("set_self", {"key": "version", "value": "1.0"})
    value_of("set_self", {"key": "version", "value": "2.0"})
    assert value_of("get_self", {"key": "version"})["value"] == "2.0"


def test_get_self_does_not_read_the_context_store(tmp_db):
    value_of("set_context", {"key": "shared", "value": "from-context"})
    assert value_of("get_self", {"key": "shared"})["value"] is None


def test_get_self_missing_key_argument_is_rejected(tmp_db):
    result = call("get_self", {})
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_INVALID_ARGS


# ======================================================
# Permission gating
# ======================================================
@pytest.mark.parametrize("name, args", TOOL_INVOCATIONS)
def test_safe_only_callers_are_denied(tmp_db, name, args):
    result = call(name, args, allowed_permissions={PERMISSION_SAFE})
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_PERMISSION_DENIED
    assert PERMISSION_FILESYSTEM in result.error


@pytest.mark.parametrize("name, args", TOOL_INVOCATIONS)
def test_filesystem_permission_allows_the_call(tmp_db, name, args):
    result = call(name, args, allowed_permissions={PERMISSION_FILESYSTEM})
    assert result.ok, f"{name} denied: {result.error_code}: {result.error}"


@pytest.mark.parametrize("name, args", TOOL_INVOCATIONS)
def test_empty_permission_set_is_denied(tmp_db, name, args):
    result = call(name, args, allowed_permissions=set())
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_PERMISSION_DENIED


def test_denied_write_never_reaches_the_database(tmp_db):
    denied = call(
        "save_note",
        {"text": "must not be stored"},
        allowed_permissions={PERMISSION_SAFE},
    )
    assert not denied.ok
    assert raw_query(tmp_db, "SELECT COUNT(*) FROM notes")[0][0] == 0

    denied = call(
        "set_context",
        {"key": "k", "value": "must not be stored"},
        allowed_permissions={PERMISSION_SAFE},
    )
    assert not denied.ok
    assert raw_query(tmp_db, "SELECT COUNT(*) FROM user_context")[0][0] == 0


# ======================================================
# Connection cleanup
# ======================================================
@pytest.mark.parametrize("name, args", TOOL_INVOCATIONS)
def test_every_tool_closes_its_connections(tmp_db, recorder, name, args):
    result = call(name, args)
    assert result.ok, f"{name} failed: {result.error_code}: {result.error}"
    assert recorder.connections, f"{name} opened no connection"
    assert recorder.all_closed


@pytest.mark.parametrize("name, args", TOOL_INVOCATIONS)
def test_every_tool_leaves_no_open_file_handle(tmp_db, name, args):
    assert call(name, args).ok
    # Windows refuses to unlink a file that still has an open handle, so a
    # successful remove here proves nothing leaked. On POSIX this is a weaker
    # check, but it never gives a false pass.
    os.remove(tmp_db)
    assert not Path(tmp_db).exists()


def test_a_full_tool_sequence_leaves_no_open_handles(tmp_db, recorder):
    value_of("save_note", {"text": "sequence", "tags": ["seq"]})
    value_of("search_notes", {"query": "sequence"})
    value_of("set_context", {"key": "k", "value": "v"})
    value_of("get_context", {"key": "k"})
    value_of("set_self", {"key": "k", "value": "v"})
    value_of("get_self", {"key": "k"})

    assert len(recorder.connections) >= 6
    assert recorder.all_closed
    os.remove(tmp_db)
    assert not Path(tmp_db).exists()


def test_connections_are_closed_when_the_handler_raises(tmp_db, recorder, monkeypatch):
    # Force a failure inside the handler, after the connection is open, to
    # prove cleanup is in a finally block rather than on the happy path only.
    real_encode_tags = notes_store._encode_tags

    def boom(tags):
        real_encode_tags(tags)
        raise RuntimeError("induced failure")

    monkeypatch.setattr(notes_store, "_encode_tags", boom)
    result = call("save_note", {"text": "will fail"})
    assert not result.ok
    assert recorder.all_closed


# ======================================================
# Isolation
# ======================================================
def test_tools_never_touch_the_real_database(tmp_db):
    real_db = Path(aria_memory_db.__file__).resolve().parent / "aria_memory.db"
    assert Path(aria_memory_db.DB_PATH) != real_db
    value_of("save_note", {"text": "isolation check"})
    assert raw_query(tmp_db, "SELECT COUNT(*) FROM notes")[0][0] == 1
