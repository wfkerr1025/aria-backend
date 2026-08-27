# backend/tests/test_routing_financial_queries.py
#
# Financial questions reaching the search path.
#
# The multi-provider backend can answer "what is MSFT trading at" -- but
# only if routing sends the turn down the search path in the first place.
# Measured before this arc, 10 of 17 financial and FX phrasings did not:
#
#     MSFT quote                        -> clarification_needed
#     stock quote for Microsoft         -> request
#     what is the market price of MSFT  -> question
#     is NASDAQ open today              -> question
#     is the stock market closed        -> question
#     status of the stock market today  -> request
#     what is the USD to JPY rate       -> question
#     EUR to USD today                  -> request
#     FX rate for GBP/USD               -> request
#
# Each of those reached a model with no lookup behind it, which for a
# question about a live price is a fabrication by construction.
#
# The other seven already routed correctly. This suite covers all of them
# so a later change cannot quietly undo the ones that were never broken.

from __future__ import annotations

import pytest

from backend.core import search_intent as si
from backend.core.conversation_manager import (
    INTENT_SEARCH_QUERY,
    INTENT_WEATHER_QUERY,
    detect_intent,
)


def route(query: str) -> str:
    return detect_intent(query, is_multi_turn_followup=False)


FINANCE = [
    "msft stock price",
    "microsofts stock price",
    "stock price of Microsoft",
    "what is MSFT trading at",
    "current price of MSFT",
    "share price of Microsoft",
    "MSFT quote",
    "stock quote for Microsoft",
    "what is the market price of MSFT",
]

MARKET_STATUS = [
    "is the market open right now",
    "is NASDAQ open today",
    "is the stock market closed",
    "status of the stock market today",
]

FX = [
    "current EUR/USD exchange rate",
    "what is the USD to JPY rate",
    "EUR to USD today",
    "FX rate for GBP/USD",
]


# ======================================================
# A. Positive routing
# ======================================================
@pytest.mark.parametrize("query", FINANCE + MARKET_STATUS)
def test_a_finance_question_routes_to_search(query):
    assert route(query) == INTENT_SEARCH_QUERY
    assert si.search_domain(query) == si.DOMAIN_FINANCE


@pytest.mark.parametrize("query", FX)
def test_an_fx_question_routes_to_search(query):
    assert route(query) == INTENT_SEARCH_QUERY
    assert si.search_domain(query) == si.DOMAIN_FX


def test_a_market_status_question_needs_no_ticker():
    """"Is the market open" names no company and is still a lookup."""
    assert route("is the market open right now") == INTENT_SEARCH_QUERY
    assert si.search_domain("is the market open right now") == si.DOMAIN_FINANCE


def test_fx_wins_over_finance_when_both_could_match():
    """"what is the USD to JPY rate" contains "rate", which reads as
    finance -- but the question is a conversion, and the more specific
    answer is the more useful one."""
    assert si.search_domain("what is the USD to JPY rate") == si.DOMAIN_FX


# ======================================================
# B. Negative routing
# ======================================================
@pytest.mark.parametrize("query,expected", [
    ("Who founded Microsoft?", "question"),
    ("Explain what a stock is.", "request"),
    ("I need this done right now.", "request"),
])
def test_a_static_question_is_not_a_search(query, expected):
    """The bare word "stock" is deliberately not in the vocabulary.

    "Explain what a stock is" is a definition a model answers perfectly
    well; routing it to a quote provider would be wasteful and wrong.
    Every finance entry is a phrase that only occurs when someone wants a
    number.
    """
    assert route(query) == expected
    assert route(query) != INTENT_SEARCH_QUERY
    assert si.search_domain(query) is None


def test_weather_still_wins():
    """Financial routing must not reach across into the fusion engine.

    "right now" appears in both "is the market open right now" and
    "what's the weather in Tokyo right now", and only one of them is a
    web lookup.
    """
    query = "What's the weather in Tokyo right now?"
    assert route(query) == INTENT_WEATHER_QUERY
    assert si.search_domain(query) is None


