# backend/tests/test_aria_memory_phase3.py
#
# Test suite for the Phase 3 Memory & Knowledge System:
#   backend/aria_memory_db.py          schema + connection factory
#   backend/aria_memory_notes.py       notes CRUD + keyword search
#   backend/aria_memory_embeddings.py  vector blob storage
#   backend/aria_memory_context.py     user_context key/value store
#   backend/aria_memory_self.py        aria_self key/value store
#
# Real sqlite3 throughout -- nothing here is mocked or faked. Isolation
# comes from redirecting aria_memory_db.DB_PATH at a pytest tmp_path file
# before each test, which works for every module because they all call
# get_connection(), and get_connection() reads DB_PATH at call time. The
# project's real backend/aria_memory.db is never opened by these tests.
#
# Unlike the older plain-assert scripts in this directory, this file is
# pytest (as requested) and is named test_*.py so default collection
# finds it:
#
#   python -m pytest backend/tests/test_aria_memory_phase3.py

from __future__ import annotations

import os
import sqlite3
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import aria_memory_context as context_store
from backend import aria_memory_db as memory_db
from backend import aria_memory_embeddings as embeddings_store
from backend import aria_memory_notes as notes_store
from backend import aria_memory_self as self_store

EXPECTED_TABLES = [
    "aria_self",
    "aria_self_history",
    "embeddings",
    "file_chunks",
    "files",
    "notes",
    "semantic_index",
    "user_context",
]

EXPECTED_INDEXES = [
    "idx_aria_self_key",
    "idx_aria_self_history_key",
    "idx_embeddings_note_id",
    "idx_file_chunks_file_id",
    "idx_notes_tags",
    "idx_semantic_index_note_id",
    "idx_user_context_key",
]

EXPECTED_COLUMNS = {
    "notes": ["id", "created_at", "text", "tags", "source"],
    "embeddings": ["id", "note_id", "vector", "model", "dim"],
    "semantic_index": [
        "id", "note_id", "chunk", "vector",
        "chunk_index", "start_word", "end_word", "created_at",
        "model", "dim",
    ],
    "files": [
        "id", "path", "created_at", "last_indexed", "tags",
        "name", "mime", "size_bytes", "updated_at",
    ],
    "file_chunks": [
        "id", "file_id", "chunk", "vector",
        "chunk_index", "start_word", "end_word", "created_at",
        "text", "start_offset", "end_offset", "updated_at", "model", "dim",
        "section",
    ],
    "user_context": ["key", "value", "updated_at", "expires_at"],
    "aria_self": ["key", "value", "updated_at", "version"],
    "aria_self_history": ["id", "key", "value", "version", "updated_at", "superseded_at"],
}


# ======================================================
# Fixtures
# ======================================================
@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Point every Phase 3 module at a throwaway database file."""
    path = tmp_path / "aria_memory_test.db"
    monkeypatch.setattr(memory_db, "DB_PATH", path)
    return path


@pytest.fixture
def db(db_path):
    """A throwaway database with the Phase 3 schema already created."""
    memory_db.init_db()
    return db_path


def raw_query(db_path, sql, params=()):
    """Read the database with a connection this test file owns, not the
    module's, so a module bug can never hide behind the same handle."""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def object_names(db_path, kind):
    rows = raw_query(
        db_path,
        "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'",
        (kind,),
    )
    return sorted(row[0] for row in rows)


def is_closed(conn):
    """True when a real sqlite3.Connection has already been closed."""
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


class ConnectionRecorder:
    """Hands out real connections and remembers them, so a test can assert
    afterwards that the module closed every one it opened."""

    def __init__(self):
        self.connections = []

    def __call__(self):
        conn = memory_db.get_connection()
        self.connections.append(conn)
        return conn

    @property
    def all_closed(self):
        return all(is_closed(conn) for conn in self.connections)


@pytest.fixture
def recorder(monkeypatch):
    """Wrap get_connection in every Phase 3 module with a recorder."""
    rec = ConnectionRecorder()
    for module in (notes_store, embeddings_store, context_store, self_store):
        monkeypatch.setattr(module, "get_connection", rec)
    return rec


# ======================================================
# aria_memory_db -- schema
# ======================================================
def test_init_db_creates_database_file(db_path):
    assert not db_path.exists()
    memory_db.init_db()
    assert db_path.exists()


def test_init_db_creates_every_table(db):
    assert object_names(db, "table") == EXPECTED_TABLES


def test_init_db_creates_every_index(db):
    created = object_names(db, "index")
    for name in EXPECTED_INDEXES:
        assert name in created


