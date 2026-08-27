# backend/tests/test_planner_search_activation.py
#
# Whether the planner asks for a lookup on a question that needs one.
#
# Worth being precise about what this suite does and does not cover,
# because the reported symptom -- "I could not retrieve current data for
# this query" -- has three candidate causes and only one of them is here.
#
#   1. the planner emits no web_search step        <- this suite
#   2. the lookup runs and DuckDuckGo returns an
#      empty instant-answer envelope               <- NOT a planner fault
#   3. the evidence layer rejects what came back   <- test_evidence_bundling
#
# Measured on the four reported queries, (1) was already correct for three
# of them and (2) is what actually happens: DuckDuckGo's instant-answer
# API carries no stock quotes, no headlines and no exchange rates, and
# returns {"Abstract":"","AbstractSource":"",...} for all of them.
#
# What this arc did fix is a real gap either side of those four: phrasings
# that ask after the present state of something and produced no lookup at
# all. Those are the SEARCH_REQUIRED cases below.
#
# PlanBuilder is deterministic Python -- there is no planner prompt and no
# model in this path -- so every assertion here is exact rather than
# probabilistic.

from __future__ import annotations

import pytest

from backend.core import search_intent as si
from backend.core.conversation_manager import INTENT_SEARCH_QUERY, INTENT_WEATHER_QUERY, detect_intent
from backend.planning.plan import KIND_SEARCH
from backend.planning.plan_builder import PlanBuilder


def search_step(query: str):
    return PlanBuilder().search_step(query, 1)


# The four from the report.
REPORTED = [
    "What is the stock price of Microsoft?",
    "Show me the latest news about AI regulation.",
    "What is the current EUR/USD exchange rate?",
]

# Phrasings that ask after the present state of something. Every one was
# measured as a miss before this arc: no lookup step, answered from the
# model's weights.
NEWLY_COVERED = [
    "What's happening in the markets right now?",
    "Give me today's headlines",
    "What is the live score of the game?",
    "Any breaking news on the election?",
    "Is the market open right now?",
]

# The same question, in the word orders people actually type it in.
#
# Every phrase in the table is matched contiguously, so the table was
# really a list of word *orders*: "python latest news" matched because
# "latest news" happens to sit adjacent in it, and none of these did.
# Measured live -- "new python news" planned no lookup, routed to a 0.5B
# model, and came back with a template asking the user to paste the
# article in.
#
# Fixed with the bare nouns "news" and "headlines" rather than by adding
# permutations, which would have been a second list to drift.
WORD_ORDERS = [
    "new python news",
    "latest python news",
    "python news",
    "any AI headlines",
    "what's the news on rust",
]

# "news" is also an ordinary English noun, and none of these is a request
# to go and look anything up. Same shape as the "quote" veto: a word with
# two senses needs the other sense named.
NEWS_IDIOMS = [
    "that is news to me",
    "I have good news about the build",
    "bad news, the deploy failed",
    "great news everyone",
]

# Questions a model can answer from what it already knows, or that are
# about the user's own material. A lookup here is wasted latency at best
# and a privacy leak at worst.
STATIC = [
    "Who founded Microsoft?",
    "Explain how TCP works",
    "What does the acronym API stand for?",
    "Summarize the build pipeline",
    "Fix the failing shader test",
    "search my notes for the shader error",
    "research the history of Rome",
]


# ======================================================
# A question that needs a lookup gets one
# ======================================================
@pytest.mark.parametrize("query", REPORTED)
def test_the_reported_queries_plan_a_lookup(query):
    step = search_step(query)

    assert step is not None, "no web_search step was planned"
    assert step.kind == KIND_SEARCH
    assert step.args.get("query"), "the step carries no query to search for"


@pytest.mark.parametrize("query", NEWLY_COVERED)
def test_present_state_questions_plan_a_lookup(query):
    """The genuine gap this arc closed.

    Each of these asks what is true right now, which no model's weights
    can hold -- and each produced no lookup step at all.
    """
    assert search_step(query) is not None


@pytest.mark.parametrize("query", REPORTED + NEWLY_COVERED)
def test_routing_and_planning_agree(query):
    """The invariant that keeps the two halves from drifting.

    A query that routes as a search and then plans no search is the worst
    of both: nothing looks wrong and the answer is invented. Both read the
    same table, and this asserts they still reach the same verdict.
    """
    assert detect_intent(query, is_multi_turn_followup=False) == INTENT_SEARCH_QUERY
    assert search_step(query) is not None


# ======================================================
# A question that does not gets left alone
# ======================================================
@pytest.mark.parametrize("query", STATIC)
def test_a_static_question_may_answer_directly(query):
    """The negative case, and it is not a formality.

    Every phrase added to the table is a phrase that could fire here. A
    vocabulary that catches everything is a vocabulary that has stopped
    discriminating.
    """
    assert search_step(query) is None


def test_urgency_is_not_a_request_for_a_lookup():
    """"right now" means "hurry", not "search the web".

    A bare "right now" was the tempting addition and it is deliberately
    not in the table -- it would turn every impatient instruction into an
    outbound request.
    """
    assert search_step("I need this done right now") is None
    assert search_step("fix the failing test right now") is None


