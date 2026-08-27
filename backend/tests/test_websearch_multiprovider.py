# backend/tests/test_websearch_multiprovider.py
#
# The web_search tool, backed by providers that can answer the question.
#
# DuckDuckGo's Instant Answer API was the whole backend, and it returned an
# empty envelope for every question this assistant is actually asked:
#
#     stock price of Microsoft        -> {"Abstract":"","AbstractSource":"",...}
#     latest news about AI regulation -> {"Abstract":"","AbstractSource":"",...}
#     current EUR/USD exchange rate   -> {"Abstract":"","AbstractSource":"",...}
#
# Worse, that envelope came back as the `summary`, so nothing-found looked
# exactly like something-found all the way down the pipeline.
#
# Every test here stubs http_fetch. Nothing in this file touches the
# network: a suite that depends on Yahoo being up, or on a NewsAPI key
# existing on the machine running it, is a suite that fails for reasons
# that have nothing to do with the code.

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

from backend.tools.tool_executor import normalize_tool_value
from tools import web_search as ws
from tools.providers import exchange_rate_api, news_common, news_nytimes, yahoo_finance


# ------------------------------------------------------
# Stubs, in the shape each API really returns
# ------------------------------------------------------
# The v8/chart shape, as the endpoint really answers. v7/quote is gone:
# it returns 401 without an authenticated crumb, whatever headers are
# sent, so the stub that mimicked it was testing a dead endpoint.
YAHOO_PAYLOAD = {"status": "ok", "data": {"chart": {"result": [{"meta": {
    "symbol": "MSFT", "shortName": "Microsoft Corporation",
    "longName": "Microsoft Corporation",
    "regularMarketPrice": 496.37, "chartPreviousClose": 491.71,
    "regularMarketDayHigh": 497.40, "regularMarketDayLow": 487.31,
    "regularMarketVolume": 19997284, "currency": "USD",
    "regularMarketTime": 1787774401,
}}]}}}

# The NYTimes article-search shape. This was GDELT's artlist shape until
# GDELT was removed -- api.gdeltproject.org would not answer, and a
# keyless floor that cannot be reached is not a floor. The Times leads
# the tier now, and carries a real abstract rather than a bare headline.
NEWS_PAYLOAD = {"status": "ok", "data": {"response": {"docs": [
    {"headline": {"main": "EU adopts landmark AI rules"},
     "abstract": "The rules take effect next year.",
     "web_url": "https://example.com/eu-ai-act",
     "pub_date": "2026-08-26T09:15:00+0000"},
    {"headline": {"main": "US states weigh their own AI statutes"},
     "abstract": "A patchwork is forming while Congress deliberates.",
     "web_url": "https://example.com/us-states-ai",
     "pub_date": "2026-08-25T18:02:00+0000"},
]}}}

FX_PAYLOAD = {"status": "ok", "data": {
    "result": "success", "base_code": "EUR",
    "time_last_update_utc": "2026-08-26T00:00:00+00:00",
    "conversion_rates": {"USD": 1.089, "GBP": 0.851},
}}


@pytest.fixture
def providers(monkeypatch):
    """Route each provider's http_fetch to a recorded stub.

    Keys are stubbed too, so the result does not depend on whether the
    machine running the tests happens to have one configured.
    """
    calls = {"yahoo": [], "news": [], "fx": []}

    def stub(module, bucket, payload):
        # **kwargs, not a bare url: http_fetch takes a per-call
        # timeout now, and a stub narrower than the real signature
        # fails on the argument rather than testing anything.
        def fetch(url, **kwargs):
            calls[bucket].append(url)
            return payload
        monkeypatch.setattr(module, "http_fetch", fetch)

    stub(yahoo_finance, "yahoo", YAHOO_PAYLOAD)
    stub(news_nytimes, "news", NEWS_PAYLOAD)
    monkeypatch.setattr(news_nytimes, "api_key", lambda: "test-key")
    stub(exchange_rate_api, "fx", FX_PAYLOAD)
    monkeypatch.setattr(exchange_rate_api, "api_key", lambda: "test-key")

    # One news provider is enough: these tests are about the
    # multi-provider mechanism, not about the tier's internal ordering,
    # which test_news_providers covers. Mediastack stays off so the news
    # tier has exactly one voice.
    #
    # This was GDELT, chosen because it needed no key. GDELT is gone --
    # unreachable, and a floor that cannot be reached is not a floor --
    # so the Times takes its place with a key stubbed in above.
    from tools.providers import news_mediastack
    monkeypatch.setattr(news_mediastack, "api_key", lambda: None)
    return calls