@pytest.mark.parametrize("query", [
    "quote me a line from the paper",
    "quote from the docs",
    "she quoted the spec",
])
def test_the_other_sense_of_quote_is_not_a_search(query):
    """"quote" earns its place -- "MSFT quote" is how people ask -- and it
    is also an ordinary English verb.

    Without the ambiguous-use veto, asking for a line from a document
    became an outbound request to a stock provider. This is the cost of
    the entry, and the veto is what pays it.
    """
    assert route(query) != INTENT_SEARCH_QUERY
    assert si.search_domain(query) is None


def test_a_financial_question_about_the_users_own_material_stays_local():
    """The privacy veto still outranks every phrase added here."""
    for query in ("search my notes for the stock price",
                  "what did we discuss about the exchange rate"):
        assert route(query) != INTENT_SEARCH_QUERY, query
        assert si.search_domain(query) is None, query


# ======================================================
# C. The chain actually activates
# ======================================================
def test_router_planner_and_executor_all_fire_for_one_query(monkeypatch):
    """Routing was the only broken link; this proves the rest connects.

    Every stage is the real one -- detect_intent, PlanBuilder,
    ToolExecutor, the provider-backed web_search -- with only the outbound
    HTTP replaced. A test that stubbed the planner or the executor would
    prove nothing about whether they run.
    """
    from backend.planning.plan_builder import PlanBuilder
    from backend.tools.tool_executor import ToolExecutor, ToolInvocation
    from backend.tools.tool_registry import STATUS_OK
    from tools.providers import yahoo_finance

    query = "msft stock price"

    # 1. the router sends it down the search path
    assert route(query) == INTENT_SEARCH_QUERY
    assert si.search_domain(query) == si.DOMAIN_FINANCE

    # 2. the planner asks for a lookup
    step = PlanBuilder().search_step(query, 1)
    assert step is not None, "the planner produced no web_search step"
    assert step.args.get("query")

    # 3. the executor runs it, and the provider is reached
    fetched = []
    monkeypatch.setattr(yahoo_finance, "http_fetch", lambda url: (
        fetched.append(url),
        {"status": "ok", "data": {"quoteResponse": {"result": [{
            "symbol": "MSFT", "shortName": "Microsoft Corporation",
            "regularMarketPrice": 496.37, "currency": "USD",
        }]}}},
    )[1])

    results = ToolExecutor().execute([
        ToolInvocation(tool_name="web_search", step_id="step1",
                       args={"query": step.args["query"]}),
    ])

    assert len(results) == 1
    assert results[0].status == STATUS_OK
    assert fetched, "the finance provider was never reached"
    assert "MSFT" in fetched[0]

    # 4. and the price is in what comes back
    assert results[0].normalized, "the quote did not survive execution"
    assert "496.37" in results[0].normalized[0].snippet
    assert results[0].normalized[0].source == "yahoo-finance"


# ======================================================
# The vocabulary itself
# ======================================================
def test_the_finance_phrases_are_reachable():
    """Arc 239's lesson: an entry normalize() would rewrite never matches."""
    for phrase in si.FINANCE_PHRASES + si.FX_PHRASES + si.AMBIGUOUS_USE_VETO:
        assert si.normalize_phrase(phrase) == phrase, (
            f"{phrase!r} can never match; the matcher compares "
            f"{si.normalize_phrase(phrase)!r}"
        )


def test_the_finance_vocabulary_is_part_of_the_search_vocabulary():
    """One table, so routing and planning cannot disagree about money."""
    for phrase in si.FINANCE_PHRASES + si.FX_PHRASES:
        assert phrase in si.WEB_SEARCH_PHRASES


def test_the_bare_word_stock_is_not_a_search_term():
    """Pinned, because adding it is the obvious next move and it is wrong."""
    assert "stock" not in si.WEB_SEARCH_PHRASES
    assert not si.mentions_web_search("what is a stock")
