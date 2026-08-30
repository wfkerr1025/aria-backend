"""ARIA Lite - asking the model whether a question needs the web.

search_intent holds a vocabulary, and a vocabulary can only recognise the
questions someone thought to write down. It answers "did the user ask for
a lookup" well, because asking for one uses a small, stable set of words.
It cannot answer "does this question have an answer the model does not
hold", because that is not a property of the wording.

Measured, before this module existed:

    "What is on Taco Bell's menu"      no lookup planned
    "What time does Costco close"      no lookup planned
    "population of Denmark"            no lookup planned
    "how much does a Model 3 cost"     no lookup planned

Every one answered from the model's weights, confidently and with no
indication to the user that nothing had been checked.

The obvious repair is a bigger word list, and it does not work here. This
is a Unity assistant, and the words that distinguish those questions --
menu, cost, hours, store, update, release, location, capital -- are the
same words its users type all day about software. Measured on fourteen
ordinary development requests, a marker list of that kind fired on all
fourteen: "store the value in a variable", "update the shader config",
"capital letter check in the parser". Requiring a proper noun alongside
the marker does not separate them either, because development prose is
full of proper nouns: Unity, Redis, NuGet, Editor. The two sets share
their vocabulary, so no vocabulary can tell them apart.

So the question goes to the model, which is the only thing in the system
that knows what it does and does not know.

Three properties this is built around:

    The local-scope veto is never delegated. A question about the user's
    own notes must not leave the machine, and that is a privacy boundary,
    not a judgement call. It is decided before the model is consulted and
    the model cannot overturn it.

    The vocabulary still runs first, and still wins. "Search the web for
    X" is an explicit instruction; spending an inference to confirm it
    would be latency bought for nothing.

    A classifier that fails does not search. An error, a timeout, an
    unparseable answer -- each returns the deterministic verdict, which
    is "no". The alternative is a broken component that starts sending
    the user's questions to a third party, which is the wrong direction
    to fail in.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from backend.core.search_intent import mentions_local_scope, mentions_web_search

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "CLASSIFIER_PROMPT",
    "classify",
    "prime",
    "reset_cache",
    "wants_web_search",
]

# Greedy, and short. The model is being asked for one word; letting it
# warm up to a sentence costs latency on every turn and gives the parser
# more ways to be wrong.
MAX_TOKENS = 4
TEMPERATURE = 0.0

# Stated as a property of the answer rather than of the question. "Is
# this a search query" invites the model to think about phrasing, which
# is the failure the vocabulary already has. "Would answering need
# information you do not have" is about its own knowledge, which is the
# thing being asked and the one thing it is positioned to know.
CLASSIFIER_PROMPT = """You decide whether a question needs a web search.

Answer WEB if answering it correctly would need information you do not
reliably hold: current events, prices, opening hours, menus, releases,
scores, someone's present circumstances, or any fact about the world
that changes over time or is too specific to be memorised.

Answer LOCAL if you can answer it from what you already know, or if it
is about programming, this codebase, the user's own files, or a general
concept, definition or explanation.

Answer with exactly one word: WEB or LOCAL.

Question: {query}
Answer:"""

# One verdict per query per turn. Routing and planning both ask, and they
# must agree: a query that routes as a search and then plans no search is
# the failure search_intent was written to end, and two independent model
# calls could land on different sides of it. Small, because it exists to
# join up one turn's consumers rather than to remember anything.
_CACHE: dict[str, bool] = {}
_CACHE_LIMIT = 64


def _key(query) -> str:
    return " ".join(str(query or "").lower().split())


def reset_cache() -> None:
    _CACHE.clear()


def _remember(key: str, verdict: bool) -> bool:
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[key] = verdict
    return verdict


def _read(text) -> Optional[bool]:
    """WEB, LOCAL, or None when the model said something else.

    None rather than a guess. A model that answered with a sentence has
    not answered the question asked, and reading a verdict out of it
    would be inventing one.
    """
    words = re.findall(r"[a-z]+", str(text or "").lower())
    for word in words[:3]:
        if word == "web":
            return True
        if word == "local":
            return False
    return None


def classify(query: str, generate: Callable[[str], str]) -> Optional[bool]:
    """The model's verdict, or None if it could not give one."""
    try:
        answer = generate(CLASSIFIER_PROMPT.format(query=str(query or "").strip()))
    except Exception:
        # Includes the model being unavailable, the wrong registry for
        # the current mode, and anything the provider raises. None of
        # them is a reason to fail the turn.
        logger.exception("search_activation: classifier call failed")
        return None

    verdict = _read(answer)
    if verdict is None:
        logger.info("search_activation: unparseable verdict %r", str(answer)[:80])
    return verdict