def test_a_question_about_the_users_own_material_never_reaches_the_web():
    """The veto still beats every phrase added above."""
    for query in ("search my notes for today's headlines",
                  "what's happening in this repo right now",
                  "look up the breaking news in my documents"):
        assert search_step(query) is None, query


# ======================================================
# Weather is answered by the fusion engine, not a search
# ======================================================
def test_a_non_weather_forecast_still_routes_to_weather():
    """A known limitation, pinned rather than papered over.

    "forecast" is a get_weather keyword, so "what's the forecast for the
    housing market?" reaches the weather fusion engine, fails to geocode
    "the housing market", and asks the user which city they meant.

    Adding "forecast for" to the search vocabulary does not help: routing
    decides before the planner runs, so the phrase could never take
    effect. Fixing it means teaching the weather keyword to yield to a
    non-place subject -- a routing change, out of scope for a planner-only
    arc. Recorded here so the next person finds the reason rather than the
    symptom.
    """
    query = "What's the forecast for the housing market?"

    assert detect_intent(query, is_multi_turn_followup=False) == INTENT_WEATHER_QUERY
    assert not si.mentions_web_search(query)


def test_a_weather_question_is_not_planned_as_a_web_search():
    """Deliberate, and older than this arc.

    "What is the weather in Tokyo right now?" is on the reported list, but
    it must not become a web_search: weather routes to the fusion engine,
    which returns a real reading, and a model must never generate weather
    data. Planning a search for it would replace a measurement with a
    search result.
    """
    query = "What is the weather in Tokyo right now?"

    assert detect_intent(query, is_multi_turn_followup=False) == INTENT_WEATHER_QUERY
    assert search_step(query) is None


def test_an_explicit_search_request_still_beats_weather():
    """The one case where weather words yield to a stated instruction."""
    assert si.mentions_web_search("search the web for the weather in Tokyo")


# ======================================================
# The table itself
# ======================================================
def test_no_phrase_is_dead_on_arrival():
    """The bug this arc found: an entry the matcher can never see.

    normalize() strips apostrophes out of the text but the table kept
    them, so " what's happening with " was being looked for inside
    " whats happening with " and never matched. Any entry that normalize
    would rewrite is unreachable, and there must be none.
    """
    for phrase in si.WEB_SEARCH_PHRASES + si.LOCAL_SCOPE_PHRASES + si.EXPLICIT_WEB_SEARCH:
        assert si.normalize_phrase(phrase) == phrase, (
            f"{phrase!r} can never match: the matcher compares "
            f"{si.normalize_phrase(phrase)!r}"
        )


def test_the_planner_and_the_router_share_one_table():
    """Not two lists that happen to agree today."""
    from backend.planning.plan_builder import LOCAL_SCOPE_WORDS, SEARCH_WORDS

    assert SEARCH_WORDS is si.WEB_SEARCH_PHRASES
    assert LOCAL_SCOPE_WORDS is si.LOCAL_SCOPE_PHRASES


def test_the_planner_is_deterministic():
    """No prompt, no model, no sampling -- so these assertions are exact.

    Worth pinning: the arc that produced this suite asked for a change to
    "the planner prompt", and there is none. If one is ever introduced,
    this test should be the thing that makes someone say so out loud.
    """
    import inspect

    source = inspect.getsource(PlanBuilder.search_step)
    for forbidden in ("prompt", "generate(", "llm", "completion"):
        assert forbidden not in source.lower(), (
            f"search_step now references {forbidden!r}; the planner is no "
            f"longer deterministic and these tests need revisiting"
        )


@pytest.mark.parametrize("query", REPORTED + NEWLY_COVERED)
def test_the_planned_step_names_something_to_search_for(query):
    """A step with an empty query is a step that cannot run."""
    step = search_step(query)
    assert step.args["query"].strip()
    assert "?" not in step.args["query"], "the question mark is not a search term"


# ======================================================
# Word order is not the question
# ======================================================
@pytest.mark.parametrize("query", WORD_ORDERS)
def test_the_same_question_activates_in_any_word_order(query):
    assert si.mentions_web_search(query), (
        f"{query!r} asks for current information and plans no lookup; "
        f"the answer would come from the model's weights"
    )


@pytest.mark.parametrize("query", WORD_ORDERS)
def test_every_word_order_plans_a_lookup(query):
    step = search_step(query)
    assert step is not None
    assert step.args["query"].strip()


@pytest.mark.parametrize("query", NEWS_IDIOMS)
def test_the_idiomatic_senses_of_news_do_not_activate(query):
    assert not si.mentions_web_search(query)


@pytest.mark.parametrize("query", ["the newsletter said", "read the newspaper",
                                  "newsworthy", "newscast"])
def test_a_bare_noun_is_safe_because_matching_is_whole_word(query):
    # What kept this list to phrases for so long. "search" cannot match
    # "research"; "news" must not match "newsletter" for the same reason.
    assert not si.mentions_web_search(query)