def normalized_for(result):
    return normalize_tool_value("web_search", {"raw": result, "reply": result.get("summary")})


# ======================================================
# A. Stock quote
# ======================================================
def test_a_stock_question_reaches_yahoo_and_returns_a_price(providers):
    result = ws.web_search("stock price of Microsoft")

    assert providers["yahoo"], "the finance provider was never called"
    assert "MSFT" in providers["yahoo"][0]
    assert result["items"], "no items returned"

    item = result["items"][0]
    assert "496.37" in item["snippet"]
    assert item["provenance"] == "yahoo-finance"
    assert item["url"] == "https://finance.yahoo.com/quote/MSFT"
    assert item["timestamp"]


def test_the_quote_reports_only_fields_the_payload_carried(providers, monkeypatch):
    """A missing change must not be printed as "0.00%".

    A zero printed because a field was absent reads as "flat today", which
    is a claim the payload never made.
    """
    sparse = {"status": "ok", "data": {"chart": {"result": [
        {"meta": {"symbol": "MSFT", "regularMarketPrice": 496.37, "currency": "USD"}},
    ]}}}
    monkeypatch.setattr(yahoo_finance, "http_fetch", lambda url: sparse)

    snippet = ws.web_search("stock price of Microsoft")["items"][0]["snippet"]
    assert "496.37" in snippet
    assert "%" not in snippet
    assert "Volume" not in snippet


def test_a_quote_with_no_price_is_not_a_result(providers, monkeypatch):
    monkeypatch.setattr(yahoo_finance, "http_fetch", lambda url: {
        "status": "ok", "data": {"chart": {"result": [{"meta": {"symbol": "MSFT"}}]}}})
    assert ws.web_search("stock price of Microsoft")["items"] == []


def test_the_normalized_result_carries_the_quote_through(providers):
    items = normalized_for(ws.web_search("stock price of Microsoft"))

    assert items
    assert items[0].title.startswith("Microsoft Corporation (MSFT)")
    assert "496.37" in items[0].snippet
    assert items[0].source == "yahoo-finance"
    assert items[0].timestamp is not None


# ======================================================
# B. News
# ======================================================
def test_a_news_question_reaches_newsapi_and_returns_headlines(providers):
    result = ws.web_search("latest news about AI regulation")

    assert providers["news"], "the news provider was never called"
    assert len(result["items"]) == 2
    assert all(item["provenance"] == "nytimes" for item in result["items"])
    assert result["items"][0]["title"] == "EU adopts landmark AI rules"


def test_the_query_sent_to_the_news_provider_is_the_subject_not_the_sentence():
    """"show me the latest news about X" should search for X."""
    assert news_common.news_search_terms("Show me the latest news about AI regulation.") == "ai regulation"
    assert news_common.news_search_terms("any breaking news on the election?") == "election"


def test_each_headline_keeps_its_own_title_and_publication_time(providers):
    items = normalized_for(ws.web_search("latest news about AI regulation"))

    assert [item.title for item in items] == [
        "EU adopts landmark AI rules", "US states weigh their own AI statutes",
    ]
    assert all(item.timestamp is not None for item in items)
    assert [item.rank for item in items] == [1, 2]


