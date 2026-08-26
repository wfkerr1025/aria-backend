# backend/tests/test_upgrade_09_list_context_tool.py
#
# Upgrade 9: the list_context tool wrapper -- listing live personal context
# through the dispatcher, with expired entries filtered out by the store.

from __future__ import annotations

import time

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

SHORT_TTL = 0.3


def value_of(name, args=None, allowed_permissions=None):
    result = execute_tool(name, args or {}, allowed_permissions=allowed_permissions)
    assert result.ok, f"{name} failed: {result.error_code}: {result.error}"
    return result.value


# ---------------- registration ----------------
def test_tool_is_registered():
    assert "list_context" in [tool["name"] for tool in list_tools()]


def test_tool_requires_filesystem_permission():
    assert get_tool_schema("list_context").permission == PERMISSION_FILESYSTEM


def test_tool_declares_no_arguments():
    assert get_tool_schema("list_context").parameters == {}


# ---------------- shape ----------------
def test_returns_the_expected_shape(db):
    context_store.set_context("key", "value")
    value = value_of("list_context")
    assert sorted(value) == ["context", "count"]
    assert isinstance(value["context"], dict)
    assert isinstance(value["count"], int)


def test_empty_store_returns_zeroes(db):
    assert value_of("list_context") == {"context": {}, "count": 0}


def test_count_matches_the_mapping(db):
    for key in ("a", "b", "c"):
        context_store.set_context(key, key.upper())
    value = value_of("list_context")
    assert value["count"] == len(value["context"]) == 3


def test_values_are_returned_verbatim(db):
    context_store.set_context("theme", "dark")
    assert value_of("list_context")["context"]["theme"] == "dark"


def test_keys_are_sorted(db):
    for key in ("zulu", "alpha", "mike"):
        context_store.set_context(key, key.upper())
    assert list(value_of("list_context")["context"]) == ["alpha", "mike", "zulu"]


def test_empty_string_values_are_preserved(db):
    context_store.set_context("blank", "")
    assert value_of("list_context")["context"] == {"blank": ""}


# ---------------- live data ----------------
def test_listing_reflects_an_update(db):
    context_store.set_context("theme", "dark")
    context_store.set_context("theme", "light")
    assert value_of("list_context")["context"] == {"theme": "light"}


def test_listing_reflects_a_write_made_through_the_dispatcher(db):
    value_of("set_context", {"key": "via.tool", "value": "written"})
    assert value_of("list_context")["context"] == {"via.tool": "written"}


def test_live_ttl_entries_are_included(db):
    context_store.set_context("temporary", "still here", ttl_seconds=600)
    assert "temporary" in value_of("list_context")["context"]


def test_expired_entries_are_excluded(db):
    context_store.set_context("permanent", "stays")
    context_store.set_context("fleeting", "goes", ttl_seconds=SHORT_TTL)
    time.sleep(SHORT_TTL + 0.05)
    value = value_of("list_context")
    assert value["context"] == {"permanent": "stays"}
    assert value["count"] == 1


def test_expired_entries_are_excluded_even_before_purge(db):
    context_store.set_context("fleeting", "goes", ttl_seconds=SHORT_TTL)
    time.sleep(SHORT_TTL + 0.05)
    assert value_of("list_context")["count"] == 0
    assert raw_query(db, "SELECT COUNT(*) FROM user_context")[0][0] == 1


# ---------------- isolation ----------------
def test_self_knowledge_is_not_included(db):
    self_store.set_self("identity.name", "ARIA")
    context_store.set_context("user.name", "William")
    assert value_of("list_context")["context"] == {"user.name": "William"}


# ---------------- calling and permission ----------------
def test_callable_with_no_arguments(db):
    context_store.set_context("key", "value")
    assert execute_tool("list_context").ok


def test_unknown_arguments_are_ignored_by_validation(db):
    # The registry validates declared parameters; it does not reject extra
    # keys, so this fails at the handler instead of silently succeeding.
    result = execute_tool("list_context", {"unexpected": 1})
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_EXECUTION_FAILED


def test_safe_only_caller_is_denied(db):
    result = execute_tool("list_context", {}, allowed_permissions={PERMISSION_SAFE})
    assert not result.ok
    assert result.error_code == ErrorCode.TOOL_PERMISSION_DENIED


def test_filesystem_caller_is_allowed(db):
    assert execute_tool("list_context", {}, allowed_permissions={PERMISSION_FILESYSTEM}).ok


# ---------------- cleanup ----------------
def test_connections_are_closed(db, monkeypatch):
    recorder = ConnectionRecorder()
    monkeypatch.setattr(context_store, "get_connection", recorder)
    context_store.set_context("key", "value")
    value_of("list_context")
    assert recorder.connections
    assert recorder.all_closed
