# backend/tests/test_query_expansion.py
#
# Semantic query expansion: widening a de-framed query with the words it
# could have been asked with.
#
# Three layers are pinned here. The dictionary layer (expand_query) is pure
# and runs on any machine. The vector layer asserts the combination is
# weighted, bounded and normalized. The retrieval layer asserts the point of
# the whole exercise -- that a note written in one vocabulary is findable
# from another -- and the guardrail that matters more than any of it: an
# item that already matched exactly must not score worse than it did before
# expansion existed.

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
    EXPANSION_MASS,
    MAX_EXPANSION_TERMS,
    embed_expanded_query,
    expand_query,
    expansion_vector,
)

relevance = aria_memory._keyword_relevance

semantic_only = pytest.mark.skipif(
    not semantic_embeddings.is_semantic(),
    reason="active embedding backend is the lexical fallback, which cannot encode meaning",
)


def terms_of(query: str) -> list[str]:
    return [entry["term"] for entry in expand_query(query)["expansions"]]


def weight_of(query: str, term: str) -> float:
    return next(e["weight"] for e in expand_query(query)["expansions"] if e["term"] == term)


def notes_by_id(items) -> dict:
    return {item["id"]: item for item in items}


# ======================================================
# 1. Expansion behaviour
# ======================================================
def test_login_credentials_expands_to_password_and_account():
    expanded = terms_of("login credentials")
    assert "password" in expanded
    assert "account" in expanded


def test_deployment_pipeline_expands_to_release_and_rollout():
    expanded = terms_of("deployment pipeline")
    assert "release" in expanded
    assert "rollout" in expanded


def test_the_result_carries_the_base_and_its_terms():
    expanded = expand_query("login credentials")
    assert expanded["base"] == "login credentials"
    assert expanded["terms"] == ["login", "credentials"]


def test_every_expansion_has_a_term_and_a_weight():
    for entry in expand_query("login credentials")["expansions"]:
        assert isinstance(entry["term"], str) and entry["term"]
        assert 0.0 < entry["weight"] <= 1.0


def test_a_synonym_never_outweighs_a_typed_term():
    # The whole weighting scheme rests on this: nothing in any table may be
    # worth as much as a word the user actually chose.
    for table in query_expansion.TABLES.values():
        for expansions in table.values():
            for weight in expansions.values():
                assert 0.0 < weight < 1.0


def test_expansions_are_ordered_by_descending_weight():
    weights = [e["weight"] for e in expand_query("login credentials")["expansions"]]
    assert weights == sorted(weights, reverse=True)


def test_ties_are_broken_alphabetically():
    expansions = expand_query("deployment pipeline")["expansions"]
    tied = [e["term"] for e in expansions if e["weight"] == 0.7]
    assert tied == sorted(tied)


def test_the_number_of_expansions_is_capped():
    # "login credentials" alone offers more than the cap.
    assert len(expand_query("login credentials")["expansions"]) == MAX_EXPANSION_TERMS


def test_a_query_term_is_never_its_own_expansion():
    # "login" suggests "credentials", but the user already typed it.
    assert "credentials" not in terms_of("login credentials")
    assert "login" not in terms_of("login credentials")


def test_an_unknown_query_expands_to_nothing():
    expanded = expand_query("zebra giraffe")
    assert expanded["terms"] == ["zebra", "giraffe"]
    assert expanded["expansions"] == []


def test_an_empty_query_expands_to_nothing():
    assert expand_query("")["expansions"] == []
    assert expand_query(None)["terms"] == []


def test_stopwords_do_not_reach_the_table():
    assert expand_query("the login")["terms"] == ["login"]


def test_expansion_is_case_insensitive():
    assert terms_of("LOGIN") == terms_of("login")


# --- phrases ---
def test_a_phrase_key_expands_as_one_concept():
    expanded = terms_of("coffee machine")
    assert "espresso" in expanded
    assert "break room" in expanded


def test_a_phrase_wins_over_its_own_single_words():
    # "coffee" alone offers "caffeine"; the phrase entry does not, and the
    # phrase is what a two-word query should match.
    assert "caffeine" in terms_of("coffee")
    assert "caffeine" not in terms_of("coffee machine")


def test_a_phrase_is_found_inside_a_longer_query():
    assert "espresso" in terms_of("coffee machine repair")


def test_acronyms_expand_to_their_long_form():
    assert "database" in terms_of("db")
    assert "configuration" in terms_of("config")
    assert "time off" in terms_of("pto")