def test_no_key_means_no_call_and_no_result(monkeypatch):
    """A missing key is configuration, not a search miss.

    Checked on a keyed provider; GDELT has no key to be missing, which is
    the point of it being the floor.
    """
    from tools.providers import news_nytimes

    called = []
    monkeypatch.setattr(news_nytimes, "http_fetch", lambda url: called.append(url))
    monkeypatch.setattr(news_nytimes, "api_key", lambda: None)

    assert news_nytimes.lookup("latest news about AI regulation") == []
    assert not called, "the endpoint was called without a key"


# ======================================================
# C. Exchange rate
# ======================================================
def test_an_fx_question_reaches_the_rate_provider(providers):
    result = ws.web_search("current EUR/USD exchange rate")

    assert providers["fx"], "the FX provider was never called"
    assert "/EUR" in providers["fx"][0], "the base currency is wrong"

    item = result["items"][0]
    assert item["provenance"] == "exchange-rate-api"
    assert "1 EUR = 1.089 USD" in item["snippet"]
    assert item["timestamp"]


@pytest.mark.parametrize("query,pair", [
    ("current EUR/USD exchange rate", ("EUR", "USD")),
    ("what is the exchange rate from euros to dollars", ("EUR", "USD")),
    ("convert pounds to yen", ("GBP", "JPY")),
])
def test_the_pair_is_read_in_the_order_it_was_asked(query, pair):
    """Backwards would return a real, precise, inverted number."""
    assert exchange_rate_api.extract_pair(query) == pair


def test_a_question_with_one_currency_is_not_an_fx_question():
    assert exchange_rate_api.extract_pair("how many dollars is that") is None
    assert exchange_rate_api.lookup("what is the dollar doing") == []


# ======================================================
# D. Hard miss
# ======================================================
def test_a_query_no_provider_can_serve_is_a_hard_miss(providers):
    result = ws.web_search("asdfghjklqwertyuiop")

    assert result["items"] == []
    assert result["summary"] == ws.NO_RESULTS
    assert result["heading"] is None
    assert result["source_url"] is None
    assert not any(providers.values()), "a provider was called for a query it cannot serve"


def test_a_hard_miss_produces_no_usable_evidence(providers):
    """The whole point: nothing-found must not look like something-found.

    The old backend returned its empty envelope as the summary, which read
    as a finding all the way down. This asserts the replacement does not.
    """
    from backend.core import evidence_routing as er

    result = ws.web_search("asdfghjklqwertyuiop")
    assert normalize_tool_value("web_search", {"raw": result, "reply": result["summary"]}) == []

    line = f"web_search (step1): {result['summary']}"
    assert er.normalize_tool_line(line).usable is False


def test_a_provider_that_fails_does_not_lose_the_others(providers, monkeypatch):
    """One broken backend must not cost the answers the rest found."""
    def explode(url):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(news_nytimes, "http_fetch", explode)
    result = ws.web_search("stock price of Microsoft and the latest news")

    assert result["items"], "a failing provider took the whole search down"
    assert result["items"][0]["provenance"] == "yahoo-finance"


def test_a_result_with_no_text_is_dropped(providers, monkeypatch):
    monkeypatch.setattr(yahoo_finance, "lookup", lambda q: [
        {"title": "Empty", "snippet": "   ", "url": None,
         "timestamp": None, "provenance": "yahoo-finance", "rank": 1},
    ])
    assert ws.web_search("stock price of Microsoft")["items"] == []


def test_the_tool_never_raises(providers, monkeypatch):
    monkeypatch.setattr(yahoo_finance, "lookup", lambda q: (_ for _ in ()).throw(ValueError("x")))
    result = ws.web_search("stock price of Microsoft")
    assert result["status"] == "ok"


# ======================================================
# E. Two providers on one query
# ======================================================
def test_a_query_both_can_serve_returns_both(providers):
    result = ws.web_search("Microsoft stock price and the latest news")

    provenances = {item["provenance"] for item in result["items"]}
    assert provenances == {"yahoo-finance", "nytimes"}
    assert result["providers"] == ["nytimes", "yahoo-finance"]


