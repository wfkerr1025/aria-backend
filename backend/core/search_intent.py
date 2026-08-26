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
    "EXPLICIT_WEB_SEARCH",
    "LOCAL_SCOPE_PHRASES",
    "WEB_SEARCH_PHRASES",
    "mentions_local_scope",
    "mentions_web_search",
    "normalize",
]


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
    "latest news", "current events", "what's happening with", "news about",
    "stock price", "share price", "current price", "trading at",
    "exchange rate",
    "latest version", "current version", "release notes", "changelog",
    "up to date", "documentation for", "docs for",
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
    "we discussed", "you said earlier", "i told you",
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


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, pad both ends with a space.

    The padding is what makes whole-word matching a substring test: a
    phrase is present only as " phrase ", so "search" matches "search the
    web" and "please search" but not "research" or "searching". Apostrophes
    are dropped rather than split, so "what's" stays one token and
    "what's happening with" keeps matching.
    """
    lowered = str(text or "").lower().replace("'", "")
    return f" {' '.join(re.sub(r'[^a-z0-9]+', ' ', lowered).split())} "


def mentions_local_scope(text: str) -> bool:
    """Whether the query named the user's own material."""
    normalized = normalize(text)
    return any(f" {phrase} " in normalized for phrase in LOCAL_SCOPE_PHRASES)


def mentions_web_search(text: str) -> bool:
    """Whether this query is a request to look something up on the web.

    The local-scope veto runs first and wins outright: a question about
    the user's own notes is a retrieval question however it is phrased.
    """
    if mentions_local_scope(text):
        return False
    normalized = normalize(text)
    return any(f" {phrase} " in normalized for phrase in WEB_SEARCH_PHRASES)
