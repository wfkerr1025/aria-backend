# backend/tests/test_news_providers.py
#
# The free news providers, replacing NewsAPI.
#
# NewsAPI's free tier is development-only: localhost origins, 100 requests
# a day, and no production use permitted. A provider that cannot legally
# run in the product is not a provider, so it is gone.
#
# What replaces it:
#
#     nytimes     key, free tier      abstract + headline + timestamp
#     mediastack  key, free tier      description + headline + timestamp
#
# GDELT was here too, and was the point of the design: keyless, always
# on, the floor the other two sat above. api.gdeltproject.org would not
# answer -- and at the old shared 10s request timeout it consumed
# web_search's whole tool budget, so the turn gave up 2.1 seconds before
# the LangSearch fallback returned ten good results. A floor that cannot
# be reached is not a floor.
#
# So both remaining providers need a key, and with neither configured
# this tier is empty. That is not a gap: LangSearch sits behind it
# precisely so a news question does not fail for want of a key, which
# test_langsearch_provider covers.
#
# Every payload here is stubbed. A suite that reached the live APIs would
# depend on three services being up and on whoever runs it holding two
# keys -- and would fail for reasons that have nothing to do with the code.

from __future__ import annotations

import pytest

from tools import web_search as ws
from tools.providers import news_mediastack, news_nytimes
from tools.providers.news_common import iso_timestamp, trim


# ------------------------------------------------------
# Payloads in the shape each API documents
# ------------------------------------------------------
MEDIASTACK_PAYLOAD = {"status": "ok", "data": {"pagination": {}, "data": [
    {"title": "Regulators circle AI labs", "description": "Oversight is tightening.",
     "url": "https://example.com/regulators", "source": "The Verge",
     "published_at": "2026-08-26T07:00:00+00:00"},
]}}

NYTIMES_PAYLOAD = {"status": "ok", "data": {"response": {"docs": [
    {"headline": {"main": "Inside the AI rulebook"},
     "abstract": "A look at how the new statute was drafted.",
     "lead_paragraph": "Lawmakers spent two years on it.",
     "web_url": "https://nytimes.com/ai-rulebook",
     "pub_date": "2026-08-26T10:30:00+0000"},
]}}}

QUERY = "latest news about AI regulation"


