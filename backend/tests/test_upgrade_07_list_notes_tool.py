# backend/tests/test_upgrade_07_list_notes_tool.py
#
# Upgrade 7: the list_notes tool wrapper and its dispatcher registration,
# exercised through execute_tool() rather than by calling the wrapper.

from __future__ import annotations

from phase3_upgrade_helpers import ConnectionRecorder, db, raw_query  # noqa: F401

from backend import aria_memory_notes as notes_store
from backend.core.errors import ErrorCode
from backend.core.tool_registry import (
    PERMISSION_FILESYSTEM,
    PERMISSION_SAFE,
    execute_tool,
    get_tool_schema,
    list_tools,
)


def value_of(name, args=None, allowed_permissions=None):
    result = execute_tool(name, args or {}, allowed_permissions=allowed_permissions)
    assert result.ok, f"{name} failed: {result.error_code}: {result.error}"
    return result.value


# ---------------- registration ----------------
def test_tool_is_registered():
    assert "list_notes" in [tool["name"] for tool in list_tools()]


def test_tool_requires_filesystem_permission():
    assert get_tool_schema("list_notes").permission == PERMISSION_FILESYSTEM


def test_registering_it_left_the_other_tools_alone():
    registered = [tool["name"] for tool in list_tools()]
    for name in ["weather", "web_search", "save_note", "search_notes"]:
        assert name in registered


# ---------------- shape ----------------
def test_returns_the_expected_shape(db):
    notes_store.save_note("something")
    value = value_of("list_notes")
    assert sorted(value) == ["count", "results"]
    assert isinstance(value["results"], list)
    assert isinstance(value["count"], int)


def test_rows_carry_every_note_field(db):
    notes_store.save_note("full row", ["a"], "voice")
    row = value_of("list_notes")["results"][0]
    assert sorted(row) == ["created_at", "id", "source", "tags", "text"]
    assert row["tags"] == "a"
    assert row["source"] == "voice"


def test_count_matches_the_rows(db):
    for n in range(3):
        notes_store.save_note(f"note {n}")
    value = value_of("list_notes")
    assert value["count"] == len(value["results"]) == 3


def test_empty_store_returns_nothing(db):
    assert value_of("list_notes") == {"results": [], "count": 0}


# ---------------- ordering and paging ----------------
def test_notes_come_back_newest_first(db):
    ids = [notes_store.save_note(f"note {n}") for n in range(4)]
    assert [row["id"] for row in value_of("list_notes")["results"]] == list(reversed(ids))


def test_limit_is_honoured(db):
    for n in range(5):
        notes_store.save_note(f"note {n}")
    assert value_of("list_notes", {"limit": 2})["count"] == 2


def test_limit_keeps_the_newest(db):
    ids = [notes_store.save_note(f"note {n}") for n in range(4)]
    got = [row["id"] for row in value_of("list_notes", {"limit": 2})["results"]]
    assert got == [ids[3], ids[2]]


def test_offset_pages_through(db):
    ids = [notes_store.save_note(f"note {n}") for n in range(4)]
    page = [row["id"] for row in value_of("list_notes", {"limit": 2, "offset": 2})["results"]]
    assert page == [ids[1], ids[0]]


def test_pages_do_not_overlap(db):
    for n in range(6):
        notes_store.save_note(f"note {n}")
    first = {row["id"] for row in value_of("list_notes", {"limit": 3})["results"]}
    second = {row["id"] for row in value_of("list_notes", {"limit": 3, "offset": 3})["results"]}
    assert first & second == set()
    assert len(first | second) == 6


def test_offset_past_the_end_is_empty(db):
    notes_store.save_note("only one")
    assert value_of("list_notes", {"offset": 50})["count"] == 0


def test_listing_reflects_a_deletion(db):
    keep = notes_store.save_note("keep")
    drop = notes_store.save_note("drop")
    notes_store.delete_note(drop)
    assert [row["id"] for row in value_of("list_notes")["results"]] == [keep]


# ---------------- validation and permission ----------------
def test_non_integer_limit_is_rejected(db):
    result = execute_tool("list_notes", {"limit": "many"})
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_INVALID_ARGS


def test_non_integer_offset_is_rejected(db):
    result = execute_tool("list_notes", {"offset": "later"})
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_INVALID_ARGS


def test_safe_only_caller_is_denied(db):
    result = execute_tool("list_notes", {}, allowed_permissions={PERMISSION_SAFE})
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_PERMISSION_DENIED


def test_filesystem_caller_is_allowed(db):
    assert execute_tool("list_notes", {}, allowed_permissions={PERMISSION_FILESYSTEM}).ok


# ---------------- cleanup ----------------
def test_connections_are_closed(db, monkeypatch):
    recorder = ConnectionRecorder()
    monkeypatch.setattr(notes_store, "get_connection", recorder)
    notes_store.save_note("cleanup")
    value_of("list_notes")
    assert recorder.connections
    assert recorder.all_closed