@pytest.mark.parametrize("table", sorted(EXPECTED_COLUMNS))
def test_table_columns_match_schema(db, table):
    columns = [row[1] for row in raw_query(db, f"PRAGMA table_info({table})")]
    assert columns == EXPECTED_COLUMNS[table]


def test_notes_id_is_autoincrement(db):
    sql = raw_query(
        db, "SELECT sql FROM sqlite_master WHERE type='table' AND name='notes'"
    )[0][0]
    assert "AUTOINCREMENT" in sql.upper()


def test_init_db_is_idempotent_and_preserves_data(db):
    note_id = notes_store.save_note("survives re-init")
    memory_db.init_db()
    memory_db.init_db()
    assert object_names(db, "table") == EXPECTED_TABLES
    assert notes_store.get_note(note_id)["text"] == "survives re-init"


def test_init_db_on_existing_database_does_not_duplicate_objects(db):
    before = raw_query(db, "SELECT COUNT(*) FROM sqlite_master")[0][0]
    memory_db.init_db()
    after = raw_query(db, "SELECT COUNT(*) FROM sqlite_master")[0][0]
    assert before == after


# ======================================================
# aria_memory_db -- row_factory
# ======================================================
def test_get_connection_sets_row_factory(db):
    conn = memory_db.get_connection()
    try:
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


def test_row_supports_name_index_and_dict_access(db):
    notes_store.save_note("row factory check", ["alpha"], "unit-test")
    conn = memory_db.get_connection()
    try:
        row = conn.execute("SELECT id, text, tags FROM notes").fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row["text"] == "row factory check"   # by column name
        assert row[1] == "row factory check"        # by position
        assert dict(row) == {
            "id": row["id"],
            "text": "row factory check",
            "tags": "alpha",
        }
        assert sorted(row.keys()) == ["id", "tags", "text"]
    finally:
        conn.close()


def test_row_lookup_is_case_insensitive(db):
    notes_store.save_note("case check")
    conn = memory_db.get_connection()
    try:
        row = conn.execute("SELECT text FROM notes").fetchone()
        assert row["TEXT"] == row["text"]
    finally:
        conn.close()


# ======================================================
# aria_memory_db -- connection cleanup
# ======================================================
def test_init_db_closes_its_connection(db_path, monkeypatch):
    opened = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        # Still a real sqlite3 connection -- this only records it.
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(memory_db.sqlite3, "connect", spy)
    memory_db.init_db()
    assert opened, "init_db opened no connection"
    assert all(is_closed(conn) for conn in opened)


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(lambda: notes_store.save_note("cleanup", ["t"]), id="save_note"),
        pytest.param(lambda: notes_store.get_note(1), id="get_note"),
        pytest.param(
            lambda: notes_store.search_notes_keyword("cleanup"), id="search_notes"
        ),
        pytest.param(
            # Foreign keys are enforced, so the vector needs a real note to
            # hang off -- an invented note_id is now an IntegrityError.
            lambda: embeddings_store.store_embedding(
                notes_store.save_note("vector owner"), bytes([0, 1])
            ),
            id="store_embedding",
        ),
        pytest.param(
            lambda: embeddings_store.get_embeddings_for_note(1), id="get_embeddings"
        ),
        pytest.param(lambda: context_store.set_context("k", "v"), id="set_context"),
        pytest.param(lambda: context_store.get_context("k"), id="get_context"),
        pytest.param(lambda: context_store.list_context(), id="list_context"),
        pytest.param(lambda: self_store.set_self("k", "v"), id="set_self"),
        pytest.param(lambda: self_store.get_self("k"), id="get_self"),
        pytest.param(lambda: self_store.list_self(), id="list_self"),
    ],
)
def test_every_operation_closes_its_connection(db, recorder, operation):
    operation()
    assert recorder.connections, "operation opened no connection"
    assert recorder.all_closed


def test_connection_is_closed_even_when_the_query_raises(db, recorder):
    # A dict is not a bindable sqlite3 type, so the INSERT raises inside the
    # try block -- the finally clause still has to close the connection.
    with pytest.raises(sqlite3.Error):
        notes_store.save_note({"not": "a string"})
    assert recorder.connections
    assert recorder.all_closed


def test_no_open_handles_remain_after_operations(db):
    notes_store.save_note("handle check", ["x"])
    context_store.set_context("k", "v")
    self_store.set_self("k", "v")
    # Windows refuses to unlink a file that still has an open handle, so a
    # successful remove here proves nothing leaked. On POSIX this is a
    # weaker check, but it never gives a false pass.
    os.remove(db)
    assert not db.exists()


