# backend/tests/test_domain_expansion.py
#
# Domain-adaptive expansion: which synonyms a query gets depends on what it
# is talking about.
#
# The interesting cases are the ones where a word means two things. "scene"
# is a level to Unity and a place to everyone else; "asset" is a prefab or a
# balance-sheet entry; "routing" is a backend concern and also a word about
# ARIA's own dispatcher. A flat table had to pick one reading for each of
# them, so these tests pin the choice, the detection that drives it, and the
# guarantee that scoping expansion did not cost the queries that were
# already working.

from __future__ import annotations

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory
from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.aria_memory import memory_ranking
from backend.files import file_ingestion as ingestion
from backend.llm import query_expansion
from backend.llm import semantic_embeddings
from backend.llm.query_expansion import (
    ExpansionDomain,
    detect_domain,
    embed_expanded_query,
    expand_query,
    expansion_vector,
    table_for,
)

D = ExpansionDomain

semantic_only = pytest.mark.skipif(
    not semantic_embeddings.is_semantic(),
    reason="active embedding backend is the lexical fallback, which cannot encode meaning",
)


def terms_of(query: str, domain: ExpansionDomain) -> list[str]:
    return [entry["term"] for entry in expand_query(query, domain)["expansions"]]


def relevance(query: str, item: dict, domain: ExpansionDomain) -> float:
    return aria_memory._keyword_relevance(query, item, expand_query(query, domain))


def vector_for(query: str, domain: ExpansionDomain) -> list[float]:
    vector = expansion_vector(expand_query(query, domain))
    return vector if vector is not None else semantic_embeddings.embed_text_semantic(query)


def similarity(query: str, text: str, domain: ExpansionDomain) -> float:
    return semantic_embeddings.cosine_similarity(
        vector_for(query, domain), semantic_embeddings.embed_text_semantic(text)
    )


def notes_by_id(items) -> dict:
    return {item["id"]: item for item in items}


# ======================================================
# 1. Domain selection
# ======================================================
@pytest.mark.parametrize("query", [
    "how do I instance a prefab",
    "the shader will not compile",
    "my gameobject lost its monobehaviour",
    "addressables versus assetbundle",
])
def test_unity_queries_select_unity(query):
    assert detect_domain(query) is D.UNITY


@pytest.mark.parametrize("query", [
    "what is the forecast tomorrow",
    "storm radar over the county",
    "how much precipitation this week",
    "the temperature tonight",
])
def test_weather_queries_select_weather(query):
    assert detect_domain(query) is D.WEATHER


@pytest.mark.parametrize("query", [
    "the routing layer picks a provider",
    "websocket streaming keeps timing out",
    "which fallback fires when the endpoint is down",
])
def test_backend_queries_select_backend(query):
    assert detect_domain(query) is D.BACKEND


@pytest.mark.parametrize("query", [
    "how does aria store notes",
    "the score floor for file search",
    "what does nl routing do",
])
def test_aria_queries_select_aria_internal(query):
    assert detect_domain(query) is D.ARIA_INTERNAL


@pytest.mark.parametrize("query", [
    "the coffee machine is broken",
    "when is my vacation",
    "login credentials",
    "zebra giraffe",
    "",
])
def test_everything_else_selects_general(query):
    assert detect_domain(query) is D.GENERAL


# --- the router's opinion wins ---
def test_a_weather_intent_selects_weather():
    # The router saw the whole message; a bare city name gives this nothing.
    assert detect_domain("what about Paris", "weather.query") is D.WEATHER


def test_an_intent_beats_the_querys_own_words():
    assert detect_domain("prefab", "weather.query") is D.WEATHER


def test_an_intent_with_no_opinion_falls_through_to_the_words():
    assert detect_domain("my prefab broke", "chat.general") is D.UNITY
    assert detect_domain("my prefab broke", "code.help") is D.UNITY


def test_an_unknown_intent_falls_through_rather_than_failing():
    assert detect_domain("the coffee machine", "something.new") is D.GENERAL