# --- sources ---
def test_an_expansion_records_which_term_it_stands_in_for():
    sources = next(e for e in expand_query("login credentials")["expansions"]
                   if e["term"] == "password")["sources"]
    assert set(sources) == {"login", "credentials"}


def test_a_source_weight_is_specific_to_its_term():
    # "release" is a better stand-in for "deployment" (0.7) than for
    # "pipeline" (0.5), and both are recorded rather than collapsed.
    release = next(e for e in expand_query("deployment pipeline")["expansions"]
                   if e["term"] == "release")
    assert release["sources"]["deployment"] == 0.7
    assert release["sources"]["pipeline"] == 0.5
    assert release["weight"] == 0.7


def test_the_headline_weight_is_the_best_of_the_sources():
    for entry in expand_query("deployment pipeline")["expansions"]:
        assert entry["weight"] == max(entry["sources"].values())


# --- determinism ---
def test_expansion_is_deterministic():
    assert expand_query("login credentials") == expand_query("login credentials")


def test_expansion_is_deterministic_across_many_calls():
    runs = [terms_of("deployment pipeline") for _ in range(5)]
    assert all(run == runs[0] for run in runs)


def test_term_order_does_not_change_the_expansion_set():
    assert set(terms_of("login credentials")) == set(terms_of("credentials login"))


def test_expansion_does_not_mutate_the_table():
    before = {
        domain: {key: dict(value) for key, value in table.items()}
        for domain, table in query_expansion.TABLES.items()
    }
    expand_query("login credentials deployment pipeline coffee machine")
    assert query_expansion.TABLES == before


# --- no LLM calls ---
def test_expansion_needs_no_model_at_all(monkeypatch):
    # Pure dictionary work: it must not reach for an embedding backend, let
    # alone a chat model.
    def explode(*args, **kwargs):
        raise AssertionError("expansion must not touch an embedding backend")

    monkeypatch.setattr(semantic_embeddings, "active_backend", explode)
    monkeypatch.setattr(semantic_embeddings, "embed_texts_semantic", explode)
    assert "password" in terms_of("login credentials")