@pytest.fixture(autouse=True)
def no_ambient_keys(monkeypatch):
    """Keys are set per test, never inherited from the machine."""
    for var in ("MEDIASTACK_KEY", "NYTIMES_KEY", "MEDIASTACK_ALLOW_HTTP"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(news_mediastack, "api_key", lambda: None)
    monkeypatch.setattr(news_nytimes, "api_key", lambda: None)


def stub(monkeypatch, module, payload):
    calls = []

    # **kwargs, not a bare url: http_fetch takes a per-call
    # timeout now, and a stub narrower than the real signature
    # fails on the argument rather than testing anything.
    def fetch(url, **kwargs):
        calls.append(url)
        return payload

    monkeypatch.setattr(module, "http_fetch", fetch)
    return calls


# ======================================================
# A. GDELT -- no key, always available
# ======================================================






# ======================================================
# B. Mediastack -- optional
# ======================================================
def test_mediastack_returns_items_when_key_present(monkeypatch):
    monkeypatch.setattr(news_mediastack, "api_key", lambda: "test-key")
    stub(monkeypatch, news_mediastack, MEDIASTACK_PAYLOAD)

    items = news_mediastack.lookup(QUERY)
    assert len(items) == 1
    assert items[0]["provenance"] == "mediastack"
    assert items[0]["title"] == "Regulators circle AI labs"
    assert "Oversight is tightening." in items[0]["snippet"]
    assert "The Verge" in items[0]["snippet"]
    assert items[0]["published_at"].startswith("2026-08-26T07:00:00")


def test_mediastack_returns_empty_list_without_key(monkeypatch):
    """A missing key is configuration, not a search failure.

    And nothing is called: an endpoint hit without a key would answer 401,
    which is a request nobody needed to make.
    """
    called = stub(monkeypatch, news_mediastack, MEDIASTACK_PAYLOAD)

    assert news_mediastack.lookup(QUERY) == []
    assert not called


def test_mediastack_does_not_downgrade_to_plaintext(monkeypatch):
    """Its free tier serves HTTP only, and this does not quietly accept it.

    Falling back would put the API key and the user's query on the wire in
    the clear. Silently downgrading someone's transport security is not a
    call a search provider gets to make, so it stays HTTPS unless the
    operator opts in explicitly.
    """
    monkeypatch.setattr(news_mediastack, "api_key", lambda: "test-key")
    called = stub(monkeypatch, news_mediastack, MEDIASTACK_PAYLOAD)

    news_mediastack.lookup(QUERY)
    assert called[0].startswith("https://")

    monkeypatch.setenv("MEDIASTACK_ALLOW_HTTP", "1")
    called.clear()
    news_mediastack.lookup(QUERY)
    assert called[0].startswith("http://"), "the explicit opt-in was ignored"


def test_mediastack_reports_an_api_error_as_no_results(monkeypatch):
    monkeypatch.setattr(news_mediastack, "api_key", lambda: "test-key")
    stub(monkeypatch, news_mediastack, {"status": "ok", "data": {
        "error": {"code": "usage_limit_reached", "message": "quota"}}})

    assert news_mediastack.lookup(QUERY) == []


# ======================================================
# C. NYTimes -- optional
# ======================================================
def test_nytimes_returns_items_when_key_present(monkeypatch):
    monkeypatch.setattr(news_nytimes, "api_key", lambda: "test-key")
    stub(monkeypatch, news_nytimes, NYTIMES_PAYLOAD)

    items = news_nytimes.lookup(QUERY)
    assert len(items) == 1
    assert items[0]["provenance"] == "nytimes"
    assert items[0]["title"] == "Inside the AI rulebook"
    assert items[0]["source"] == "The New York Times"
    assert "how the new statute was drafted" in items[0]["snippet"]
    assert items[0]["published_at"].startswith("2026-08-26T10:30:00")


def test_nytimes_returns_empty_list_without_key(monkeypatch):
    called = stub(monkeypatch, news_nytimes, NYTIMES_PAYLOAD)

    assert news_nytimes.lookup(QUERY) == []
    assert not called


def test_nytimes_falls_back_through_abstract_then_lead_then_headline(monkeypatch):
    """A story with no body text is still a story."""
    monkeypatch.setattr(news_nytimes, "api_key", lambda: "test-key")

    stub(monkeypatch, news_nytimes, {"status": "ok", "data": {"response": {"docs": [
        {"headline": {"main": "Only a headline"}, "web_url": "https://nytimes.com/x",
         "pub_date": "2026-08-26T10:30:00+0000"},
    ]}}})
    item = news_nytimes.lookup(QUERY)[0]

    assert item["snippet"] == "Only a headline"
    assert item["title"] == "Only a headline"


def test_nytimes_skips_a_doc_with_no_headline(monkeypatch):
    monkeypatch.setattr(news_nytimes, "api_key", lambda: "test-key")
    stub(monkeypatch, news_nytimes, {"status": "ok", "data": {"response": {"docs": [
        {"web_url": "https://nytimes.com/x", "abstract": "no headline"},
    ]}}})

    assert news_nytimes.lookup(QUERY) == []


# ======================================================
# Shared normalization
# ======================================================
def test_markup_is_stripped_from_a_description():
    """A snippet carrying raw tags reaches the model as text to parse,
    and reaches the user, on the raw-evidence path, as tags on screen."""
    assert trim("<p>Oversight is <b>tightening</b>.</p>", 300) == "Oversight is tightening."
    assert trim("Tom &amp; Jerry &quot;news&quot;", 300) == 'Tom & Jerry "news"'


def test_a_long_snippet_is_cut_at_a_word_boundary():
    result = trim("word " * 200, 100)
    assert len(result) <= 101
    assert not result.rstrip("…").endswith("wor")


def test_an_unparseable_timestamp_is_none_not_now():
    """Stamping an undated story with "now" dates a week-old article to
    this minute, and a reader cannot tell the difference."""
    assert iso_timestamp("not a date") is None
    assert iso_timestamp("") is None
    assert iso_timestamp(None) is None


@pytest.mark.parametrize("raw,expected_prefix", [
    ("20260826T091500Z", "2026-08-26T09:15:00"),
    ("2026-08-26T07:00:00+00:00", "2026-08-26T07:00:00"),
    ("2026-08-26T10:30:00+0000", "2026-08-26T10:30:00"),
    ("2026-08-26", "2026-08-26"),
])
def test_every_providers_timestamp_format_is_understood(raw, expected_prefix):
    assert iso_timestamp(raw).startswith(expected_prefix)


# ======================================================
# D. Aggregation through web_search
# ======================================================
@pytest.fixture
def both(monkeypatch):
    monkeypatch.setattr(news_mediastack, "api_key", lambda: "test-key")
    monkeypatch.setattr(news_nytimes, "api_key", lambda: "test-key")
    stub(monkeypatch, news_mediastack, MEDIASTACK_PAYLOAD)
    stub(monkeypatch, news_nytimes, NYTIMES_PAYLOAD)


def test_news_search_aggregates_multiple_providers(both):
    result = ws.web_search(QUERY)
    provenances = [item["provenance"] for item in result["items"]]

    assert set(provenances) == {"nytimes", "mediastack"}
    assert result["providers"] == ["mediastack", "nytimes"]


def test_the_named_publisher_leads(both):
    """Order is preference order, and dedup keeps the first occurrence."""
    assert ws.web_search(QUERY)["items"][0]["provenance"] == "nytimes"


def test_a_story_carried_by_two_providers_appears_once(monkeypatch):
    """Syndication is the normal case, not an edge case.

    Three copies of one story spend the answer's budget saying the same
    thing three times -- and read to a model as three sources agreeing.
    """
    shared = {"status": "ok", "data": {"pagination": {}, "data": [
        {"title": "Inside the AI rulebook", "description": "The same story, syndicated.",
         "url": "https://nytimes.com/ai-rulebook", "source": "The Times",
         "published_at": "2026-08-26T10:30:00+00:00"},
    ]}}
    monkeypatch.setattr(news_nytimes, "api_key", lambda: "test-key")
    monkeypatch.setattr(news_mediastack, "api_key", lambda: "test-key")
    stub(monkeypatch, news_nytimes, NYTIMES_PAYLOAD)
    stub(monkeypatch, news_mediastack, shared)

    items = ws.web_search(QUERY)["items"]
    urls = [item["url"] for item in items]

    assert len(urls) == len(set(urls)), "the same URL appeared twice"
    assert sum(1 for i in items if "ai-rulebook" in (i["url"] or "")) == 1
    assert items[0]["provenance"] == "nytimes", "the preferred copy did not survive"


def test_ranks_stay_unique_across_providers(both):
    items = ws.web_search(QUERY)["items"]
    assert [item["rank"] for item in items] == list(range(1, len(items) + 1))


def test_one_configured_provider_is_enough_for_an_answer(monkeypatch):
    """Neither is required; either alone answers."""
    monkeypatch.setattr(news_mediastack, "api_key", lambda: "test-key")
    stub(monkeypatch, news_mediastack, MEDIASTACK_PAYLOAD)

    result = ws.web_search(QUERY)
    assert result["items"]
    assert result["providers"] == ["mediastack"]


def test_all_providers_empty_is_still_a_hard_miss(monkeypatch):
    """Arc 240A's behaviour, preserved: no results is not a summary."""
    monkeypatch.setattr(news_mediastack, "api_key", lambda: "test-key")
    stub(monkeypatch, news_mediastack, {"status": "ok", "data": {"data": []}})

    result = ws.web_search(QUERY)
    assert result["items"] == []
    assert result["summary"] == ws.NO_RESULTS


def test_one_provider_failing_does_not_lose_the_others(monkeypatch):
    monkeypatch.setattr(news_nytimes, "api_key", lambda: "test-key")
    monkeypatch.setattr(news_nytimes, "http_fetch",
                        lambda url, **kwargs: (_ for _ in ()).throw(RuntimeError("upstream down")))
    monkeypatch.setattr(news_mediastack, "api_key", lambda: "test-key")
    stub(monkeypatch, news_mediastack, MEDIASTACK_PAYLOAD)

    result = ws.web_search(QUERY)
    assert result["items"], "a failing provider took the whole search down"
    assert all(item["provenance"] == "mediastack" for item in result["items"])


def test_a_finance_question_does_not_reach_the_news_providers(monkeypatch):
    """Applicability is decided before the call, not after."""
    monkeypatch.setattr(news_mediastack, "api_key", lambda: "test-key")
    called = stub(monkeypatch, news_mediastack, MEDIASTACK_PAYLOAD)

    news_mediastack.lookup("msft stock price")
    assert not called


# ======================================================
# NewsAPI is gone
# ======================================================
def test_newsapi_is_no_longer_a_provider():
    """Its free tier forbids production use, so it cannot be the backend.

    Pinned rather than just deleted: re-adding it is an easy mistake to
    make, and the reason it went is a licensing fact, not a technical one.
    """
    from tools import providers

    assert not hasattr(providers, "newsapi")
    assert "newsapi" not in [p.PROVENANCE for p in providers.PROVIDERS]