def test_no_intent_is_the_same_as_none():
    assert detect_domain("my prefab broke") is detect_domain("my prefab broke", None)


# --- detection is careful about word boundaries ---
def test_a_marker_does_not_fire_inside_a_longer_word():
    # "aria" must not fire on "area", nor "rain" on "training".
    assert detect_domain("the area of the room") is D.GENERAL
    assert detect_domain("training the new hire") is D.GENERAL


def test_detection_is_case_insensitive():
    assert detect_domain("My Unity Prefab") is D.UNITY


def test_detection_is_deterministic():
    assert all(detect_domain("prefab shader") is D.UNITY for _ in range(5))


def test_priority_is_fixed_not_by_count():
    # Three backend markers and one Unity marker: Unity still wins, because
    # the answer must not depend on how many words happened to land.
    query = "the prefab routing provider endpoint"
    assert detect_domain(query) is D.UNITY


def test_aria_outranks_backend_on_a_shared_word():
    # "routing" belongs to both; "nl routing" is unambiguous.
    assert detect_domain("nl routing") is D.ARIA_INTERNAL
    assert detect_domain("routing") is D.BACKEND


# ======================================================
# 2. Domain-specific expansions
# ======================================================
def test_prefab_expands_only_in_unity():
    assert "gameobject" in terms_of("prefab", D.UNITY)
    for domain in (D.GENERAL, D.WEATHER, D.BACKEND, D.ARIA_INTERNAL):
        assert expand_query("prefab", domain)["expansions"] == []


def test_forecast_expands_only_in_weather():
    assert "precipitation" in terms_of("forecast", D.WEATHER)
    for domain in (D.GENERAL, D.UNITY, D.BACKEND, D.ARIA_INTERNAL):
        assert expand_query("forecast", domain)["expansions"] == []


def test_routing_expands_only_in_backend_and_aria():
    assert "intent" in terms_of("routing", D.BACKEND)
    for domain in (D.GENERAL, D.UNITY, D.WEATHER):
        assert expand_query("routing", domain)["expansions"] == []


def test_each_domains_headline_entries_are_present():
    assert "assetbundle" in terms_of("build pipeline", D.UNITY)
    assert "floor" in terms_of("threshold", D.BACKEND)
    assert "reflectivity" in terms_of("radar", D.WEATHER)
    assert "threshold" in terms_of("score floor", D.ARIA_INTERNAL)


def test_the_result_reports_the_domain_it_used():
    assert expand_query("prefab", D.UNITY)["domain"] is D.UNITY
    assert expand_query("prefab", D.GENERAL)["domain"] is D.GENERAL


def test_the_domain_defaults_to_general():
    assert expand_query("login credentials")["domain"] is D.GENERAL
    assert expand_query("login credentials") == expand_query("login credentials", D.GENERAL)


def test_a_phrase_key_still_wins_inside_a_domain():
    # "build pipeline" is one Unity concept, not "build" plus "pipeline".
    assert "assetbundle" in terms_of("build pipeline", D.UNITY)
    assert "compile" not in terms_of("build pipeline", D.UNITY)


# --- shared vocabulary ---
@pytest.mark.parametrize("domain", list(ExpansionDomain))
def test_general_engineering_words_expand_in_every_domain(domain):
    # Choosing a domain must not cost a query the words that mean the same
    # thing everywhere -- a Unity error is still an exception.
    assert "exception" in terms_of("error", domain)
    assert "configuration" in terms_of("config", domain)


@pytest.mark.parametrize("domain", list(ExpansionDomain))
def test_shared_entries_are_identical_across_domains(domain):
    assert table_for(domain)["error"] == query_expansion.GENERAL_EXPANSIONS["error"]


# ======================================================
# 3. No cross-domain pollution
# ======================================================
def test_a_weather_query_gets_no_unity_expansions():
    unity_only = {"gameobject", "prefab", "monobehaviour", "assetbundle"}
    assert not unity_only & set(terms_of("the scene outside looks stormy", D.WEATHER))


