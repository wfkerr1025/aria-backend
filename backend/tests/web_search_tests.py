# backend/tests/web_search_tests.py
#
# Regression tests for tools/web_search.py's handling of a non-object
# `data` value from DuckDuckGo's Instant Answer API. Real production
# crash: a conversational, non-search-shaped query (e.g. "can you
# search the web?") could get back a bare string/list/number/null at
# the top level instead of a JSON object, and the old code did
# `data = result.get("data", {}) or {}` then `data.get("AbstractText")`
# unconditionally — a non-empty string passes the `or {}` fallback
# untouched, so `.get()` crashed with "'str' object has no attribute
# 'get'" (backend/websocket/handlers.py's _answer_tool_query_directly()
# is what actually surfaced this to a live chat as a Dispatch error).
#
# http_fetch() is monkeypatched directly — no real network calls — so
# these tests exercise web_search()'s own normalization, not
# DuckDuckGo's live API.
#
# Self-contained, plain-assert tests, matching backend/tests/weather_fusion_tests.py
# and friends — not pytest. Run directly:
#
#   python backend/tests/web_search_tests.py

from __future__ import annotations

import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _Patcher:
    def __init__(self):
        self._saved = []

    def set(self, obj, attr, value):
        self._saved.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def restore(self):
        for obj, attr, original in reversed(self._saved):
            setattr(obj, attr, original)


def _fake_http_ok(data):
    return {"status": "ok", "data": data, "url": "http://x", "code": 200, "ok": True, "headers": {}, "elapsed_ms": 1.0}


tests = []


def test(fn):
    tests.append(fn)
    return fn


# ============================================================
# PART 1 — _normalize_data() in isolation
# ============================================================
@test
def test_normalize_data_passes_a_real_dict_through_unchanged():
    from tools.web_search import _normalize_data
    d = {"AbstractText": "hello", "Heading": "H"}
    assert _normalize_data(d) is d


@test
def test_normalize_data_wraps_a_nonempty_string_as_abstract_text():
    from tools.web_search import _normalize_data
    result = _normalize_data("some real text DuckDuckGo sent back")
    assert result == {"AbstractText": "some real text DuckDuckGo sent back"}


@test
def test_normalize_data_strips_whitespace_from_a_string():
    from tools.web_search import _normalize_data
    result = _normalize_data("  padded text  ")
    assert result == {"AbstractText": "padded text"}


@test
def test_normalize_data_treats_empty_string_as_nothing_usable():
    from tools.web_search import _normalize_data
    assert _normalize_data("") == {}
    assert _normalize_data("   ") == {}


@test
def test_normalize_data_treats_a_list_as_nothing_usable():
    from tools.web_search import _normalize_data
    assert _normalize_data(["a", "b"]) == {}


@test
def test_normalize_data_treats_a_number_as_nothing_usable():
    from tools.web_search import _normalize_data
    assert _normalize_data(42) == {}
    assert _normalize_data(3.14) == {}


@test
def test_normalize_data_treats_none_as_nothing_usable():
    from tools.web_search import _normalize_data
    assert _normalize_data(None) == {}


# ============================================================
# PART 2 — web_search() end-to-end, every malformed `data` shape
# ============================================================
@test
def test_web_search_never_crashes_on_string_data():
    from tools import web_search as ws_mod

    p = _Patcher()
    try:
        p.set(ws_mod, "http_fetch", lambda url: _fake_http_ok("No good result found."))
        result = ws_mod.web_search("can you search the web?")
    finally:
        p.restore()

    assert result["status"] == "ok"
    assert result["summary"] == "No good result found."
    assert result["raw"] == "No good result found.", "raw must preserve the true unmodified API response"


@test
def test_web_search_never_crashes_on_list_data():
    from tools import web_search as ws_mod

    p = _Patcher()
    try:
        p.set(ws_mod, "http_fetch", lambda url: _fake_http_ok(["unexpected", "array"]))
        result = ws_mod.web_search("some query")
    finally:
        p.restore()

    assert result["status"] == "ok"
    assert result["summary"] is None
    assert result["related"] == []
    assert result["raw"] == ["unexpected", "array"]


@test
def test_web_search_never_crashes_on_number_data():
    from tools import web_search as ws_mod

    p = _Patcher()
    try:
        p.set(ws_mod, "http_fetch", lambda url: _fake_http_ok(42))
        result = ws_mod.web_search("some query")
    finally:
        p.restore()

    assert result["status"] == "ok"
    assert result["summary"] is None
    assert result["raw"] == 42


@test
def test_web_search_never_crashes_on_null_data():
    from tools import web_search as ws_mod

    p = _Patcher()
    try:
        p.set(ws_mod, "http_fetch", lambda url: _fake_http_ok(None))
        result = ws_mod.web_search("some query")
    finally:
        p.restore()

    assert result["status"] == "ok"
    assert result["summary"] is None
    assert result["raw"] is None


@test
def test_web_search_still_extracts_real_fields_from_a_normal_dict_response():
    """Confirms the fix didn't change the ordinary, well-formed path."""
    from tools import web_search as ws_mod

    p = _Patcher()
    try:
        p.set(ws_mod, "http_fetch", lambda url: _fake_http_ok({
            "AbstractText": "Python is a programming language.",
            "Heading": "Python",
            "AbstractURL": "https://en.wikipedia.org/wiki/Python",
            "RelatedTopics": [
                {"Text": "Python (genus), a snake", "FirstURL": "https://example.com/snake"},
                "not a dict, must be skipped without crashing",
                {"Text": "", "FirstURL": ""},
            ],
        }))
        result = ws_mod.web_search("python")
    finally:
        p.restore()

    assert result["status"] == "ok"
    assert result["heading"] == "Python"
    assert result["summary"] == "Python is a programming language."
    assert result["source_url"] == "https://en.wikipedia.org/wiki/Python"
    assert result["related"] == [{"text": "Python (genus), a snake", "url": "https://example.com/snake"}]


@test
def test_web_search_http_failure_path_is_unaffected():
    """The pre-existing http_fetch-failed branch must still work exactly as before."""
    from tools import web_search as ws_mod

    p = _Patcher()
    try:
        p.set(ws_mod, "http_fetch", lambda url: {"status": "error", "error": "timed out"})
        result = ws_mod.web_search("some query")
    finally:
        p.restore()

    assert result["status"] == "error"
    assert result["error"] == "timed out"


# ============================================================
# PART 3 — the real end-to-end path this crash was reported through:
# backend.core.tool_executor.format_search_reply() must never crash
# either, given whatever web_search() can now return.
# ============================================================
@test
def test_format_search_reply_never_crashes_on_any_normalized_shape():
    from backend.core.tool_executor import format_search_reply
    from tools import web_search as ws_mod

    p = _Patcher()
    try:
        for fake_data in ("a bare string", ["a", "list"], 42, None, {}):
            p.set(ws_mod, "http_fetch", lambda url, d=fake_data: _fake_http_ok(d))
            result = ws_mod.web_search("can you search the web?")
            reply = format_search_reply(result)
            assert isinstance(reply, str) and reply, f"expected a non-empty string reply for data={fake_data!r}, got {reply!r}"
            p.restore()
            p = _Patcher()
    finally:
        p.restore()


# ============================================================
# RUNNER
# ============================================================
def main() -> int:
    failures = []
    for t in tests:
        name = t.__name__
        try:
            t()
            print(f"PASS  {name}")
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            failures.append(name)
        except Exception as e:
            print(f"ERROR {name}: {e}")
            traceback.print_exc()
            failures.append(name)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1

    print("All tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
