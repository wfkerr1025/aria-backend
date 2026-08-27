"""ARIA Lite - the one place that decides a query wants the web.

Two lists used to answer that question, and they disagreed.

backend/core/conversation_manager.py's TOOL_KEYWORDS["web_search"] decided
whether a turn *routes* as a search. backend/planning/plan_builder.py's
SEARCH_WORDS decided whether a plan *contains* a lookup step. The routing
list was widened after live queries phrased as bare imperatives fell
through to the model; the planning list was not. So "search the current
stock price of Microsoft" classified as a search, entered the reasoning
turn, produced a plan with no search step, ran no tool, and was answered
from the model's weights -- which is the hallucinated-price bug, exactly.

A query that routes as a search and then plans no search is the worst of
both: the user is told nothing is wrong and the answer is invented. One
table, one matcher, both consumers importing from here.

This module deliberately imports nothing from ARIA. It is a vocabulary and
two predicates, so both the transport-facing router and the planner can
depend on it without either depending on the other.
"""

from __future__ import annotations

import re

__all__ = [
    "DOMAIN_FINANCE",
    "DOMAIN_FX",
    "AMBIGUOUS_USE_VETO",
    "EXPLICIT_WEB_SEARCH",
    "FINANCE_PHRASES",
    "FX_PHRASES",
    "search_domain",
    "normalize_phrase",
    "LOCAL_SCOPE_PHRASES",
    "WEB_SEARCH_PHRASES",
    "mentions_local_scope",
    "mentions_web_search",
    "normalize",
]


# Questions about money, which are questions about right now.
#
# Kept as their own tables rather than folded into the list below, because
# two things need them separately: the vocabulary that decides a query
# wants the web, and search_domain(), which says which kind of lookup it
# wants so a caller can route to the right provider.
#
# What is deliberately NOT here is the bare word "stock". "Explain what a
# stock is" is a definition question a model answers perfectly well, and
# routing it to a quote provider would be both wasteful and wrong. Every
# entry is a phrase that only occurs when someone wants a number.
FINANCE_PHRASES: tuple[str, ...] = (
    "stock price", "share price", "stock quote", "market price",
    "current price", "trading at", "ticker", "quote",
    "stock market", "nasdaq", "nyse", "dow jones", "s p 500", "sp 500",
    "market open", "market closed", "market close",
)

# Currency conversion. "to usd" and friends are listed explicitly rather
# than derived, so "convert this to USD" is caught and "I sent it to us"
# is not.
FX_PHRASES: tuple[str, ...] = (
    "exchange rate", "fx rate", "conversion rate", "currency pair",
    "to usd", "to eur", "to jpy", "to gbp", "to cad", "to aud",
    "to chf", "to cny", "to inr", "to nzd",
)

# Phrases that mean an otherwise-financial word is not being used in its
# financial sense.
#
# "quote" earns its place in FINANCE_PHRASES -- "MSFT quote" is how people
# ask -- and it is also an ordinary English verb. "Quote me a line from the
# paper" is a request about a document, and sending it to a stock provider
# is both wrong and an outbound request nobody asked for. Checked before
# the search vocabulary, like the local-scope veto and for the same
# reason: a word with two senses needs the other sense named.
AMBIGUOUS_USE_VETO: tuple[str, ...] = (
    "quote me", "quote from", "quote the", "quotes from", "quoted",
    # "news" is in the search vocabulary as a bare noun, because that is
    # the only way to cover "new python news" / "latest python news" /
    # "python news" without keeping a list of word orders. It is also an
    # ordinary English noun with a fixed set of idiomatic uses, and none
    # of them is a request to go and look anything up.
    "good news", "bad news", "great news", "news to me",
)

# A request to go and look something up.
#
# Every entry is matched whole-word (see normalize), which is what makes a
# bare word safe here: "search" cannot match "research", and "google"
# cannot match "googled" -- the two failure modes that kept this list to
# phrases for so long.
WEB_SEARCH_PHRASES: tuple[str, ...] = (
    # Explicit requests, which name the tool outright.
    "search the web", "search for", "search online", "web search",
    "look up", "look it up", "google", "on the web", "on the internet",
    "find out about",
    # The bare imperative. This is the one that mattered: "Search the
    # latest world news" is the most natural way to ask, and it matched
    # nothing until this entry existed. Safe only because matching is
    # whole-word and because LOCAL_SCOPE_PHRASES is checked first.
    "search",
    # Things that are current by nature. A model's weights cannot hold
    # today's price or this week's release, so a confident answer to one
    # of these without a lookup is a fabrication by construction.
    "latest news", "current events", "news about",
    "stock price", "share price", "current price", "trading at",
    "exchange rate",
    "latest version", "current version", "release notes", "changelog",
    "up to date", "documentation for", "docs for",

    # Asking after the present state of something. Each of these was
    # measured as a miss: the planner produced no lookup step and the
    # turn was answered from the model's weights.
    #
    # "whats happening" replaces "what's happening with", which could
    # never match anything. normalize() strips apostrophes out of the
    # text but the table kept them, so the padded comparison was looking
    # for " what's happening with " inside " whats happening with " --
    # see the test that now forbids a phrase normalize would alter.
    "whats happening",
    "breaking news", "todays headlines", "headlines today",
    "live score",

    # The bare nouns, for the same reason "search" is here bare.
    #
    # Every multi-word entry above is matched as a contiguous phrase, so
    # the table was really a list of word *orders*. "python latest news"
    # matched, because "latest news" happens to sit adjacent in it. "new
    # python news", "latest python news" and "python news" all missed --
    # the same question, the same three words, arranged the way people
    # actually type them. Measured live: "new python news" planned no
    # lookup, routed to a 0.5B model and came back with a template
    # asking the user to paste the article in.
    #
    # Adding permutations would have been a second list to drift. A
    # bare noun covers the whole family, and is safe here for the reason
    # "search" and "google" are: matching is whole-word, so "news"
    # cannot match "newsletter", and the idiomatic senses are named in
    # AMBIGUOUS_USE_VETO.
    "news", "headlines",

    # Money. Split out below into FINANCE_PHRASES and FX_PHRASES so a
    # caller can tell which kind of lookup a query wants, but they are all
    # search phrases first: a price, a rate or a market's status is true
    # for as long as it takes to say it, and no model's weights hold any
    # of them.
    *FINANCE_PHRASES,
    *FX_PHRASES,
    #
    # NOT here: "forecast for". "forecast" is a get_weather keyword, so
    # "what's the forecast for the housing market?" routes to the weather
    # fusion engine and the planner is never reached -- adding it would
    # claim a coverage that cannot take effect. Fixing that means teaching
    # the weather keyword to yield, which is routing, and routing is out
    # of scope here. See test_planner_search_activation.
)

