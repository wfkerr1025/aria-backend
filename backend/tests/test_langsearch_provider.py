# backend/tests/test_langsearch_provider.py
#
# LangSearch: the one general-web provider.
#
# Brave and Tavily are gone -- two keys and two response shapes for one
# job -- and SerpAPI was never built. What replaced them is a single free
# provider that also backs up the news path when no news key is present.
#
# The two facts worth pinning here are the ones a future edit is most
# likely to break:
#
#   * the fallback is conditional. A news question a publisher answered
#     must not also collect a page of search links. Making LangSearch
#     just another entry in the provider list would break this silently:
#     the answers would still be correct, only diluted, and no assertion
#     about correctness would notice.
#
#   * a finance question is declined by the provider itself, not gated by
#     the dispatcher. A quote that failed to arrive is a missing quote,
#     and must never be replaced by an article about the price.
#
# Nothing in this file touches the network.

from __future__ import annotations

import pytest

from tools import providers
from tools import web_search as ws
from tools.providers import web_langsearch

QUERY = "pytest 8.2 release notes"

# The documented response shape: a Bing-style webPages.value list.
PAYLOAD = {"status": "ok", "data": {"data": {"webPages": {"value": [
    # displayUrl arrives as a whole URL, not the bare host the name
    # suggests. An earlier fixture here used a bare host and agreed with
    # a _domain() that split on "/" -- so both were wrong together and
    # the suite reported a publisher of "https:" as passing.
    {"name": "pytest 8.2 release notes",
     "url": "https://docs.pytest.org/en/stable/changelog.html",
     "displayUrl": "https://docs.pytest.org/en/stable/changelog.html",
     "snippet": "pytest 8.2 adds --strict-config and drops Python 3.7."},
    {"name": "What is new in pytest 8.2",
     "url": "https://blog.example.com/pytest-82",
     "snippet": "A walkthrough of the 8.2 changes."},
]}}}}