def test_the_module_does_not_import_an_llm_engine():
    # Read off the import statements rather than the raw text: the module
    # legitimately writes "provider" in a comment and in the backend table,
    # and a substring search cannot tell prose from a dependency.
    import ast
    import inspect

    imported = set()
    for node in ast.walk(ast.parse(inspect.getsource(query_expansion))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    assert not any("llm_engine" in name or "providers" in name for name in imported)
    assert imported == {
        "__future__", "math", "enum", "typing",
        "backend.llm", "backend.llm.query_deframing",
        "llm", "llm.query_deframing",
    }


# ======================================================
# 2. The query vector
# ======================================================
def test_an_unexpanded_query_falls_back_to_the_plain_vector():
    # Byte-identical, not merely close: a query the table does not know must
    # behave exactly as it did before expansion existed.
    assert expansion_vector(expand_query("zebra giraffe")) is None
    assert embed_expanded_query("zebra giraffe") == semantic_embeddings.embed_text_semantic(
        "zebra giraffe"
    )


def test_the_expanded_vector_is_unit_length():
    vector = embed_expanded_query("login credentials")
    assert sum(value * value for value in vector) == pytest.approx(1.0, abs=1e-6)


def test_the_expanded_vector_has_the_backends_width():
    assert len(embed_expanded_query("login credentials")) == semantic_embeddings.dimension()


def test_the_expanded_vector_differs_from_the_bare_one():
    assert embed_expanded_query("login credentials") != semantic_embeddings.embed_text_semantic(
        "login credentials"
    )


def test_the_query_is_de_framed_before_it_is_expanded(monkeypatch):
    seen = []
    real = semantic_embeddings.embed_texts_semantic
    monkeypatch.setattr(
        semantic_embeddings, "embed_texts_semantic",
        lambda texts: (seen.extend(texts), real(texts))[1],
    )
    embed_expanded_query("tell me about the login credentials")
    assert seen[0] == "login credentials"


def test_the_base_query_dominates_the_vector():
    # Closer to what was typed than to any single synonym: the combination
    # must not drift off the topic it started from.
    base = semantic_embeddings.embed_text_semantic("login credentials")
    expanded = embed_expanded_query("login credentials")
    to_base = semantic_embeddings.cosine_similarity(expanded, base)
    for entry in expand_query("login credentials")["expansions"]:
        synonym = semantic_embeddings.embed_text_semantic(entry["term"])
        assert to_base > semantic_embeddings.cosine_similarity(expanded, synonym)


def test_expansion_mass_bounds_how_far_the_vector_can_move(monkeypatch):
    # With the mass at zero the expansions cannot contribute, so the result
    # is the base query again -- which is what makes the constant the single
    # dial on how forgiving retrieval is.
    monkeypatch.setattr(query_expansion, "EXPANSION_MASS", 0.0)
    expanded = embed_expanded_query("login credentials")
    base = semantic_embeddings.embed_text_semantic("login credentials")
    assert semantic_embeddings.cosine_similarity(expanded, base) == pytest.approx(1.0, abs=1e-6)


def test_the_default_mass_keeps_the_base_in_the_majority():
    assert 0.0 < EXPANSION_MASS < 1.0


def test_the_vector_is_deterministic():
    assert embed_expanded_query("login credentials") == embed_expanded_query("login credentials")


# ======================================================
# 2b. Semantic improvement
# ======================================================
def similarity(query_vector, text) -> float:
    return semantic_embeddings.cosine_similarity(
        query_vector, semantic_embeddings.embed_text_semantic(text)
    )


SYNONYM_PAIRS = [
    ("login credentials", "the password policy requires a new passphrase every 90 days"),
    ("login credentials", "how to sign in and reset your account"),
    ("deployment pipeline", "the release was rolled out to production last night"),
    ("coffee machine", "the espresso maker in the break room is broken again"),
    ("config", "the configuration file holds every setting"),
    ("db", "the database is a sqlite file on disk"),
]

EXACT_PAIRS = [
    ("login credentials", "your login is stored separately from your account credentials"),
    ("deployment pipeline", "the deployment pipeline runs nightly"),
    ("coffee machine", "the coffee machine needs descaling"),
]


@semantic_only
@pytest.mark.parametrize("query,text", SYNONYM_PAIRS)
def test_a_query_worded_differently_scores_higher_after_expansion(query, text):
    bare = similarity(semantic_embeddings.embed_text_semantic(query), text)
    expanded = similarity(embed_expanded_query(query), text)
    assert expanded > bare


@semantic_only
@pytest.mark.parametrize("query,text", EXACT_PAIRS)
def test_expansion_does_not_cost_an_exact_match(query, text):
    # The guardrail. Widening a query is only worth doing if it cannot make
    # the queries that were already right any worse.
    bare = similarity(semantic_embeddings.embed_text_semantic(query), text)
    expanded = similarity(embed_expanded_query(query), text)
    assert expanded >= bare


@semantic_only
def test_a_synonym_note_can_cross_the_retrieval_floor(db):
    # Concretely what this buys: a note that scored under the file floor on
    # its own wording clears it once the query is expanded.
    text = "the release was rolled out to production last night"
    bare = similarity(semantic_embeddings.embed_text_semantic("deployment pipeline"), text)
    expanded = similarity(embed_expanded_query("deployment pipeline"), text)
    assert bare < ingestion.DEFAULT_MIN_SCORE <= expanded


# ======================================================
# 3. Hybrid retrieval and coverage
# ======================================================
def test_a_synonym_earns_partial_keyword_credit():
    # "sign in" stands in for "login" only, at 0.7, over two query terms.
    assert relevance("login credentials", {"text": "please sign in here"}) == pytest.approx(0.35)


def test_a_synonym_covering_both_terms_credits_both():
    # "password" is a stand-in for "login" and for "credentials" alike, so a
    # note that says it covers the query twice over at 0.7 each.
    assert relevance("login credentials", {"text": "the password policy"}) == pytest.approx(0.7)


def test_a_typed_term_still_earns_full_credit():
    assert relevance("login credentials", {"text": "login and credentials"}) == pytest.approx(1.0)


def test_a_synonym_cannot_add_to_a_term_already_present():
    # "sign in" stands in for "login", which this note already says; the
    # term is worth its full 1.0 and the synonym adds nothing on top, so
    # the missing half of the query is still missing.
    assert relevance("login credentials", {"text": "the login page, sign in here"}) == pytest.approx(0.5)
    assert relevance("login credentials", {"text": "the login page"}) == pytest.approx(0.5)


def test_the_best_synonym_wins_rather_than_the_sum():
    # Three stand-ins for "login" at once must not out-score having typed it.
    many = relevance("login", {"text": "sign in with your password for authentication"})
    assert many == pytest.approx(0.7)
    assert many < relevance("login", {"text": "the login page"})


def test_a_synonym_is_credited_at_its_rate_for_that_term():
    # "release" is worth 0.7 for "deployment" and 0.5 for "pipeline".
    assert relevance("deployment pipeline", {"text": "the release notes"}) == pytest.approx(0.6)


def test_an_unrelated_item_still_scores_zero():
    assert relevance("login credentials", {"text": "zebra giraffe"}) == 0.0


def test_a_synonym_in_the_tags_counts():
    assert relevance("login", {"text": "nothing", "tags": "password,auth"}) == pytest.approx(0.7)


def test_a_synonym_matches_as_a_whole_word():
    # "account" must not be satisfied by "accounting".
    assert relevance("login", {"text": "the accounting department"}) == 0.0


def test_coverage_is_deterministic():
    item = {"text": "the password policy"}
    assert relevance("login credentials", item) == relevance("login credentials", item)


def test_a_note_using_only_expanded_terms_is_retrieved(db):
    note_id = notes_store.save_note("the password policy was updated in March")
    semantic.index_note(note_id)

    items = notes_by_id(aria_memory.search_hybrid("login credentials", min_score=0.0))
    assert note_id in items
    assert items[note_id]["keyword_score"] > 0.0


@semantic_only
def test_a_synonym_note_outranks_an_unrelated_one(db):
    relevant = notes_store.save_note("the password policy was updated in March")
    unrelated = notes_store.save_note("the office plants need watering on Fridays")
    for note_id in (relevant, unrelated):
        semantic.index_note(note_id)

    ranked = aria_memory.search_ranked("login credentials")
    order = [item["id"] for item in ranked]
    assert order.index(relevant) < order.index(unrelated)


def test_an_exactly_matching_note_still_outranks_a_synonym_one(db):
    exact = notes_store.save_note("your login credentials are stored here")
    synonym = notes_store.save_note("the password policy was updated in March")
    for note_id in (exact, synonym):
        semantic.index_note(note_id)

    items = notes_by_id(aria_memory.search_hybrid("login credentials", min_score=0.0))
    assert items[exact]["keyword_score"] > items[synonym]["keyword_score"]


def test_hybrid_results_are_deterministic(db):
    note_id = notes_store.save_note("the password policy was updated in March")
    semantic.index_note(note_id)

    first = aria_memory.search_hybrid("login credentials", min_score=0.0)
    second = aria_memory.search_hybrid("login credentials", min_score=0.0)
    assert [i["keyword_score"] for i in first] == [i["keyword_score"] for i in second]


def test_the_expansion_is_built_once_per_search(db, monkeypatch):
    for text in ["login one", "login two", "the password policy"]:
        semantic.index_note(notes_store.save_note(text))

    calls = []
    real = aria_memory.expand_query
    monkeypatch.setattr(
        aria_memory,
        "expand_query",
        lambda cleaned, domain: (calls.append(cleaned), real(cleaned, domain))[1],
    )
    aria_memory.search_hybrid("login credentials", min_score=0.0)
    assert len(calls) == 1


# ======================================================
# 4. Constraints
# ======================================================
def test_ranking_weights_are_unchanged():
    assert memory_ranking.WEIGHT_SEMANTIC == 0.45
    assert memory_ranking.WEIGHT_KEYWORD == 0.25
    assert memory_ranking.WEIGHT_RECENCY == 0.20
    assert memory_ranking.WEIGHT_TYPE == 0.10


def test_score_floors_are_unchanged():
    assert semantic.DEFAULT_MIN_SCORE == 0.6
    assert ingestion.DEFAULT_MIN_SCORE == 0.55


def test_the_search_notes_tool_is_still_a_substring_search(db):
    # Expansion is a retrieval-side change. The public tool was left alone
    # and must not start returning synonym matches.
    from backend.core.tool_registry import execute_tool

    notes_store.save_note("the password policy was updated in March")
    assert execute_tool("search_notes", {"query": "login"}).value["count"] == 0
    assert execute_tool("search_notes", {"query": "password"}).value["count"] == 1


def test_indexing_does_not_expand(db, monkeypatch):
    # Only queries are widened. Expanding a chunk on the way in would bake
    # one reading of its wording into the database permanently.
    def explode(*args, **kwargs):
        raise AssertionError("indexing must not expand")

    monkeypatch.setattr(query_expansion, "expand_query", explode)
    note_id = notes_store.save_note("the release pipeline runs nightly")
    assert semantic.index_note(note_id) > 0