# Phrases that mean the lookup is about the user's own material.
#
# This is a security control, not a nicety. Without it, the bare "search"
# above turns "search my notes for the shader error" into an outbound
# request carrying the user's private text to a third-party search engine
# -- and the answer it comes back with is not even the one they asked for,
# because the notes are local. Checked before any search phrase, and
# vetoing all of them.
LOCAL_SCOPE_PHRASES: tuple[str, ...] = (
    "my notes", "my note", "my documents", "my document", "my files",
    "my file", "my memory", "my knowledge", "the notes", "our notes",
    "my project", "this repo", "this repository", "the codebase",
    "my codebase", "these docs", "the evidence", "my conversation",
    "we discussed", "we discuss", "we talked about", "you said earlier",
    "i told you",
)

# The subset that names the tool outright, used where a search request has
# to beat another intent that would otherwise win on a shared word.
#
# "Search the weather in Tokyo using web search" asks for two things and
# says which one it wants; answering it from the fusion engine ignores the
# half of the sentence that was an instruction. A plain "what's the
# weather in Tokyo" still reaches fusion, because none of these appear in
# it.
EXPLICIT_WEB_SEARCH: tuple[str, ...] = (
    "web search", "search the web", "search online", "google",
    "on the web", "on the internet",
)


def normalize_phrase(phrase: str) -> str:
    """A table entry in the form the matcher will compare it in.

    Matching pads both sides and looks for the phrase inside normalized
    text, so an entry containing anything normalize() would rewrite --
    an apostrophe, punctuation, a capital -- silently never matches.
    That is exactly what happened to "what's happening with". Any entry
    for which this is not the identity is dead on arrival, and a test
    asserts none are.
    """
    return normalize(phrase).strip()


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, pad both ends with a space.

    The padding is what makes whole-word matching a substring test: a
    phrase is present only as " phrase ", so "search" matches "search the
    web" and "please search" but not "research" or "searching". Apostrophes
    are dropped rather than split, so "what's" becomes one token, "whats"
    -- which is why the table spells it that way. See normalize_phrase.
    """
    lowered = str(text or "").lower().replace("'", "")
    return f" {' '.join(re.sub(r'[^a-z0-9]+', ' ', lowered).split())} "


def mentions_local_scope(text: str) -> bool:
    """Whether the query named the user's own material."""
    normalized = normalize(text)
    return any(f" {phrase} " in normalized for phrase in LOCAL_SCOPE_PHRASES)


def _ambiguous_use(text: str) -> bool:
    """Whether a two-sense keyword is being used in its non-search sense."""
    normalized = normalize(text)
    return any(f" {phrase} " in normalized for phrase in AMBIGUOUS_USE_VETO)


# What kind of lookup a query wants, for a caller that can pick a backend.
DOMAIN_FINANCE = "finance"
DOMAIN_FX = "fx"


def search_domain(text: str) -> str | None:
    """"finance", "fx", or None for a search that is neither.

    Returned alongside the intent rather than folded into it: detect_intent
    answers "what kind of turn is this", which every caller already
    branches on, and widening its return type would ripple through all of
    them for a fact only the search path cares about.

    FX is tested first. "what is the USD to JPY rate" contains "rate",
    which reads as finance, but the question is a conversion -- and the
    more specific answer is the more useful one.
    """
    if mentions_local_scope(text) or _ambiguous_use(text):
        return None

    normalized = normalize(text)
    if any(f" {phrase} " in normalized for phrase in FX_PHRASES):
        return DOMAIN_FX
    if any(f" {phrase} " in normalized for phrase in FINANCE_PHRASES):
        return DOMAIN_FINANCE
    return None


def mentions_web_search(text: str) -> bool:
    """Whether this query is a request to look something up on the web.

    The local-scope veto runs first and wins outright: a question about
    the user's own notes is a retrieval question however it is phrased.
    """
    if mentions_local_scope(text) or _ambiguous_use(text):
        return False
    normalized = normalize(text)
    return any(f" {phrase} " in normalized for phrase in WEB_SEARCH_PHRASES)