def test_a_backend_query_gets_no_coffee_machine_expansions():
    assert "espresso" in terms_of("coffee machine", D.GENERAL)
    assert terms_of("coffee machine", D.BACKEND) == []


def test_a_general_query_gets_no_unity_reading_of_an_ordinary_word():
    # "scene" and "asset" are ordinary English. Outside Unity they must not
    # drag a query toward gameobjects.
    assert expand_query("the scene of the crime", D.GENERAL)["expansions"] == []
    assert expand_query("asset write-down", D.GENERAL)["expansions"] == []


def test_a_unity_query_does_not_get_weather_expansions():
    assert "reflectivity" not in terms_of("the storm scene in the level", D.UNITY)


def test_domain_tables_do_not_leak_into_each_other():
    for domain, table in query_expansion.TABLES.items():
        if domain is D.GENERAL:
            continue
        shared = set(query_expansion.GENERAL_EXPANSIONS) & set(table)
        # Whatever a domain shares with GENERAL is the shared block, by
        # construction -- never a coincidental overlap.
        assert shared == set(query_expansion._SHARED)


# ======================================================
# 4. Semantic gains
# ======================================================
IN_DOMAIN = [
    ("prefab workflow", "the gameobject was saved as a reusable asset", D.UNITY),
    ("scene loading", "the level takes ages to stream in", D.UNITY),
    ("build pipeline", "we ship addressables and assetbundles nightly", D.UNITY),
    ("routing", "the intent classifier picks a provider", D.BACKEND),
    ("threshold", "the score floor was raised to 0.6", D.BACKEND),
    ("fusion", "the provider fallback chain retries once", D.BACKEND),
    ("forecast", "heavy precipitation and falling temperature tonight", D.WEATHER),
    ("radar", "storm reflectivity over the county", D.WEATHER),
    ("station", "the noaa grid observation point", D.WEATHER),
    ("semantic", "the embedding vector width is 384", D.ARIA_INTERNAL),
    ("score floor", "the retrieval threshold and cutoff", D.ARIA_INTERNAL),
]


@semantic_only
@pytest.mark.parametrize("query,text,domain", IN_DOMAIN)
def test_the_right_domain_scores_better_than_general(query, text, domain):
    # Measured gains on bge-small run from +0.04 to +0.15; the floor asserted
    # here is the specified +0.03, so a table edit that merely trims the
    # margin does not fail the suite while a regression does.
    gain = similarity(query, text, domain) - similarity(query, text, D.GENERAL)
    assert gain >= 0.03


@pytest.mark.parametrize("query,domain", [
    ("forecast", D.UNITY),
    ("prefab", D.WEATHER),
    ("radar", D.BACKEND),
    ("coffee machine", D.BACKEND),
])
def test_outside_its_domain_a_query_is_left_alone(query, domain):
    # Not merely close -- identical. A term the domain's table does not know
    # produces no expansions, so the vector is the plain one.
    assert expand_query(query, domain)["expansions"] == []
    assert vector_for(query, domain) == semantic_embeddings.embed_text_semantic(query)


# ======================================================
# 5. Exact-match invariants
# ======================================================
@pytest.mark.parametrize("query,text,domain", [
    ("prefab", "the prefab was updated", D.UNITY),
    ("forecast", "the forecast for tomorrow", D.WEATHER),
    ("routing", "the routing table", D.BACKEND),
])
def test_an_exact_keyword_match_is_untouched_by_domain(query, text, domain):
    # A term the user typed is worth 1.0 whichever table is loaded.
    assert relevance(query, {"text": text}, domain) == pytest.approx(1.0)
    assert relevance(query, {"text": text}, D.GENERAL) == pytest.approx(1.0)


# What an exact semantic match may lose to expansion. Unlike the general
# table -- where 0.5 was chosen precisely because it cost exact matches
# nothing -- a domain table's synonyms are narrow enough to pull a short
# exact-match query measurably off itself, and no mass removes that entirely
# (at 0.15 it is still -0.003). Measured worst case at the shipped mass is
# -0.023 against an average in-domain gain of +0.093, so the trade is kept
# and bounded here rather than wished away. It is safe because it is small
# relative to the headroom: the exact matches below score 0.79-0.93 against
# floors of 0.55-0.60, which the next test pins.
MAX_EXACT_MATCH_COST = 0.03