# ======================================================
# aria_memory_notes -- create / read
# ======================================================
def test_save_note_returns_new_row_id(db):
    first = notes_store.save_note("first")
    second = notes_store.save_note("second")
    assert isinstance(first, int)
    assert second > first


def test_save_note_persists_all_fields(db):
    note_id = notes_store.save_note("hello aria", ["unity", "build"], "voice")
    note = notes_store.get_note(note_id)
    assert note["id"] == note_id
    assert note["text"] == "hello aria"
    assert note["tags"] == "unity,build"
    assert note["source"] == "voice"


def test_save_note_defaults_source_to_chat(db):
    note = notes_store.get_note(notes_store.save_note("no source given"))
    assert note["source"] == "chat"


def test_created_at_is_iso_utc(db):
    note = notes_store.get_note(notes_store.save_note("timestamp check"))
    parsed = datetime.fromisoformat(note["created_at"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
    delta = abs((datetime.now(timezone.utc) - parsed).total_seconds())
    assert delta < 60


@pytest.mark.parametrize(
    "tags, expected",
    [
        (None, None),
        ([], None),
        (["solo"], "solo"),
        (["a", "b", "c"], "a,b,c"),
        (["  padded  ", "clean"], "padded,clean"),
        (["keep", "", "  "], "keep"),
        (["", "   "], None),
    ],
)
def test_tags_are_stored_as_comma_separated_or_null(db, tags, expected):
    note = notes_store.get_note(notes_store.save_note("tag encoding", tags))
    assert note["tags"] == expected


def test_get_note_returns_a_plain_dict(db):
    note = notes_store.get_note(notes_store.save_note("dict check"))
    assert isinstance(note, dict)
    assert not isinstance(note, sqlite3.Row)
    assert sorted(note) == ["created_at", "id", "source", "tags", "text"]


def test_get_note_returns_none_when_missing(db):
    assert notes_store.get_note(4242) is None


def test_get_note_returns_none_on_empty_table(db):
    assert notes_store.get_note(1) is None


# ======================================================
# aria_memory_notes -- keyword search
# ======================================================
def test_search_matches_note_text(db):
    match = notes_store.save_note("Unity shader compilation notes")
    notes_store.save_note("unrelated content")
    assert [row["id"] for row in notes_store.search_notes_keyword("shader")] == [match]


def test_search_matches_tags(db):
    match = notes_store.save_note("body text has nothing", ["telemetry"])
    notes_store.save_note("body text has nothing either", ["other"])
    assert [row["id"] for row in notes_store.search_notes_keyword("telemetry")] == [match]


def test_search_is_case_insensitive_for_ascii(db):
    note_id = notes_store.save_note("Rendering Pipeline")
    assert [row["id"] for row in notes_store.search_notes_keyword("rendering")] == [note_id]
    assert [row["id"] for row in notes_store.search_notes_keyword("RENDERING")] == [note_id]


def test_search_matches_substrings(db):
    note_id = notes_store.save_note("compilation")
    assert [row["id"] for row in notes_store.search_notes_keyword("pila")] == [note_id]


def test_search_returns_newest_first(db):
    ids = []
    for n in range(3):
        ids.append(notes_store.save_note(f"ordered note {n}", ["ordering"]))
        time.sleep(0.01)
    found = [row["id"] for row in notes_store.search_notes_keyword("ordered")]
    assert found == list(reversed(ids))


def test_search_honours_the_limit(db):
    for n in range(5):
        notes_store.save_note(f"limited note {n}")
    assert len(notes_store.search_notes_keyword("limited", limit=2)) == 2
    assert len(notes_store.search_notes_keyword("limited")) == 5


def test_search_limit_keeps_the_newest_rows(db):
    ids = []
    for n in range(4):
        ids.append(notes_store.save_note(f"newest check {n}"))
        time.sleep(0.01)
    found = [row["id"] for row in notes_store.search_notes_keyword("newest", limit=2)]
    assert found == [ids[3], ids[2]]


def test_search_returns_empty_list_when_nothing_matches(db):
    notes_store.save_note("something else entirely")
    assert notes_store.search_notes_keyword("absent") == []


def test_search_returns_dicts(db):
    notes_store.save_note("dict rows", ["tagged"])
    row = notes_store.search_notes_keyword("dict rows")[0]
    assert isinstance(row, dict)
    assert sorted(row) == ["created_at", "id", "source", "tags", "text"]


def test_search_treats_percent_as_a_literal(db):
    match = notes_store.save_note("coverage is 100% today")
    notes_store.save_note("100 things without the sign")
    assert [row["id"] for row in notes_store.search_notes_keyword("100%")] == [match]


def test_search_treats_underscore_as_a_literal(db):
    match = notes_store.save_note("build_id assigned")
    notes_store.save_note("buildXid assigned")
    assert [row["id"] for row in notes_store.search_notes_keyword("build_id")] == [match]


def test_search_for_a_bare_wildcard_matches_only_literal_wildcards(db):
    match = notes_store.save_note("50% done")
    notes_store.save_note("no sign here")
    assert [row["id"] for row in notes_store.search_notes_keyword("%")] == [match]


def test_search_with_an_empty_query_returns_everything_newest_first(db):
    ids = [notes_store.save_note(f"note {n}") for n in range(3)]
    found = [row["id"] for row in notes_store.search_notes_keyword("")]
    assert found == list(reversed(ids))


# ======================================================
# aria_memory_embeddings
# ======================================================
def test_store_embedding_returns_new_row_id(db):
    note_id = notes_store.save_note("vector owner")
    first = embeddings_store.store_embedding(note_id, bytes([1, 2]))
    second = embeddings_store.store_embedding(note_id, bytes([3, 4]))
    assert isinstance(first, int)
    assert second > first


def test_embedding_round_trips_exactly(db):
    note_id = notes_store.save_note("float vector")
    vector = struct.pack("<4f", 0.1, -0.2, 3.5, 0.0)
    embeddings_store.store_embedding(note_id, vector)
    stored = embeddings_store.get_embeddings_for_note(note_id)
    assert stored == [vector]
    assert struct.unpack("<4f", stored[0]) == pytest.approx((0.1, -0.2, 3.5, 0.0))


def test_embeddings_are_returned_as_bytes(db):
    note_id = notes_store.save_note("type check")
    embeddings_store.store_embedding(note_id, bytes(range(16)))
    stored = embeddings_store.get_embeddings_for_note(note_id)
    assert all(isinstance(vector, bytes) for vector in stored)


def test_embedding_survives_null_and_high_bytes(db):
    note_id = notes_store.save_note("binary safety")
    vector = bytes([0, 255, 0, 128]) + b"text-like" + bytes([0])
    embeddings_store.store_embedding(note_id, vector)
    assert embeddings_store.get_embeddings_for_note(note_id) == [vector]


def test_multiple_embeddings_come_back_in_insertion_order(db):
    note_id = notes_store.save_note("many vectors")
    vectors = [b"first", b"second", b"third"]
    for vector in vectors:
        embeddings_store.store_embedding(note_id, vector)
    assert embeddings_store.get_embeddings_for_note(note_id) == vectors


def test_embeddings_are_scoped_to_their_note(db):
    first = notes_store.save_note("note one")
    second = notes_store.save_note("note two")
    embeddings_store.store_embedding(first, b"one")
    embeddings_store.store_embedding(second, b"two")
    assert embeddings_store.get_embeddings_for_note(first) == [b"one"]
    assert embeddings_store.get_embeddings_for_note(second) == [b"two"]


def test_get_embeddings_returns_empty_list_for_unknown_note(db):
    assert embeddings_store.get_embeddings_for_note(9999) == []


def test_get_embeddings_returns_empty_list_for_note_without_vectors(db):
    note_id = notes_store.save_note("no vectors yet")
    assert embeddings_store.get_embeddings_for_note(note_id) == []


def test_stored_embedding_is_linked_to_the_note_row(db):
    note_id = notes_store.save_note("linkage")
    embedding_id = embeddings_store.store_embedding(note_id, b"vec")
    rows = raw_query(db, "SELECT note_id FROM embeddings WHERE id = ?", (embedding_id,))
    assert rows[0][0] == note_id


# ======================================================
# aria_memory_context and aria_memory_self (identical contracts)
# ======================================================
KEY_VALUE_STORES = [
    pytest.param(
        (
            context_store.set_context,
            context_store.get_context,
            context_store.list_context,
            "user_context",
        ),
        id="user_context",
    ),
    pytest.param(
        (
            self_store.set_self,
            self_store.get_self,
            self_store.list_self,
            "aria_self",
        ),
        id="aria_self",
    ),
]


@pytest.fixture(params=KEY_VALUE_STORES)
def kv(request):
    setter, getter, lister, table = request.param

    class Store:
        set = staticmethod(setter)
        get = staticmethod(getter)
        list = staticmethod(lister)
        table_name = table

    return Store


def test_kv_set_then_get(db, kv):
    kv.set("user.name", "William")
    assert kv.get("user.name") == "William"


def test_kv_get_returns_none_when_missing(db, kv):
    assert kv.get("never.set") is None


def test_kv_stores_a_timestamped_row(db, kv):
    kv.set("k", "v")
    rows = raw_query(db, f"SELECT key, value, updated_at FROM {kv.table_name}")
    assert len(rows) == 1
    key, value, updated_at = rows[0]
    assert (key, value) == ("k", "v")
    parsed = datetime.fromisoformat(updated_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_kv_upsert_overwrites_instead_of_inserting(db, kv):
    kv.set("mode", "offline")
    kv.set("mode", "online")
    rows = raw_query(db, f"SELECT COUNT(*) FROM {kv.table_name} WHERE key = 'mode'")
    assert rows[0][0] == 1
    assert kv.get("mode") == "online"


def test_kv_upsert_advances_updated_at(db, kv):
    kv.set("mode", "offline")
    before = raw_query(db, f"SELECT updated_at FROM {kv.table_name} WHERE key='mode'")[0][0]
    time.sleep(0.01)
    kv.set("mode", "online")
    after = raw_query(db, f"SELECT updated_at FROM {kv.table_name} WHERE key='mode'")[0][0]
    assert datetime.fromisoformat(after) > datetime.fromisoformat(before)


def test_kv_upsert_leaves_other_keys_untouched(db, kv):
    kv.set("a", "1")
    kv.set("b", "2")
    kv.set("a", "changed")
    assert kv.get("b") == "2"
    assert kv.get("a") == "changed"


def test_kv_repeated_upserts_keep_one_row(db, kv):
    for value in ("v1", "v2", "v3", "v4"):
        kv.set("repeated", value)
    rows = raw_query(db, f"SELECT COUNT(*) FROM {kv.table_name}")
    assert rows[0][0] == 1
    assert kv.get("repeated") == "v4"


def test_kv_list_returns_all_pairs(db, kv):
    kv.set("b", "second")
    kv.set("a", "first")
    assert kv.list() == {"a": "first", "b": "second"}


def test_kv_list_is_a_dict_sorted_by_key(db, kv):
    for key in ("zulu", "alpha", "mike"):
        kv.set(key, key.upper())
    listing = kv.list()
    assert isinstance(listing, dict)
    assert list(listing) == ["alpha", "mike", "zulu"]


def test_kv_list_is_empty_on_a_fresh_database(db, kv):
    assert kv.list() == {}


def test_kv_list_reflects_an_upsert(db, kv):
    kv.set("k", "old")
    kv.set("k", "new")
    assert kv.list() == {"k": "new"}


def test_kv_handles_empty_string_values(db, kv):
    kv.set("blank", "")
    assert kv.get("blank") == ""
    assert kv.list() == {"blank": ""}


def test_context_and_self_stores_are_independent(db):
    context_store.set_context("shared.key", "from-context")
    self_store.set_self("shared.key", "from-self")
    assert context_store.get_context("shared.key") == "from-context"
    assert self_store.get_self("shared.key") == "from-self"
    assert context_store.list_context() == {"shared.key": "from-context"}
    assert self_store.list_self() == {"shared.key": "from-self"}


# ======================================================
# Cross-module integration
# ======================================================
def test_full_phase3_round_trip(db):
    note_id = notes_store.save_note("ARIA remembers this", ["memory", "phase3"], "chat")
    embeddings_store.store_embedding(note_id, struct.pack("<2f", 1.0, 2.0))
    context_store.set_context("user.name", "William")
    self_store.set_self("identity.name", "ARIA Lite")

    assert notes_store.get_note(note_id)["tags"] == "memory,phase3"
    assert [row["id"] for row in notes_store.search_notes_keyword("phase3")] == [note_id]
    assert len(embeddings_store.get_embeddings_for_note(note_id)) == 1
    assert context_store.get_context("user.name") == "William"
    assert self_store.get_self("identity.name") == "ARIA Lite"


def test_tests_never_touch_the_real_database(db):
    real_db = Path(memory_db.__file__).resolve().parent / "aria_memory.db"
    assert Path(memory_db.DB_PATH) != real_db
    notes_store.save_note("isolation check")
    assert raw_query(db, "SELECT COUNT(*) FROM notes")[0][0] == 1