def _asks_for_file_work(query: str, history=()) -> bool:
    """Whether this turn is about files in the project.

    Asks the routing classifier rather than keeping a second vocabulary:
    a turn routed to the tool model for file work is the same turn that
    must not leave the machine to answer.

    The history goes with it, because the classifier now reads it. "ok, I
    need you to add some things to the inventory" names no file, so with
    an empty history it is not file work, and this turn went to the web:
    it came back with a tutorial citing UhiyamaLab, on a small model,
    while the file it was about sat unchanged on disk. The message before
    it said "created player_inventory.cs".
    """
    try:
        from backend.chat.model_router import TURN_TOOLS, classify_turn

        class _Request:
            latest_user_text = query
            messages = list(history or ())

        # TURN_TOOLS only. TURN_HEAVY covers deep reasoning, which can
        # legitimately want a lookup.
        return classify_turn(_Request()) == TURN_TOOLS
    except Exception:  # pragma: no cover - a veto must not break routing
        logger.exception("could not classify the query for the file-work veto")
        return False


def wants_web_search(query: str, *, generate: Callable[[str], str] | None = None,
                     history=()) -> bool:
    """Whether this turn should look something up.

    With no `generate`, this is exactly the deterministic answer the
    vocabulary gives -- which is what keeps every existing caller, and
    the planner's determinism, unchanged. The transport primes the cache
    once per turn; routing and planning then read the same verdict.
    """
    if mentions_local_scope(query):
        # Not delegated, and not cached: the user's own material is out
        # of scope for the web whatever else is true about the wording.
        return False

    if mentions_web_search(query):
        return True

    # AFTER the explicit-web check, and that order is the whole of it. A
    # web search is itself a tool turn as far as the routing classifier
    # is concerned, so vetoing every tool turn here silenced search
    # completely -- the first version of this did exactly that and two
    # tests said so immediately.
    #
    # What is left is file work: create, edit, move, delete. That is
    # about THIS project by definition and there is nothing on the web
    # that answers it.
    #
    # Measured: "create the actual file" was sent to the classifier,
    # which said WEB. The turn ran a search, escalated to the 12B, and
    # took two minutes to reply with instructions for using a file
    # manager. The veto costs nothing and removes that detour from the
    # most common instruction a user gives.
    if _asks_for_file_work(query, history):
        logger.info("search_activation: file work is local; not searching for %r",
                    query[:60])
        return False

    key = _key(query)
    if not key:
        return False
    if key in _CACHE:
        return _CACHE[key]

    if generate is None:
        return False

    verdict = classify(query, generate)
    if verdict is None:
        # Fail closed. A classifier that cannot answer must not be the
        # reason a question leaves the machine.
        return _remember(key, False)

    logger.info("search_activation: model says %s for %r",
                "WEB" if verdict else "LOCAL", key[:60])
    return _remember(key, verdict)


def prime(query: str, generate: Callable[[str], str] | None, history=()) -> bool:
    """Decide this turn's verdict once, before routing and planning run.

    Separate from wants_web_search only to make the call site read as
    what it is: the one place that spends an inference, so the two
    consumers behind it can be lookups.
    """
    return wants_web_search(query, generate=generate, history=history)