EXACT_SEMANTIC = [
    ("prefab workflow", "the prefab workflow is documented here", D.UNITY),
    ("radar", "the radar is offline", D.WEATHER),
    ("forecast", "the forecast for tomorrow", D.WEATHER),
    ("routing", "the routing table", D.BACKEND),
]


@semantic_only
@pytest.mark.parametrize("query,text,domain", EXACT_SEMANTIC)
def test_an_exact_semantic_match_loses_almost_nothing(query, text, domain):
    bare = semantic_embeddings.cosine_similarity(
        semantic_embeddings.embed_text_semantic(query),
        semantic_embeddings.embed_text_semantic(text),
    )
    assert similarity(query, text, domain) >= bare - MAX_EXACT_MATCH_COST


@semantic_only
@pytest.mark.parametrize("query,text,domain", EXACT_SEMANTIC)
def test_an_exact_match_still_clears_the_floor_by_a_wide_margin(query, text, domain):
    # Why the cost above is affordable: what it eats into is headroom, not
    # the margin that decides whether a chunk is retrieved at all.
    assert similarity(query, text, domain) > semantic.DEFAULT_MIN_SCORE + 0.1


@semantic_only
def test_the_gain_outweighs_the_exact_match_cost():
    # The trade, stated as a test: what the right domain wins on a
    # differently-worded note is worth several times what it costs a note
    # that already matched.
    gains = [similarity(q, t, d) - similarity(q, t, D.GENERAL) for q, t, d in IN_DOMAIN]
    costs = [
        semantic_embeddings.cosine_similarity(
            semantic_embeddings.embed_text_semantic(q),
            semantic_embeddings.embed_text_semantic(t),
        ) - similarity(q, t, d)
        for q, t, d in EXACT_SEMANTIC
    ]
    assert sum(gains) / len(gains) > 3 * max(costs)


@pytest.mark.parametrize("domain", list(ExpansionDomain))
def test_an_unknown_query_returns_an_identical_vector_in_every_domain(domain):
    assert expand_query("zebra giraffe", domain)["expansions"] == []
    assert vector_for("zebra giraffe", domain) == semantic_embeddings.embed_text_semantic(
        "zebra giraffe"
    )


def test_an_unrelated_item_still_scores_zero_in_every_domain():
    for domain in ExpansionDomain:
        assert relevance("prefab", {"text": "zebra giraffe"}, domain) == 0.0


# ======================================================
# 6. Hybrid scoring
# ======================================================
def test_a_synonym_counts_only_inside_its_domain():
    item = {"text": "the gameobject was updated"}
    assert relevance("prefab", item, D.UNITY) == pytest.approx(0.7)
    assert relevance("prefab", item, D.GENERAL) == 0.0


def test_coverage_is_still_capped_at_one_per_term():
    # "gameobject" and "asset" both stand in for "prefab"; the best one
    # counts, not the sum.
    item = {"text": "the gameobject and the asset"}
    assert relevance("prefab", item, D.UNITY) == pytest.approx(0.7)


def test_a_synonym_in_the_tags_counts_inside_the_domain():
    item = {"text": "nothing", "tags": "gameobject,unity"}
    assert relevance("prefab", item, D.UNITY) == pytest.approx(0.7)


def test_a_domain_synonym_matches_as_a_whole_word():
    assert relevance("prefab", {"text": "gameobjects everywhere"}, D.UNITY) == 0.0


def test_combined_moves_only_by_the_keyword_term():
    # Switching domains changes coverage and nothing else, so the combined
    # score moves by exactly WEIGHT_KEYWORD x delta / MAX_COMBINED.
    item = {"text": "the gameobject was updated"}
    off = relevance("prefab", item, D.GENERAL)
    on = relevance("prefab", item, D.UNITY)

    before = memory_ranking.normalize_scores(off, 0.7, 0.9, 1.0)
    after = memory_ranking.normalize_scores(on, 0.7, 0.9, 1.0)
    expected = memory_ranking.WEIGHT_KEYWORD * (on - off) / memory_ranking.MAX_COMBINED
    assert after - before == pytest.approx(expected)


