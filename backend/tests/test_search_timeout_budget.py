# backend/tests/test_search_timeout_budget.py
#
# One slow provider must not cost the turn.
#
# web_search is a chain: specialists first, then a general-web fallback
# when none of them answered. Its tool budget in tool_registry was 10.0
# seconds and http_fetch's per-request timeout was also 10 -- so one
# unreachable host consumed the whole budget on its own and the sandbox
# abandoned the tool before the rest of the chain had run.
#
# Measured live, from the server log:
#
#   17:24:42.808  planning
#   17:24:52      gdelt: connect timeout after 10030ms   (since removed)
#   17:24:52      "no specialist answered; trying the web"
#   17:24:52.819  stream_start -> "I could not retrieve current data"
#   17:24:55      langsearch: 200, ten real results
#
# The answer was given up on 2.1 seconds before the evidence arrived. The
# search stack was working; the budget was not.
#
# These tests pin the arithmetic rather than the wall clock. A test that
# actually sleeps for the timeouts would take half a minute and would
# still only prove that time passes.

from __future__ import annotations

import pytest

from tools import http_fetch as http_fetch_module
from tools.providers import NEWS_PROVIDERS, news_mediastack, news_nytimes


def test_one_request_cannot_spend_the_whole_tool_budget():
    from backend.core.tool_registry import get_tool_schema

    schema = get_tool_schema("web_search")

    # The relationship, not either number: whatever they are set to, a
    # single request must leave room for the providers behind it.
    assert http_fetch_module.DEFAULT_TIMEOUT_SECONDS < schema.timeout_seconds


def test_the_worst_realistic_chain_fits_inside_the_budget():
    from backend.core.tool_registry import get_tool_schema
    from tools.providers import PRIMARY_PROVIDERS, FALLBACK_PROVIDERS

    schema = get_tool_schema("web_search")
    default = http_fetch_module.DEFAULT_TIMEOUT_SECONDS

    def budget(module):
        return getattr(module, "NEWS_TIMEOUT_SECONDS", default)

    worst = sum(budget(m) for m in (*PRIMARY_PROVIDERS, *FALLBACK_PROVIDERS))

    # Every provider hanging until its own deadline, one after another,
    # still has to finish inside the tool's. Otherwise the fallback tier
    # is unreachable exactly when it is most needed -- which is what
    # happened.
    assert worst <= schema.timeout_seconds


@pytest.mark.parametrize("module", NEWS_PROVIDERS)
def test_the_news_tier_bounds_itself_below_the_default(module):
    # Three providers answering the same question, none of them
    # required. One being down is not a reason to spend the budget the
    # others, and the fallback behind them, still need.
    assert module.NEWS_TIMEOUT_SECONDS < http_fetch_module.DEFAULT_TIMEOUT_SECONDS


@pytest.mark.parametrize("module", NEWS_PROVIDERS)
def test_every_news_provider_actually_passes_its_timeout(monkeypatch, module):
    seen = {}

    def fake(url, **kwargs):
        seen.update(kwargs)
        return {"status": "ok", "data": {}}

    monkeypatch.setattr(module, "http_fetch", fake)
    # GDELT is keyless -- it is the floor the other two sit above.
    monkeypatch.setattr(module, "api_key", lambda: "test-key", raising=False)

    module.lookup("latest news about AI regulation")

    # Declaring the constant and not passing it would read as fixed while
    # behaving exactly as before.
    assert seen.get("timeout") == module.NEWS_TIMEOUT_SECONDS


def test_http_fetch_passes_the_timeout_through(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        headers = {"Content-Type": "application/json"}

        @staticmethod
        def json():
            return {}

    monkeypatch.setattr(http_fetch_module.requests, "get",
                        lambda url, **kw: calls.append(kw) or Response())
    monkeypatch.setattr(http_fetch_module.requests, "post",
                        lambda url, **kw: calls.append(kw) or Response())

    http_fetch_module.http_fetch("https://example.com/a")
    assert calls[-1]["timeout"] == http_fetch_module.DEFAULT_TIMEOUT_SECONDS

    http_fetch_module.http_fetch("https://example.com/b", timeout=2)
    assert calls[-1]["timeout"] == 2

    http_fetch_module.http_fetch("https://example.com/c", json_body={"q": "x"}, timeout=3)
    assert calls[-1]["timeout"] == 3


def test_a_dead_specialist_still_leaves_the_fallback_its_answer(monkeypatch):
    """The shape of the live failure, without the wall clock."""
    from tools import web_search as ws
    from tools.providers import web_langsearch

    def unreachable(url, **kwargs):
        # What http_fetch returns when a host does not answer.
        return {"status": "error", "url": url, "error": "connect timeout"}

    for module in (news_nytimes, news_mediastack):
        monkeypatch.setattr(module, "api_key", lambda: "test-key")
        monkeypatch.setattr(module, "http_fetch", unreachable)
    monkeypatch.setattr(web_langsearch, "api_key", lambda: "test-key")
    monkeypatch.setattr(web_langsearch, "http_fetch", lambda url, **kw: {
        "status": "ok", "data": {"data": {"webPages": {"value": [
            {"name": "MCP Gets Its Biggest Rewrite and Other Python News",
             "url": "https://realpython.com/python-news-august-2026/",
             "displayUrl": "https://realpython.com/python-news-august-2026/",
             "snippet": "Community news for August 2026."},
        ]}}},
    })

    result = ws.web_search("python latest news")

    assert result["providers"] == ["langsearch"]
    assert result["summary"] != ws.NO_RESULTS
