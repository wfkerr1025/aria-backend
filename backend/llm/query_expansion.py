"""ARIA Lite - domain-adaptive semantic query expansion.

Widens a de-framed query with the words it could have been asked with, so
retrieval stops depending on the user picking the same vocabulary the note
was written in.

The problem is narrow and real. A note saying "the espresso machine in the
break room is broken" is, to an embedding model, only loosely related to
"coffee machine", and to a keyword search it is nothing at all -- the two
share no term. Expansion adds "espresso" and "break room" to the query as
secondary evidence, which moves the query vector toward the note and lets
the note earn partial keyword credit, without pretending the synonym is as
good as the word the user actually typed.

Which synonyms are right depends on what is being talked about. "scene" is
a level to Unity, a place to a news report and a chapter to a screenwriter;
"asset" is a prefab in one conversation and a balance-sheet entry in
another. A single flat table has to pick one reading and inflict it on
every query, so expansion is scoped to a domain -- taken from the router's
intent where it has an opinion, and from the query's own vocabulary
otherwise. Terms that mean the same thing everywhere ("error", "config")
are shared into every table rather than duplicated, so picking a domain
never costs a query the general words it also contains.

Rule-based and dictionary-driven, deliberately. This runs in front of every
retrieval, so the same reasoning applies as in query_deframing: a step that
is occasionally creative is worse than one that is always predictable,
because a query that expands differently on two calls makes search results
irreproducible for no visible reason. There are no LLM calls here.

Two things are load-bearing about how expansions are weighted:

    A synonym never outranks the real word. Every expansion carries a
    weight below 1.0, and the total weight of all expansions is capped
    (EXPANSION_MASS) at a fraction of the base query's. A query expanding
    to six terms would otherwise be six parts synonym to one part topic,
    and the combined vector would drift off the thing that was asked about
    -- far enough, on a query with several expandable words, to push an
    exact match below the retrieval floor.

    Expansion can only ever help. A term the user typed is worth 1.0 and a
    synonym cannot add to that, so an item containing every query term
    scores exactly what it scored before expansion existed, and an item
    containing none of them and no synonyms still scores zero. Only the
    middle moves.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import TypedDict

try:
    from backend.llm import semantic_embeddings
    from backend.llm.query_deframing import deframe_query
except ImportError:  # running from inside the backend directory
    from llm import semantic_embeddings
    from llm.query_deframing import deframe_query

__all__ = [
    "ARIA_INTERNAL_EXPANSIONS",
    "BACKEND_EXPANSIONS",
    "GENERAL_EXPANSIONS",
    "STOPWORDS",
    "TABLES",
    "UNITY_EXPANSIONS",
    "WEATHER_EXPANSIONS",
    "ExpansionDomain",
    "ExpansionResult",
    "detect_domain",
    "embed_expanded_query",
    "expand_query",
    "expansion_vector",
    "table_for",
    "tokenize",
]

# Words that carry no topic of their own. Kept deliberately short: an
# aggressive stoplist starts removing words that are the whole point of a
# query ("how to" for a how-to note), and coverage is a ratio, so every term
# dropped changes the denominator for every item equally.
#
# This is the one definition of the list. Keyword coverage in aria_memory
# imports tokenize from here rather than keeping a second copy, because two
# tokenizers that disagree about one stopword would silently disagree about
# every coverage denominator.
STOPWORDS = frozenset({"the", "a", "an", "and", "or", "of", "for", "to"})

# Trimmed from the edges of a token so "credentials?" and "credentials" are
# the same term. Interior punctuation is left alone -- "self-service" and
# "user.name" are single terms, not two.
_EDGE_PUNCTUATION = "\"'`.,;:!?()[]{}<>"

# A table maps a term or phrase to {synonym: weight}.
ExpansionTable = dict[str, dict[str, float]]


class ExpansionDomain(Enum):
    """What a query is about, and therefore which synonyms apply."""

    GENERAL = "general"
    UNITY = "unity"
    BACKEND = "backend"
    WEATHER = "weather"
    ARIA_INTERNAL = "aria_internal"


class ExpansionResult(TypedDict):
    """What expand_query returns.

    Each entry in expansions carries a headline weight and per-source
    weights; see expand_query for why the two differ.
    """

    base: str
    terms: list[str]
    expansions: list[dict]
    domain: ExpansionDomain


# ======================================================
# The dictionaries
# ======================================================
# Each key maps to the terms a person might have used instead, with how much
# evidence that substitution is worth. Weights are judgement calls on one
# axis: how confident are we that a document using this word is about the
# same thing as one using the key?
#
#   0.7-0.8  the same concept, different spelling or register
#            ("config"/"configuration", "db"/"database", "pto"/"time off")
#   0.5-0.6  strongly related, usually co-occurring
#            ("login"/"authentication", "pipeline"/"build")
#   0.3-0.4  same neighbourhood, weak evidence on its own
#            ("coffee machine"/"barista")
#
# Multi-word keys are matched as phrases and win over their own single words,
# so "coffee machine" expands as one concept rather than as "coffee" plus
# "machine". Both directions of a pair are listed explicitly rather than
# derived: "db" -> "database" being worth 0.8 does not by itself mean the
# reverse is, and a table that is read in one direction is easier to correct
# than one that is half generated.
GENERAL_EXPANSIONS: ExpansionTable = {
    # --- access and identity ---
    "login": {"sign in": 0.7, "password": 0.7, "authentication": 0.6,
              "credentials": 0.6, "account": 0.5},
    "credentials": {"password": 0.7, "login": 0.6, "account": 0.5,
                    "secret": 0.5, "api key": 0.5, "token": 0.5},
    "password": {"credentials": 0.7, "login": 0.6, "passphrase": 0.6,
                 "authentication": 0.5, "account": 0.5},
    "authentication": {"login": 0.6, "sign in": 0.6, "credentials": 0.6,
                       "oauth": 0.5, "permissions": 0.4},
    "account": {"login": 0.6, "profile": 0.5, "credentials": 0.5,
                "user": 0.5},
    "permissions": {"access": 0.7, "roles": 0.6, "authorisation": 0.6,
                    "privileges": 0.6},

    # --- shipping software ---
    "deployment": {"release": 0.7, "rollout": 0.7, "deploy": 0.7,
                   "pipeline": 0.5, "ship": 0.4},
    "deploy": {"deployment": 0.7, "release": 0.7, "rollout": 0.6,
               "publish": 0.5},
    "release": {"deployment": 0.7, "rollout": 0.6, "version": 0.5,
                "publish": 0.5, "ship": 0.5},
    "rollout": {"release": 0.7, "deployment": 0.7, "rollback": 0.4},
    "pipeline": {"build": 0.6, "ci": 0.6, "workflow": 0.5,
                 "deployment": 0.5, "release": 0.5},
    "build": {"compile": 0.7, "pipeline": 0.5, "ci": 0.5, "artifact": 0.5},
    "ci": {"continuous integration": 0.7, "pipeline": 0.6, "build": 0.6},

    # --- things going wrong ---
    "error": {"exception": 0.7, "failure": 0.6, "bug": 0.6, "crash": 0.5,
              "stack trace": 0.5},
    "bug": {"defect": 0.7, "issue": 0.6, "error": 0.6, "regression": 0.5},
    "crash": {"exception": 0.6, "error": 0.6, "freeze": 0.5, "hang": 0.5},
    "slow": {"performance": 0.7, "latency": 0.6, "lag": 0.6,
             "bottleneck": 0.5},
    "performance": {"latency": 0.6, "throughput": 0.6, "speed": 0.6,
                    "profiling": 0.5},

    # --- storage and interfaces ---
    "db": {"database": 0.8, "sqlite": 0.5, "storage": 0.5},
    "database": {"db": 0.8, "sqlite": 0.5, "sql": 0.5, "storage": 0.5},
    "api": {"endpoint": 0.6, "interface": 0.5, "rest": 0.5},
    "ui": {"interface": 0.7, "frontend": 0.6, "layout": 0.5},
    "config": {"configuration": 0.8, "settings": 0.7, "setup": 0.5},
    "configuration": {"config": 0.8, "settings": 0.7, "setup": 0.5},
    "settings": {"config": 0.7, "configuration": 0.7, "preferences": 0.6},
    "docs": {"documentation": 0.8, "readme": 0.6, "guide": 0.6},
    "documentation": {"docs": 0.8, "readme": 0.6, "guide": 0.6},

    # --- the rest of a working life ---
    "coffee machine": {"espresso": 0.7, "coffee maker": 0.7, "kitchen": 0.5,
                       "break room": 0.5, "barista": 0.3},
    "coffee": {"espresso": 0.6, "caffeine": 0.5, "kitchen": 0.4},
    "meeting": {"standup": 0.6, "sync": 0.5, "call": 0.5, "agenda": 0.5},
    "vacation": {"holiday": 0.7, "time off": 0.7, "pto": 0.6, "leave": 0.6},
    "pto": {"time off": 0.8, "vacation": 0.7, "holiday": 0.6, "leave": 0.6},
    "invoice": {"billing": 0.7, "payment": 0.6, "receipt": 0.6},
}

# Vocabulary that means the same thing whichever domain is talking. A Unity
# question about an error and a backend question about an error both want
# "exception"; without this, picking a domain would silently cost a query
# every general word it also contains. Taken from GENERAL_EXPANSIONS by
# reference rather than retyped, so "error" has one definition however many
# tables it appears in.
_SHARED_KEYS = (
    "error", "bug", "crash", "slow", "performance",
    "config", "configuration", "settings", "docs", "documentation",
    "api", "db", "database", "build", "pipeline",
)
_SHARED: ExpansionTable = {key: GENERAL_EXPANSIONS[key] for key in _SHARED_KEYS}

# Unity. These are not in GENERAL on purpose: "scene" and "asset" are
# ordinary English words, and expanding them toward "gameobject" for someone
# asking about a crime scene or an asset write-down would be exactly the
# false precision domains exist to avoid.
UNITY_EXPANSIONS: ExpansionTable = {
    **_SHARED,
    "unity": {"editor": 0.5, "engine": 0.5, "gameobject": 0.4},
    "scene": {"level": 0.6, "hierarchy": 0.5, "gameobject": 0.5,
              "environment": 0.4},
    "prefab": {"gameobject": 0.7, "asset": 0.5, "instance": 0.4},
    "shader": {"material": 0.6, "rendering": 0.6, "gpu": 0.4},
    "script": {"component": 0.6, "monobehaviour": 0.6, "code": 0.5},
    "asset": {"resource": 0.6, "prefab": 0.5, "import": 0.4},
    "build pipeline": {"assetbundle": 0.6, "addressables": 0.5,
                       "player build": 0.5, "il2cpp": 0.4},
    "assetbundle": {"addressables": 0.6, "bundle": 0.6, "asset": 0.5},
    "addressables": {"assetbundle": 0.6, "asset": 0.5, "catalog": 0.4},
    "material": {"shader": 0.6, "texture": 0.5, "rendering": 0.5},
    "gameobject": {"prefab": 0.6, "component": 0.5, "transform": 0.4},
    "component": {"monobehaviour": 0.7, "script": 0.6, "gameobject": 0.5},
    "editor": {"inspector": 0.6, "unity": 0.5, "tooling": 0.4},
    "physics": {"collider": 0.6, "rigidbody": 0.6, "raycast": 0.5},
    "animation": {"animator": 0.7, "clip": 0.5, "rig": 0.4},
}

# The service layer: providers, transports, thresholds.
BACKEND_EXPANSIONS: ExpansionTable = {
    **_SHARED,
    "routing": {"intent": 0.7, "nlp": 0.5, "dispatch": 0.5,
                "classification": 0.5},
    "intent": {"routing": 0.7, "classification": 0.6, "nlp": 0.5},
    "fusion": {"provider": 0.6, "fallback": 0.5, "merge": 0.5},
    "provider": {"backend": 0.6, "model": 0.5, "fusion": 0.5,
                 "fallback": 0.5},
    "fallback": {"retry": 0.6, "provider": 0.5, "degraded": 0.5},
    "threshold": {"floor": 0.6, "cutoff": 0.5, "limit": 0.5},
    "floor": {"threshold": 0.7, "minimum": 0.6, "cutoff": 0.5},
    "endpoint": {"api": 0.7, "route": 0.6, "handler": 0.5},
    "ipc": {"transport": 0.6, "channel": 0.5, "socket": 0.5},
    "websocket": {"socket": 0.7, "stream": 0.5, "transport": 0.5},
    "streaming": {"stream": 0.8, "token stream": 0.6, "chunked": 0.5},
    "handler": {"endpoint": 0.6, "route": 0.5, "dispatch": 0.5},
    "schema": {"contract": 0.6, "packet": 0.5, "payload": 0.5},
    "timeout": {"deadline": 0.6, "retry": 0.5, "latency": 0.5},
}

# Forecasts and the services behind them.
WEATHER_EXPANSIONS: ExpansionTable = {
    **_SHARED,
    "forecast": {"precipitation": 0.6, "outlook": 0.6, "temperature": 0.5,
                 "conditions": 0.5},
    "radar": {"reflectivity": 0.7, "storm": 0.5, "precipitation": 0.5},
    "station": {"grid": 0.6, "observation": 0.6, "noaa": 0.5},
    "temperature": {"degrees": 0.6, "celsius": 0.5, "fahrenheit": 0.5,
                    "heat": 0.5},
    "precipitation": {"rain": 0.7, "rainfall": 0.7, "snow": 0.6},
    "rain": {"precipitation": 0.7, "rainfall": 0.7, "showers": 0.6},
    "snow": {"snowfall": 0.7, "precipitation": 0.6, "winter": 0.4},
    "storm": {"thunderstorm": 0.7, "severe": 0.6, "radar": 0.5},
    "wind": {"gust": 0.7, "breeze": 0.5, "knots": 0.4},
    "humidity": {"dew point": 0.6, "moisture": 0.6},
    "alert": {"warning": 0.7, "advisory": 0.7, "watch": 0.6},
    "geocode": {"coordinates": 0.7, "location": 0.6, "latitude": 0.5},
}

# ARIA talking about itself: the retrieval stack, not the app it helps build.
ARIA_INTERNAL_EXPANSIONS: ExpansionTable = {
    **_SHARED,
    "nl routing": {"intent": 0.7, "fusion": 0.6, "classification": 0.5},
    "score floor": {"threshold": 0.7, "min score": 0.7, "cutoff": 0.6},
    "semantic": {"embedding": 0.6, "meaning": 0.6, "vector": 0.6},
    "embedding": {"vector": 0.7, "encoder": 0.6, "semantic": 0.6},
    "expansion": {"synonym": 0.7, "recall": 0.5, "widening": 0.5},
    "deframing": {"framing": 0.7, "preprocessing": 0.5, "cleaning": 0.5},
    "coverage": {"keyword score": 0.7, "term match": 0.6},
    "hybrid": {"keyword": 0.6, "semantic": 0.6, "retrieval": 0.6},
    "ranking": {"scoring": 0.7, "ordering": 0.6, "combined score": 0.6},
    "recency": {"freshness": 0.7, "decay": 0.6, "age": 0.5},
    "chunk": {"passage": 0.6, "fragment": 0.6, "window": 0.5},
    "note": {"memory": 0.6, "record": 0.5},
    "tool": {"command": 0.6, "registry": 0.5, "dispatcher": 0.5},
}

TABLES: dict[ExpansionDomain, ExpansionTable] = {
    ExpansionDomain.GENERAL: GENERAL_EXPANSIONS,
    ExpansionDomain.UNITY: UNITY_EXPANSIONS,
    ExpansionDomain.BACKEND: BACKEND_EXPANSIONS,
    ExpansionDomain.WEATHER: WEATHER_EXPANSIONS,
    ExpansionDomain.ARIA_INTERNAL: ARIA_INTERNAL_EXPANSIONS,
}

# ======================================================
# Domain detection
# ======================================================
# Words that give a query away when the router has no opinion. Deliberately
# narrow: a marker should be a word that is nearly always about its domain,
# because a false positive here silently swaps a query's whole vocabulary.
# "build" and "script" are absent for that reason -- both are as at home in
# a backend question as a Unity one.
DOMAIN_MARKERS: dict[ExpansionDomain, tuple[str, ...]] = {
    # Checked before UNITY and BACKEND: these are how ARIA is asked about
    # itself, and several of them ("routing", "semantic") also read as
    # ordinary backend words on their own.
    ExpansionDomain.ARIA_INTERNAL: (
        "nl routing", "score floor", "min score", "deframing",
        "query expansion", "hybrid search", "keyword coverage",
        "semantic index", "aria",
    ),
    ExpansionDomain.UNITY: (
        "unity", "prefab", "gameobject", "monobehaviour", "shader",
        "assetbundle", "addressables", "il2cpp", "scriptableobject",
        "inspector", "rigidbody", "collider", "animator",
    ),
    ExpansionDomain.WEATHER: (
        "weather", "forecast", "radar", "precipitation", "temperature",
        "rainfall", "thunderstorm", "humidity", "noaa", "dew point",
    ),
    ExpansionDomain.BACKEND: (
        "routing", "fusion", "provider", "fallback", "threshold",
        "endpoint", "ipc", "websocket", "streaming", "backend", "handler",
        "payload",
    ),
}

# Substrings of a routing intent that settle the domain on their own. The
# router's own intents ("weather.query") are matched here, but so is any
# other string a caller passes: intents are configuration, and matching on
# substrings keeps a newly added one from silently falling through.
INTENT_MARKERS: tuple[tuple[str, ExpansionDomain], ...] = (
    ("weather", ExpansionDomain.WEATHER),
    ("unity", ExpansionDomain.UNITY),
    ("aria", ExpansionDomain.ARIA_INTERNAL),
    ("backend", ExpansionDomain.BACKEND),
    ("routing", ExpansionDomain.BACKEND),
)

# The order markers are tried in. Fixed rather than by match count, so the
# answer never depends on how many words happened to land: a query naming a
# prefab and a websocket is a Unity question about a websocket.
_MARKER_PRIORITY = (
    ExpansionDomain.ARIA_INTERNAL,
    ExpansionDomain.UNITY,
    ExpansionDomain.WEATHER,
    ExpansionDomain.BACKEND,
)


def detect_domain(raw_query: str, routing_intent: str | None = None) -> ExpansionDomain:
    """Decide which vocabulary a query is speaking.

    The router's intent wins where it has an opinion -- it saw the whole
    message and any conversation history, which this sees neither of. It
    routinely has none: the router has no Unity intent, and "chat.general"
    says only that nothing else matched. The query's own words are the
    fallback, tried in a fixed priority order.

    Returns GENERAL when nothing matches, which is not a failure. Most
    queries are general, and GENERAL is a full table rather than an empty
    one.
    """
    intent = str(routing_intent or "").lower()
    for marker, domain in INTENT_MARKERS:
        if marker in intent:
            return domain

    # Padded on both sides so a marker matches whole words: "aria" must not
    # fire on "area", and "rain" would otherwise fire on "training".
    lowered = f" {' '.join(str(raw_query or '').lower().split())} "
    for domain in _MARKER_PRIORITY:
        for marker in DOMAIN_MARKERS[domain]:
            if f" {marker} " in lowered:
                return domain
    return ExpansionDomain.GENERAL


def table_for(domain: ExpansionDomain) -> ExpansionTable:
    """The expansion table a domain uses, GENERAL for anything unknown."""
    return TABLES.get(domain, GENERAL_EXPANSIONS)


# How much of the final query vector expansions are allowed to account for,
# relative to the base query's 1.0. Every expansion is scaled down together
# when their weights sum past this, so a query with one synonym and a query
# with six are both still mostly about what was typed.
#
# 0.5 is where this stops being free. Measured on bge-small over synonym and
# exact-match pairs, raising the mass keeps improving synonym queries the
# whole way up -- but the worst exact match stops gaining at 0.5 and starts
# losing beyond it:
#
#   mass   synonym avg   worst exact-match delta
#   0.25      0.6597            +0.0045
#   0.50      0.6824            +0.0024
#   0.75      0.6971            -0.0024
#   1.00      0.7070            -0.0080
#   3.00      0.7293            -0.0439
#
# So this is the most forgiveness available for no cost to the queries that
# were already right. Raise it only with the second column in view: past
# here, better synonym recall is being bought with exact-match precision.
#
# That "no cost" holds for GENERAL, whose synonyms are broad enough to sit
# near anything the base query is near. A domain table is narrower, and on a
# short exact-match query its synonyms do pull the vector off itself: worst
# case measured at -0.023, and no mass removes it (still -0.003 at 0.15).
# The mass stays at 0.5 anyway, because that cost buys an average +0.093 on
# in-domain wording and it comes out of headroom rather than margin -- the
# exact matches it touches score 0.79-0.93 against floors of 0.55-0.60.
# test_domain_expansion.py pins both halves of that trade.
EXPANSION_MASS = 0.5

# Ceiling on how many expansions any one query carries. Bounds both the
# vector drift above and the work: each surviving term is embedded.
MAX_EXPANSION_TERMS = 6

# Longest phrase key in any table, so the scanner knows how far ahead to
# look. Derived rather than written down, so adding a three-word key to a
# table is enough on its own.
_MAX_PHRASE_WORDS = max(
    len(key.split()) for table in TABLES.values() for key in table
)


def tokenize(text: str) -> list[str]:
    """Split a query into distinct, meaningful search terms.

    Lowercased, stopwords removed, duplicates dropped, and returned in order
    of first appearance so the same query always yields the same terms in
    the same order.
    """
    terms: list[str] = []
    for word in str(text or "").lower().split():
        term = word.strip(_EDGE_PUNCTUATION)
        if term and term not in STOPWORDS and term not in terms:
            terms.append(term)
    return terms


def expand_query(
    cleaned: str,
    domain: ExpansionDomain = ExpansionDomain.GENERAL,
) -> ExpansionResult:
    """Widen a de-framed query with related terms from its domain.

    Returns {"base", "terms", "expansions", "domain"}, where each expansion
    is {"term", "weight", "sources"}. sources maps each query term this
    stands in for to what it is worth as a stand-in for that particular
    term, which is what lets keyword coverage credit a synonym against the
    word it replaces rather than as extra evidence of its own. weight is the
    best of those, and is what the query vector uses -- a term is embedded
    once, however many words it covers.

    The two can differ. "release" is worth 0.7 as a stand-in for
    "deployment" and 0.5 for "pipeline"; a query holding both gets one
    "release" vector at 0.7, while coverage still credits the pipeline half
    at its own 0.5 rather than at the better term's rate.

    domain defaults to GENERAL, so a caller with no opinion gets the broad
    table rather than nothing. Callers that route -- search_hybrid and the
    two semantic searches -- pass what detect_domain decided.

    Expects the output of deframe_query. Expanding a framed question would
    look up its framing words, and "explain" is a word some table entry will
    eventually have an opinion about.

    Expansions are ordered by descending weight, ties broken alphabetically,
    and capped at MAX_EXPANSION_TERMS, so the same query and domain always
    produce the same list in the same order.
    """
    base = str(cleaned or "")
    terms = tokenize(base)
    table = table_for(domain)

    # Terms the user actually typed are never also expansions of themselves:
    # "login credentials" must not re-suggest "credentials" as a synonym for
    # "login", which would count the same word twice in the vector.
    typed = set(terms)

    # term -> {query term it stands in for: what it is worth for that term}.
    sources: dict[str, dict[str, float]] = {}

    for phrase, covered in _matched_keys(terms, table):
        for term, weight in table[phrase].items():
            if term in typed:
                continue
            per_source = sources.setdefault(term, {})
            for source in covered:
                # A phrase key stands in for every term it consumed, and the
                # best entry wins when two keys suggest the same synonym.
                per_source[source] = max(per_source.get(source, 0.0), weight)

    best = {term: max(per_source.values()) for term, per_source in sources.items()}
    ordered = sorted(best.items(), key=lambda pair: (-pair[1], pair[0]))
    expansions = [
        {"term": term, "weight": weight, "sources": dict(sources[term])}
        for term, weight in ordered[:MAX_EXPANSION_TERMS]
    ]
    return {
        "base": base,
        "terms": terms,
        "expansions": expansions,
        "domain": domain,
    }


def _matched_keys(
    terms: list[str],
    table: ExpansionTable,
) -> list[tuple[str, tuple[str, ...]]]:
    """Table keys present in the term list, longest phrase first.

    Returns (key, terms it consumed). Scanning longest-first and consuming
    what matched is what makes "coffee machine" one concept: were both
    lengths allowed to fire, the query would also carry the expansions of
    "coffee" and of "machine", and the phrase entry would be pointless.
    """
    matches: list[tuple[str, tuple[str, ...]]] = []
    index = 0
    while index < len(terms):
        for size in range(min(_MAX_PHRASE_WORDS, len(terms) - index), 0, -1):
            window = tuple(terms[index:index + size])
            if " ".join(window) in table:
                matches.append((" ".join(window), window))
                index += size
                break
        else:
            index += 1
    return matches


def expansion_vector(expansion: ExpansionResult) -> list[float] | None:
    """Combine a base query and its expansions into one query vector.

    The base carries weight 1.0 and the expansions share EXPANSION_MASS
    between them, scaled down proportionally when their own weights sum past
    it. The result is re-normalized, because cosine_similarity compares
    directions and every stored vector is unit length.

    Returns None when there is nothing to expand, which is the signal to the
    caller to embed the query the way it always did. That path matters: a
    query with no dictionary hit must produce byte-identical results to
    before expansion existed, not merely similar ones.
    """
    terms = expansion["expansions"]
    if not terms:
        return None

    total = sum(item["weight"] for item in terms)
    scale = min(1.0, EXPANSION_MASS / total) if total else 0.0

    vectors = semantic_embeddings.embed_texts_semantic(
        [expansion["base"]] + [item["term"] for item in terms]
    )
    combined = list(vectors[0])
    for item, vector in zip(terms, vectors[1:]):
        weight = item["weight"] * scale
        for position, value in enumerate(vector):
            combined[position] += weight * value

    # Normalized here rather than through semantic_embeddings, whose
    # equivalent is private and rounds to the float32 the database stores.
    # This vector is never stored -- it is compared and discarded -- so it
    # has no storage width to match.
    norm = math.sqrt(sum(value * value for value in combined))
    if norm == 0.0:
        return combined
    return [value / norm for value in combined]


def embed_expanded_query(query: str, routing_intent: str | None = None) -> list[float]:
    """De-frame, detect a domain, expand and embed a query in one step.

    The single entry point for query-side embedding: search_semantic and
    search_files_semantic both call this so the two halves of retrieval
    cannot drift into de-framing, routing or expanding differently.

    The domain is detected from the raw query rather than the cleaned one.
    Framing is stripped before expansion, but it is still evidence of what
    is being asked, and "tell me about my Unity prefabs" should reach the
    Unity table whichever word carried the clue.

    Indexing deliberately does not go through here. Expanding a stored chunk
    would bake one reading of its wording into the database permanently,
    where expanding a query is a decision that can be revisited on the next
    search.
    """
    cleaned = deframe_query(query)
    domain = detect_domain(query, routing_intent)
    vector = expansion_vector(expand_query(cleaned, domain))
    return vector if vector is not None else semantic_embeddings.embed_text_semantic(cleaned)