def test_hybrid_routes_the_query_to_its_domain(db):
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    items = notes_by_id(aria_memory.search_hybrid("prefab", min_score=0.0))
    assert items[note_id]["keyword_score"] == pytest.approx(0.7)


def test_hybrid_honours_an_explicit_routing_intent(db):
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    # The intent says weather, so the Unity reading of "prefab" is not
    # loaded and the note earns no keyword credit. On a backend that scores
    # only shared words it does not come back at all, which is the same
    # point made harder -- without the expansion there is nothing linking
    # this note to the query.
    items = notes_by_id(
        aria_memory.search_hybrid("prefab", min_score=0.0, routing_intent="weather.query")
    )
    hit = items.get(note_id)
    assert hit is None or hit["keyword_score"] == 0.0


def test_search_ranked_passes_the_intent_through(db):
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    ranked = {item["id"]: item for item in aria_memory.search_ranked("prefab")}
    assert ranked[note_id]["keyword_score"] == pytest.approx(0.7)


def test_file_search_accepts_a_routing_intent(db, tmp_path):
    path = tmp_path / "unity.md"
    path.write_text("the gameobject was saved as a reusable asset", encoding="utf-8")
    ingestion.ingest_file(str(path))

    # Both spellings must work; the point is that the parameter exists and
    # does not disturb retrieval.
    assert ingestion.search_files_semantic("prefab", min_score=0.0) is not None
    assert ingestion.search_files_semantic(
        "prefab", min_score=0.0, routing_intent="unity"
    ) is not None


# ======================================================
# 7. Determinism
# ======================================================
def test_the_same_query_and_domain_expand_identically():
    assert expand_query("prefab shader", D.UNITY) == expand_query("prefab shader", D.UNITY)


def test_the_same_query_and_domain_embed_identically():
    assert vector_for("prefab shader", D.UNITY) == vector_for("prefab shader", D.UNITY)


def test_repeated_detection_and_embedding_agree():
    runs = [embed_expanded_query("how do I instance a prefab") for _ in range(3)]
    assert all(run == runs[0] for run in runs)


def test_expansions_are_ordered_deterministically():
    for domain in ExpansionDomain:
        weights = [e["weight"] for e in expand_query("error config build", domain)["expansions"]]
        assert weights == sorted(weights, reverse=True)


def test_hybrid_is_deterministic_under_a_domain(db):
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    runs = [
        [i["keyword_score"] for i in aria_memory.search_hybrid("prefab", min_score=0.0)]
        for _ in range(3)
    ]
    assert all(run == runs[0] for run in runs)


# ======================================================
# Constraints
# ======================================================
def test_ranking_weights_are_unchanged():
    assert memory_ranking.WEIGHT_SEMANTIC == 0.45
    assert memory_ranking.WEIGHT_KEYWORD == 0.25
    assert memory_ranking.WEIGHT_RECENCY == 0.20
    assert memory_ranking.WEIGHT_TYPE == 0.10


def test_score_floors_are_unchanged():
    assert semantic.DEFAULT_MIN_SCORE == 0.6
    assert ingestion.DEFAULT_MIN_SCORE == 0.55


def test_the_expansion_mass_is_unchanged():
    assert query_expansion.EXPANSION_MASS == 0.5
    assert query_expansion.MAX_EXPANSION_TERMS == 6


def test_detection_needs_no_model(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("domain detection must not touch an embedding backend")

    monkeypatch.setattr(semantic_embeddings, "active_backend", explode)
    monkeypatch.setattr(semantic_embeddings, "embed_texts_semantic", explode)
    assert detect_domain("how do I instance a prefab") is D.UNITY
    assert "gameobject" in terms_of("prefab", D.UNITY)


def test_every_domain_has_a_table():
    for domain in ExpansionDomain:
        assert table_for(domain)
