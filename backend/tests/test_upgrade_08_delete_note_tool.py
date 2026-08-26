# backend/tests/test_upgrade_08_delete_note_tool.py
#
# Upgrade 8: the delete_note tool wrapper -- removing a note along with its
# embeddings (explicitly, no cascade) and its indexed chunks (by cascade).

from __future__ import annotations

from phase3_upgrade_helpers import ConnectionRecorder, db, raw_query  # noqa: F401

from backend import aria_memory_note_embeddings as note_embeddings
from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
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


def furnished_note(text="delete me " + " ".join(f"w{n}" for n in range(60))):
    """A note with two vectors and a full set of indexed chunks."""
    note_id = notes_store.save_note(text)
    note_embeddings.embed_note(note_id)
    note_embeddings.embed_note(note_id)
    semantic.index_note(note_id)
    return note_id


# ---------------- registration ----------------
def test_tool_is_registered():
    assert "delete_note" in [tool["name"] for tool in list_tools()]


def test_tool_requires_filesystem_permission():
    assert get_tool_schema("delete_note").permission == PERMISSION_FILESYSTEM


# ---------------- shape ----------------
def test_returns_the_expected_shape(db):
    note_id = notes_store.save_note("plain note")
    value = value_of("delete_note", {"note_id": note_id})
    assert sorted(value) == ["chunks_deleted", "embeddings_deleted", "note_id", "status"]
    assert value["note_id"] == note_id
    assert value["status"] == "deleted"


def test_counts_report_what_actually_went(db):
    note_id = furnished_note()
    expected_chunks = len(semantic.get_note_chunks(note_id))
    value = value_of("delete_note", {"note_id": note_id})
    assert value["embeddings_deleted"] == 2
    assert value["chunks_deleted"] == expected_chunks


def test_a_bare_note_reports_zero_counts(db):
    note_id = notes_store.save_note("nothing attached")
    value = value_of("delete_note", {"note_id": note_id})
    assert value["embeddings_deleted"] == 0
    assert value["chunks_deleted"] == 0


# ---------------- deletion ----------------
def test_the_note_row_is_gone(db):
    note_id = notes_store.save_note("plain note")
    value_of("delete_note", {"note_id": note_id})
    assert notes_store.get_note(note_id) is None
    assert raw_query(db, "SELECT COUNT(*) FROM notes")[0][0] == 0


def test_embeddings_are_removed(db):
    note_id = furnished_note()
    value_of("delete_note", {"note_id": note_id})
    assert raw_query(db, "SELECT COUNT(*) FROM embeddings")[0][0] == 0


def test_indexed_chunks_are_removed(db):
    note_id = furnished_note()
    value_of("delete_note", {"note_id": note_id})
    assert raw_query(db, "SELECT COUNT(*) FROM semantic_index")[0][0] == 0
    assert semantic.get_note_chunks(note_id) == []


def test_deleting_a_note_with_vectors_does_not_raise(db):
    # embeddings has no ON DELETE CASCADE, so this is the case that would
    # be an IntegrityError if the wrapper did not clear them first.
    note_id = notes_store.save_note("has vectors")
    note_embeddings.embed_note(note_id)
    assert execute_tool("delete_note", {"note_id": note_id}).ok


def test_other_notes_are_untouched(db):
    doomed = furnished_note()
    kept = furnished_note("keep me " + " ".join(f"k{n}" for n in range(60)))
    value_of("delete_note", {"note_id": doomed})
    assert notes_store.get_note(kept) is not None
    assert semantic.get_note_chunks(kept) != []
    assert note_embeddings.get_note_vectors(kept) != []


def test_deleted_note_disappears_from_search(db):
    note_id = notes_store.save_note("unity shader notes")
    value_of("delete_note", {"note_id": note_id})
    assert value_of("search_notes", {"query": "shader"})["count"] == 0


def test_deleted_note_disappears_from_semantic_search(db):
    note_id = furnished_note()
    value_of("delete_note", {"note_id": note_id})
    assert semantic.search_semantic("w1") == []


# ---------------- missing note ----------------
def test_missing_note_reports_not_found(db):
    value = value_of("delete_note", {"note_id": 4242})
    assert value == {
        "note_id": 4242,
        "status": "not_found",
        "embeddings_deleted": 0,
        "chunks_deleted": 0,
    }


def test_missing_note_is_not_an_error(db):
    assert execute_tool("delete_note", {"note_id": 4242}).ok


def test_deleting_twice_is_safe(db):
    note_id = notes_store.save_note("delete twice")
    assert value_of("delete_note", {"note_id": note_id})["status"] == "deleted"
    assert value_of("delete_note", {"note_id": note_id})["status"] == "not_found"


# ---------------- validation and permission ----------------
def test_missing_note_id_is_rejected(db):
    result = execute_tool("delete_note", {})
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_INVALID_ARGS


def test_non_integer_note_id_is_rejected(db):
    result = execute_tool("delete_note", {"note_id": "first"})
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_INVALID_ARGS


def test_safe_only_caller_is_denied(db):
    note_id = notes_store.save_note("must survive")
    result = execute_tool("delete_note", {"note_id": note_id}, allowed_permissions={PERMISSION_SAFE})
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_PERMISSION_DENIED


def test_denied_delete_leaves_the_note_alone(db):
    note_id = notes_store.save_note("must survive")
    execute_tool("delete_note", {"note_id": note_id}, allowed_permissions={PERMISSION_SAFE})
    assert notes_store.get_note(note_id) is not None


def test_filesystem_caller_is_allowed(db):
    note_id = notes_store.save_note("deletable")
    assert execute_tool(
        "delete_note", {"note_id": note_id}, allowed_permissions={PERMISSION_FILESYSTEM}
    ).ok


# ---------------- cleanup ----------------
def test_connections_are_closed(db, monkeypatch):
    note_id = notes_store.save_note("cleanup")
    recorder = ConnectionRecorder()
    monkeypatch.setattr(notes_store, "get_connection", recorder)
    value_of("delete_note", {"note_id": note_id})
    assert recorder.connections
    assert recorder.all_closed