def test_the_merged_order_is_deterministic(providers):
    query = "Microsoft stock price and the latest news"
    first = [(i["provenance"], i["rank"], i["snippet"]) for i in ws.web_search(query)["items"]]

    for _ in range(5):
        again = [(i["provenance"], i["rank"], i["snippet"]) for i in ws.web_search(query)["items"]]
        assert again == first


def test_ranks_are_unique_across_providers(providers):
    items = ws.web_search("Microsoft stock price and the latest news")["items"]
    ranks = [item["rank"] for item in items]

    assert ranks == list(range(1, len(items) + 1)), "ranks collide or skip"


def test_the_quote_leads_the_headlines(providers):
    """A quote answers "what is it trading at"; an article is context."""
    items = ws.web_search("Microsoft stock price and the latest news")["items"]
    assert items[0]["provenance"] == "yahoo-finance"


def test_both_providers_survive_normalization(providers):
    items = normalized_for(ws.web_search("Microsoft stock price and the latest news"))

    assert {item.source for item in items} == {"yahoo-finance", "nytimes"}
    assert any("496.37" in (item.snippet or "") for item in items)
    # The Times carries a real abstract, so the snippet is article text
    # rather than the headline GDELT could only repeat back.
    assert any("rules take effect" in (item.snippet or "") for item in items)
    assert any("EU adopts landmark AI rules" == item.title for item in items)


# ======================================================
# F. Nothing outside the tool moved
# ======================================================
FORBIDDEN = [
    "backend/core/provider_router.py",
    "backend/core/safety_manager.py",
    "backend/core/auto_selector.py",
    "backend/core/weather_fusion.py",
    "backend/core/weather_nl.py",
    "backend/core/conversation_manager.py",
    "backend/core/turn_orchestrator.py",
    "backend/planning/plan_builder.py",
    "backend/aria_synthesis/synthesis_engine.py",
    "backend/aria_synthesis/synthesis_prompt.py",
    "backend/aria_synthesis/bundle_builder.py",
]


@pytest.mark.parametrize("path", FORBIDDEN)
def test_the_forbidden_subsystems_are_untouched(path):
    """Asserted against git, not against memory.

    This arc replaces a search backend. Anything it changed in routing,
    planning, bundling or synthesis would be a change nobody asked for,
    reviewed, or would think to look for in a commit titled "search
    provider".

    search_intent.py is deliberately not on this list: Arc 239 changed it
    and is still uncommitted, so it differs from HEAD for a reason that
    has nothing to do with this arc. evidence_routing.py is not here
    either -- this arc adds one placeholder string to it, called out in
    the report rather than hidden behind a passing test.
    """
    repo = pathlib.Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", path],
        cwd=repo, capture_output=True,
    )
    if result.returncode == 128:
        pytest.skip("not a git checkout")
    assert result.returncode == 0, f"{path} differs from HEAD"

# ======================================================
# What the live run taught us
# ======================================================
# Reported: "msft stock price" answered "I could not retrieve current data
# for this query" in 90ms -- too fast to have made a request. Three
# separate defects, each of which alone was enough:
#
#   1. http_fetch sent no User-Agent, and Yahoo's edge answers an
#      anonymous client with HTTP 429 "Edge: Too Many Requests". The same
#      request with a User-Agent returns 200 immediately, so the wording
#      is misleading -- it is a refusal, not a rate limit.
#   2. http_fetch reported status "ok" for that 429, handing the refusal
#      body downstream as though it were data.
#   3. /v7/finance/quote now returns 401 even with a User-Agent; it needs
#      an authenticated crumb. /v8/finance/chart does not.
def test_an_http_error_is_not_a_successful_fetch():
    """A 429 is not data, however well-formed its body is."""
    import tools.http_fetch as hf

    class Refused:
        status_code = 429
        headers = {"Content-Type": "text/plain"}
        text = "Edge: Too Many Requests"

    original = hf.requests.get
    hf.requests.get = lambda url, **kw: Refused()
    try:
        result = hf.http_fetch("https://example.com/x")
    finally:
        hf.requests.get = original

    assert result["status"] == "error"
    assert result["code"] == 429
    assert result["data"] is None


