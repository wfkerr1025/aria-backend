# backend/tests/test_upgrade_10_list_self_tool.py
#
# Upgrade 10: the list_self tool wrapper -- listing self-knowledge through
# the dispatcher, with the version in force reported alongside each value.

from __future__ import annotations

from phase3_upgrade_helpers import ConnectionRecorder, db, raw_query  # noqa: F401

from backend import aria_memory_context as context_store
from backend import aria_memory_self as self_store
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
    assert "list_self" in [tool["name"] for tool in list_tools()]


def test_tool_requires_filesystem_permission():
    assert get_tool_schema("list_self").permission == PERMISSION_FILESYSTEM


def test_tool_declares_no_arguments():
    assert get_tool_schema("list_self").parameters == {}


def test_every_memory_tool_is_registered():
    registered = [tool["name"] for tool in list_tools()]
    for name in [
        "save_note", "search_notes", "set_context", "get_context",
        "set_self", "get_self", "list_notes", "delete_note",
        "list_context", "list_self",
    ]:
        assert name in registered


# ---------------- shape ----------------
def test_returns_the_expected_shape(db):
    self_store.set_self("key", "value")
    value = value_of("list_self")
    assert sorted(value) == ["count", "self", "versions"]
    assert isinstance(value["self"], dict)
    assert isinstance(value["versions"], dict)
    assert isinstance(value["count"], int)


def test_empty_store_returns_zeroes(db):
    assert value_of("list_self") == {"self": {}, "versions": {}, "count": 0}


def test_count_matches_the_mapping(db):
    for key in ("a", "b", "c"):
        self_store.set_self(key, key.upper())
    value = value_of("list_self")
    assert value["count"] == len(value["self"]) == 3


def test_keys_are_sorted(db):
    for key in ("zulu", "alpha", "mike"):
        self_store.set_self(key, key.upper())
    assert list(value_of("list_self")["self"]) == ["alpha", "mike", "zulu"]


def test_values_are_returned_verbatim(db):
    self_store.set_self("identity.name", "ARIA Lite")
    assert value_of("list_self")["self"]["identity.name"] == "ARIA Lite"


# ---------------- versions ----------------
def test_a_new_key_reports_version_one(db):
    self_store.set_self("key", "value")
    assert value_of("list_self")["versions"] == {"key": 1}


def test_versions_track_revisions(db):
    self_store.set_self("key", "v1")
    self_store.set_self("key", "v2")
    self_store.set_self("key", "v3")
    assert value_of("list_self")["versions"] == {"key": 3}


def test_versions_are_reported_per_key(db):
    self_store.set_self("stable", "once")
    self_store.set_self("revised", "v1")
    self_store.set_self("revised", "v2")
    assert value_of("list_self")["versions"] == {"revised": 2, "stable": 1}


def test_versions_cover_exactly_the_listed_keys(db):
    for key in ("a", "b"):
        self_store.set_self(key, "value")
    value = value_of("list_self")
    assert set(value["versions"]) == set(value["self"])


def test_an_unchanged_rewrite_does_not_bump_the_version(db):
    self_store.set_self("key", "same")
    self_store.set_self("key", "same")
    assert value_of("list_self")["versions"] == {"key": 1}


def test_rollback_is_reflected_in_the_listing(db):
    self_store.set_self("key", "v1")
    self_store.set_self("key", "v2")
    self_store.rollback_self("key", 1)
    value = value_of("list_self")
    assert value["self"] == {"key": "v1"}
    assert value["versions"] == {"key": 3}


# ---------------- live data ----------------
def test_listing_reflects_a_write_made_through_the_dispatcher(db):
    value_of("set_self", {"key": "via.tool", "value": "written"})
    assert value_of("list_self")["self"] == {"via.tool": "written"}


def test_personal_context_is_not_included(db):
    context_store.set_context("user.name", "William")
    self_store.set_self("identity.name", "ARIA")
    assert value_of("list_self")["self"] == {"identity.name": "ARIA"}


# ---------------- calling and permission ----------------
def test_callable_with_no_arguments(db):
    self_store.set_self("key", "value")
    assert execute_tool("list_self").ok


def test_safe_only_caller_is_denied(db):
    result = execute_tool("list_self", {}, allowed_permissions={PERMISSION_SAFE})
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_PERMISSION_DENIED


def test_filesystem_caller_is_allowed(db):
    assert execute_tool("list_self", {}, allowed_permissions={PERMISSION_FILESYSTEM}).ok


# ---------------- cleanup ----------------
def test_connections_are_closed(db, monkeypatch):
    recorder = ConnectionRecorder()
    monkeypatch.setattr(self_store, "get_connection", recorder)
    self_store.set_self("key", "value")
    value_of("list_self")
    assert recorder.connections
    assert recorder.all_closed