def stub(monkeypatch, module, payload):
    """Record every http_fetch call and answer with `payload`."""
    calls = []

    def fake(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return payload

    monkeypatch.setattr(module, "http_fetch", fake)
    return calls


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr(web_langsearch, "api_key", lambda: "test-key")


@pytest.fixture
def silent_specialists(monkeypatch):
    """Every specialist finds nothing, so the fallback tier is reached."""
    for module in providers.PRIMARY_PROVIDERS:
        monkeypatch.setattr(module, "lookup", lambda query: [])


# ------------------------------------------------------
# The provider
# ------------------------------------------------------

def test_langsearch_returns_normalized_results(monkeypatch, keyed):
    stub(monkeypatch, web_langsearch, PAYLOAD)

    items = web_langsearch.lookup(QUERY)

    assert len(items) == 2
    assert [item["rank"] for item in items] == [1, 2]
    assert all(item["provenance"] == "langsearch" for item in items)


def test_langsearch_names_the_publisher_and_keeps_the_link(monkeypatch, keyed):
    stub(monkeypatch, web_langsearch, PAYLOAD)

    item = web_langsearch.lookup(QUERY)[0]

    assert item["title"] == "pytest 8.2 release notes"
    assert item["url"] == "https://docs.pytest.org/en/stable/changelog.html"
    assert item["source"] == "docs.pytest.org"
    # The publisher is named in the snippet as well: "whose page this is"
    # and "which engine found it" are different facts, and the second one
    # is not an attribution a reader can check.
    assert "docs.pytest.org" in item["snippet"]


def test_langsearch_falls_back_to_the_host_when_no_display_url_is_sent(monkeypatch, keyed):
    stub(monkeypatch, web_langsearch, PAYLOAD)

    assert web_langsearch.lookup(QUERY)[1]["source"] == "blog.example.com"


@pytest.mark.parametrize("display_url", [
    "https://docs.pytest.org/en/stable/changelog.html",
    "http://docs.pytest.org/en/stable/changelog.html",
    "docs.pytest.org/en/stable/changelog.html",
    "docs.pytest.org",
    "https://docs.pytest.org/search?q=strict",
])
def test_the_publisher_is_a_domain_whatever_form_display_url_takes(
        monkeypatch, keyed, display_url):
    stub(monkeypatch, web_langsearch, {"status": "ok", "data": {"data": {"webPages": {
        "value": [{"name": "pytest 8.2 release notes",
                   "url": "https://docs.pytest.org/en/stable/changelog.html",
                   "displayUrl": display_url, "snippet": "Body text."}],
    }}}})

    # "https:" is not a publisher, and it would have been shown to the
    # reader as one next to a link they could check it against.
    assert web_langsearch.lookup(QUERY)[0]["source"] == "docs.pytest.org"


def test_a_split_version_number_is_put_back_together(monkeypatch, keyed):
    # Exactly as the live API sends it: punctuation spaced out as its own
    # token. Left alone this reaches the model as three numbers where the
    # page had one.
    stub(monkeypatch, web_langsearch, {"status": "ok", "data": {"data": {"webPages": {
        "value": [{"name": "Topics tagged release",
                   "url": "https://discuss.python.org/tag/release",
                   "snippet": "python 3 . 12 . 14 , 3 . 11 . 16 and 3 . 10 . 21 "
                              "are now available ! the 3.15 branch is locked"}],
    }}}})

    snippet = web_langsearch.lookup(QUERY)[0]["snippet"]

    assert "3.12.14" in snippet and "3.11.16" in snippet and "3.10.21" in snippet
    assert "3.15" in snippet


def test_a_split_version_number_in_the_title_is_put_back_together(monkeypatch, keyed):
    stub(monkeypatch, web_langsearch, {"status": "ok", "data": {"data": {"webPages": {
        "value": [{"name": "python 3 . 14 . 7 release",
                   "url": "https://www.python.org/downloads/",
                   "snippet": "Body text."}],
    }}}})

    # The title needs the same repair as the snippet, and needs it in the
    # same place -- after trim(), whose cleanup is what creates the form
    # the repair matches.
    assert web_langsearch.lookup(QUERY)[0]["title"] == "python 3.14.7 release"


def test_a_failed_decode_marker_is_not_shown_to_the_reader(monkeypatch, keyed):
    stub(monkeypatch, web_langsearch, {"status": "ok", "data": {"data": {"webPages": {
        "value": [{"name": "Changelog � pytest documentation",
                   "url": "https://docs.pytest.org/en/stable/changelog.html",
                   "snippet": "1 summary � release highlights 4"}],
    }}}})

    item = web_langsearch.lookup(QUERY)[0]

    # It arrives that way in LangSearch's own JSON, so there is nothing
    # to recover -- but a black diamond on screen, and a "decoding
    # failed" character in the evidence, are both worse than its absence.
    assert "�" not in item["title"]
    assert "�" not in item["snippet"]
    assert item["title"] == "Changelog pytest documentation"


def test_sentence_punctuation_is_left_alone(monkeypatch, keyed):
    stub(monkeypatch, web_langsearch, {"status": "ok", "data": {"data": {"webPages": {
        "value": [{"name": "A page", "url": "https://example.com/x",
                   "snippet": "The release shipped. Next came the docs."}],
    }}}})

    # Only digits are rejoined; a wider rule would produce "shipped.Next".
    assert "shipped. Next" in web_langsearch.lookup(QUERY)[0]["snippet"]


def test_a_human_site_name_is_not_used_as_the_domain(monkeypatch, keyed):
    stub(monkeypatch, web_langsearch, {"status": "ok", "data": {"data": {"webPages": {
        "value": [{"name": "pytest 8.2 release notes",
                   "url": "https://docs.pytest.org/en/stable/changelog.html",
                   "siteName": "pytest documentation", "snippet": "Body text."}],
    }}}})

    # siteName is a display name, not something a reader can check the
    # link against. The domain is the attribution.
    assert web_langsearch.lookup(QUERY)[0]["source"] == "docs.pytest.org"


def test_langsearch_dates_nothing_the_provider_did_not_date(monkeypatch, keyed):
    stub(monkeypatch, web_langsearch, PAYLOAD)

    # An undated page stamped with the moment it was fetched carries a
    # date it never had, and a reader cannot tell the difference.
    assert all(item["published_at"] is None for item in web_langsearch.lookup(QUERY))


def test_langsearch_keeps_a_timestamp_the_provider_did_send(monkeypatch, keyed):
    stub(monkeypatch, web_langsearch, {"status": "ok", "data": {"data": {"webPages": {
        "value": [{"name": "A dated page", "url": "https://example.com/x",
                   "snippet": "Body text.",
                   "datePublished": "2026-08-20T09:15:00+00:00"}],
    }}}})

    assert web_langsearch.lookup(QUERY)[0]["published_at"].startswith("2026-08-20T09:15")


def test_langsearch_sends_the_key_as_a_bearer_token(monkeypatch, keyed):
    calls = stub(monkeypatch, web_langsearch, PAYLOAD)

    web_langsearch.lookup(QUERY)

    assert calls[0]["headers"]["Authorization"] == "Bearer test-key"
    assert calls[0]["json_body"]["query"] == QUERY


def test_langsearch_posts_to_the_documented_endpoint(monkeypatch, keyed):
    calls = stub(monkeypatch, web_langsearch, PAYLOAD)

    web_langsearch.lookup(QUERY)

    # Pinned so a silent change to the URL shows up here rather than as
    # a provider that quietly stops finding anything. /v1/search answers
    # 404; this is the path that serves, confirmed live.
    assert calls[0]["url"] == "https://api.langsearch.com/v1/web-search"


def test_langsearch_reads_the_documented_response_shape(monkeypatch, keyed):
    # The real envelope, verbatim: code / log_id / msg at the top, the
    # results under data.webPages.value.
    stub(monkeypatch, web_langsearch, {"status": "ok", "data": {
        "code": 200, "log_id": "abc123", "msg": None,
        "data": {
            "_type": "SearchResponse",
            "queryContext": {"originalQuery": QUERY},
            "webPages": {
                "webSearchUrl": "https://langsearch.com/search?q=pytest",
                "totalEstimatedMatches": 100,
                "value": [{
                    "id": "https://api.langsearch.com/v1/documents/1",
                    "name": "pytest 8.2 release notes",
                    "url": "https://docs.pytest.org/en/stable/changelog.html",
                    "displayUrl": "https://docs.pytest.org/en/stable/changelog.html",
                    "snippet": "pytest 8.2 adds --strict-config.",
                    "summary": "A much longer provider-written summary.",
                    "siteName": "pytest documentation",
                    "siteIcon": "https://docs.pytest.org/favicon.ico",
                    "datePublished": "2026-04-27T00:00:00Z",
                    "dateLastCrawled": "2026-08-26T11:04:00Z",
                }],
                "someResultsRemoved": False,
            },
        },
    }})

    item = web_langsearch.lookup(QUERY)[0]

    assert item["title"] == "pytest 8.2 release notes"
    assert item["url"] == "https://docs.pytest.org/en/stable/changelog.html"
    assert item["published_at"].startswith("2026-04-27")
    # The snippet, not the provider's `summary` field: a summary written
    # elsewhere is not traceable to the page it claims to describe.
    assert "strict-config" in item["snippet"]
    assert "provider-written" not in item["snippet"]


@pytest.mark.parametrize("code", [403, 401, "500"])
def test_langsearch_treats_an_in_body_error_code_as_a_failure(monkeypatch, keyed, code):
    stub(monkeypatch, web_langsearch, {"status": "ok", "data": {
        "code": code, "msg": "invalid api key",
        "data": {"webPages": {"value": [
            {"name": "leftover", "url": "https://example.com/x", "snippet": "Body."},
        ]}},
    }})

    # A rejected request that arrived over a 200 is still a rejected
    # request. Reading past the code would report it as "found nothing".
    assert web_langsearch.lookup(QUERY) == []


def test_the_raw_logging_survives_whatever_comes_back(monkeypatch, keyed):
    class Unserializable:
        pass

    for payload in (None, "a string", [1, 2, 3],
                    {"status": "error", "code": 404, "error": "HTTP 404: Not Found",
                     "data": None},
                    {"status": "ok", "data": {"obj": Unserializable()}}):
        stub(monkeypatch, web_langsearch, payload)
        # The diagnostic must not be the thing that breaks the turn.
        assert web_langsearch.lookup(QUERY) == []


def test_langsearch_does_not_ask_for_a_written_answer(monkeypatch, keyed):
    calls = stub(monkeypatch, web_langsearch, PAYLOAD)

    web_langsearch.lookup(QUERY)

    body = calls[0]["json_body"]
    # A summary written by the search provider would arrive as evidence
    # without being traceable to any one page -- exactly the shape the
    # evidence pipeline exists to reject.
    assert body["summary"] is False
    assert body["include_images"] is False
    assert body["include_videos"] is False
    assert body["rerank"] is True


def test_langsearch_without_a_key_calls_nothing(monkeypatch):
    monkeypatch.setattr(web_langsearch, "api_key", lambda: None)
    calls = stub(monkeypatch, web_langsearch, PAYLOAD)

    assert web_langsearch.lookup(QUERY) == []
    # A missing key is a configuration state, not a search failure. The
    # endpoint would have answered 401 -- a request nobody needed to make.
    assert calls == []


@pytest.mark.parametrize("response", [
    {"status": "error", "code": 401, "data": None},
    {"status": "ok", "data": None},
    {"status": "ok", "data": {"data": {"webPages": {"value": []}}}},
    {"status": "ok", "data": {"unexpected": "shape"}},
    None,
])
def test_langsearch_handles_a_failed_call_as_an_empty_result(monkeypatch, keyed, response):
    stub(monkeypatch, web_langsearch, response)

    assert web_langsearch.lookup(QUERY) == []


def test_langsearch_survives_a_raising_transport(monkeypatch, keyed):
    def boom(url, **kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(web_langsearch, "http_fetch", boom)

    assert web_langsearch.lookup(QUERY) == []


def test_langsearch_skips_a_row_missing_a_title_or_a_url(monkeypatch, keyed):
    stub(monkeypatch, web_langsearch, {"status": "ok", "data": {"data": {"webPages": {
        "value": [
            {"url": "https://example.com/no-title", "snippet": "Body."},
            {"name": "No link", "snippet": "Body."},
            {"name": "Complete", "url": "https://example.com/ok", "snippet": "Body."},
        ],
    }}}})

    items = web_langsearch.lookup(QUERY)

    # Skipped, not reconstructed. A result is a claim plus somewhere to
    # check it; half of one is not the other half's evidence.
    assert [item["title"] for item in items] == ["Complete"]


# ------------------------------------------------------
# Applicability
# ------------------------------------------------------

@pytest.mark.parametrize("query", [
    "pytest 8.2 release notes",
    "how do I write a Unity editor window",
    "what is a coroutine",
    "latest news about AI regulation",
])
def test_langsearch_claims_general_and_news_questions(query):
    assert web_langsearch.applies_to(query)


@pytest.mark.parametrize("query", [
    "msft stock price",
    "what is Apple trading at",
    "current EUR/USD exchange rate",
    "convert 100 gbp to usd",
    "weather in Toronto",
    "what is the temperature outside",
    "",
    "   ",
])
def test_langsearch_declines_what_a_specialist_answers_better(query):
    assert not web_langsearch.applies_to(query)


def test_a_finance_question_never_reaches_langsearch(monkeypatch):
    monkeypatch.setattr(web_langsearch, "api_key", lambda: "test-key")
    calls = stub(monkeypatch, web_langsearch, PAYLOAD)

    # Declined before the call, not filtered after it: an article about
    # the price is a worse answer to a question that had a better one.
    assert web_langsearch.lookup("msft stock price") == []
    assert calls == []


# ------------------------------------------------------
# The dispatcher: one tier below the specialists
# ------------------------------------------------------

def test_the_web_tier_answers_when_no_specialist_did(monkeypatch, keyed, silent_specialists):
    stub(monkeypatch, web_langsearch, PAYLOAD)

    result = ws.web_search(QUERY)

    assert result["providers"] == ["langsearch"]
    assert result["heading"] == "pytest 8.2 release notes"
    assert result["source_url"] == "https://docs.pytest.org/en/stable/changelog.html"


def test_news_with_no_key_falls_back_to_the_web(monkeypatch, keyed, silent_specialists):
    stub(monkeypatch, web_langsearch, {"status": "ok", "data": {"data": {"webPages": {
        "value": [{"name": "EU adopts landmark AI rules",
                   "url": "https://example.com/eu-ai-act",
                   "displayUrl": "reuters.com",
                   "snippet": "The rules take effect next year."}],
    }}}})

    result = ws.web_search("latest news about AI regulation")

    # Without this tier the question failed outright for want of a key.
    assert result["summary"] != ws.NO_RESULTS
    assert result["providers"] == ["langsearch"]


def test_news_a_publisher_answered_does_not_also_hit_the_web(monkeypatch, keyed):
    from tools.providers import news_nytimes

    for module in providers.PRIMARY_PROVIDERS:
        monkeypatch.setattr(module, "lookup", lambda query: [])
    monkeypatch.setattr(news_nytimes, "lookup", lambda query: [{
        "title": "EU adopts landmark AI rules",
        "snippet": "EU adopts landmark AI rules (reuters.com)",
        "url": "https://example.com/eu-ai-act",
        "timestamp": "2026-08-26T09:15:00+00:00",
        "provenance": "nytimes",
        "rank": 1,
    }])
    calls = stub(monkeypatch, web_langsearch, PAYLOAD)

    result = ws.web_search("latest news about AI regulation")

    # A dated abstract from a named publisher is better evidence than a
    # link about the same story. Carrying both would spend the answer's
    # budget twice on one fact.
    assert result["providers"] == ["nytimes"]
    assert calls == []


def test_a_failed_quote_is_a_miss_not_an_article_about_the_price(
        monkeypatch, keyed, silent_specialists):
    calls = stub(monkeypatch, web_langsearch, PAYLOAD)

    result = ws.web_search("msft stock price")

    assert result["summary"] == ws.NO_RESULTS
    assert result["items"] == []
    assert calls == []


def test_every_provider_empty_is_still_a_hard_miss(monkeypatch, keyed, silent_specialists):
    stub(monkeypatch, web_langsearch,
         {"status": "ok", "data": {"data": {"webPages": {"value": []}}}})

    result = ws.web_search(QUERY)

    assert result["status"] == "ok"
    assert result["summary"] == ws.NO_RESULTS
    assert result["providers"] == []
    assert result["heading"] is None


def test_a_raising_web_provider_does_not_raise_out_of_the_tool(
        monkeypatch, keyed, silent_specialists):
    def boom(query):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(web_langsearch, "lookup", boom)

    assert ws.web_search(QUERY)["summary"] == ws.NO_RESULTS


# ------------------------------------------------------
# The registry
# ------------------------------------------------------

def test_the_superseded_web_providers_are_gone():
    names = [getattr(p, "PROVENANCE", p.__name__) for p in providers.PROVIDERS]

    assert "langsearch" in names
    for retired in ("brave", "tavily", "serpapi"):
        assert retired not in names


def test_the_web_tier_sits_below_every_specialist():
    assert providers.FALLBACK_PROVIDERS == (web_langsearch,)
    assert web_langsearch not in providers.PRIMARY_PROVIDERS
    assert providers.PROVIDERS[-1] is web_langsearch


def test_langsearch_normalizes_for_the_evidence_layer(monkeypatch, keyed, silent_specialists):
    from backend.tools.tool_executor import normalize_tool_value

    stub(monkeypatch, web_langsearch, PAYLOAD)

    result = ws.web_search(QUERY)
    # The executor's envelope, which is the shape the normalizer is
    # handed in production -- the tool's own dict rides under "raw".
    # A list of findings comes back, not an envelope: empty means "ran
    # and found nothing", which the evidence layer must tell apart from
    # failure.
    normalized = normalize_tool_value(
        "web_search", {"raw": result, "reply": result.get("summary")})

    assert len(normalized) == 2
    assert {item.source for item in normalized} == {"langsearch"}
    assert all(item.url for item in normalized)
    assert normalized[0].title == "pytest 8.2 release notes"