def test_a_user_agent_is_sent():
    """Without one, Yahoo refuses outright. Pinned so it is not dropped."""
    import tools.http_fetch as hf

    seen = {}

    class Ok:
        status_code = 200
        headers = {"Content-Type": "application/json"}

        def json(self):
            return {"ok": True}

    original = hf.requests.get

    def capture(url, **kwargs):
        seen.update(kwargs.get("headers") or {})
        return Ok()

    hf.requests.get = capture
    try:
        hf.http_fetch("https://example.com/x")
    finally:
        hf.requests.get = original

    assert "User-Agent" in seen
    assert seen["User-Agent"].strip()


def test_the_provider_refuses_a_refusal(monkeypatch):
    """The 429 body must not become a quote.

    This is the half that already worked: the provider validated the shape
    and returned [], which is why the failure showed up as an honest "no
    results" rather than as a fabricated price.
    """
    monkeypatch.setattr(yahoo_finance, "http_fetch", lambda url: {
        "status": "error", "code": 429, "data": None,
        "error": "HTTP 429: Edge: Too Many Requests",
    })
    assert yahoo_finance.lookup("msft stock price") == []


def test_the_chart_endpoint_is_the_one_called(providers):
    """v7/quote is 401-only now; v8/chart still serves the numbers."""
    ws.web_search("msft stock price")

    assert providers["yahoo"], "no request was made"
    url = providers["yahoo"][0]
    assert "/v8/finance/chart/" in url
    assert "/v7/finance/quote" not in url


def test_the_days_move_is_derived_from_the_previous_close(providers):
    """v8/chart carries the previous close, not the move.

    Derived from two numbers in the same payload -- not guessed, and not
    printed at all when either is missing.
    """
    snippet = ws.web_search("msft stock price")["items"][0]["snippet"]

    # 496.37 - 491.71 = +4.66, which is +0.95%
    assert "+4.66" in snippet
    assert "+0.95%" in snippet


def test_no_market_cap_is_invented(providers):
    """v8/chart does not carry it, so it must not appear."""
    snippet = ws.web_search("msft stock price")["items"][0]["snippet"]
    assert "Market cap" not in snippet



# ======================================================
# The collision that made all of this invisible
# ======================================================
def test_the_tools_package_is_regular_not_namespace():
    """Why "I could not retrieve current data" survived three arcs.

    There are two packages named `tools`: this repository's root one, and
    backend/tools/ (the Phase 9 planning layer). Without an __init__.py at
    the root, the root one is a PEP 420 namespace package -- and Python's
    finder does not stop at the first match. A directory with no
    __init__.py is recorded as a namespace *portion* and the scan
    continues; any regular package found later wins.

    So `import tools` resolved to backend/tools/ inside the running
    server, even though ws_server.py puts the repository root at
    sys.path[0]. `from tools.web_search import web_search` raised
    ModuleNotFoundError, the tool failed in under a millisecond, and the
    turn reported no evidence -- with no request ever reaching the
    network.

    It never reproduced under pytest, because the test process runs from
    the repository root with backend/ absent from sys.path. That is the
    part worth remembering: the tests were green and the product was
    broken, and the difference was sys.path.
    """
    import tools

    root = pathlib.Path(__file__).resolve().parents[2]
    assert tools.__file__, "tools is still a namespace package"
    assert pathlib.Path(tools.__file__).parent == root / "tools", (
        f"import tools resolved to {tools.__file__}, not the repository root"
    )


def test_the_search_tool_imports_the_way_the_server_imports_it():
    """The failing import, reproduced in the server's own path layout."""
    root = pathlib.Path(__file__).resolve().parents[2]
    probe = (
        "import sys, os;"
        f"sys.path.insert(0, {str(root / 'backend')!r});"
        f"sys.path.insert(0, {str(root)!r});"
        "from tools.web_search import web_search;"
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=root, capture_output=True, text=True,
    )
    assert "ok" in result.stdout, (
        f"the server's import layout cannot reach tools.web_search:\n{result.stderr[-400:]}"
    )
