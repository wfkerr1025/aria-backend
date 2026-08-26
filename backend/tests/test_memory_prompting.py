# backend/tests/test_memory_prompting.py
#
# backend/llm/memory_prompting.py and backend/llm/llm_engine.py -- turning
# what ARIA remembers into the prompts it sends.
#
# The summarizer and prompt builders are pure functions over item dicts, so
# most tests hand them constructed items: that is the only way to assert
# "this summary says exactly what the fields say" without a live retrieval
# pass in the middle. The end-to-end tests at the bottom go through the real
# database, retrieval, ranking and embedding stack.

from __future__ import annotations

import re

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory_context as context_store
from backend import aria_memory_notes as notes_store
from backend import aria_memory_self as self_store
from backend import aria_memory_semantic_index as semantic
from backend.llm import llm_engine, memory_prompting, semantic_embeddings

BASE_PROMPT = "You are ARIA, a helpful assistant."


def item(**overrides) -> dict:
    base = {
        "id": overrides.pop("id", 1),
        "type": "note",
        "text": "the deployment pipeline runs nightly",
        "semantic_score": 0.7,
        "keyword_score": 0.4,
        "recency_score": 0.9,
    }
    base.update(overrides)
    return base


def context(**overrides) -> dict:
    base = {
        "query": "deployment schedule",
        "items": [item()],
        "summary": "a summary",
        "routing": {
            "semantic_used": True,
            "hybrid_used": True,
            "ranking_used": True,
            "backend": "test-backend",
            "embedding_dim": 384,
        },
        "self_knowledge": {"identity.name": "ARIA Lite"},
        "preferences": {"tone": "concise"},
    }
    base.update(overrides)
    return base


# ======================================================
# summarize_items
# ======================================================
def test_empty_items_summarize_to_an_empty_string():
    assert memory_prompting.summarize_items([]) == ""


def test_summary_is_deterministic():
    items = [item(id=1), item(id=2, type="chunk", text="another memory")]
    assert memory_prompting.summarize_items(items) == memory_prompting.summarize_items(items)


def test_summary_is_stable_across_repeated_calls():
    items = [item(id=n, semantic_score=n / 10) for n in range(1, 5)]
    summaries = {memory_prompting.summarize_items(items) for _ in range(5)}
    assert len(summaries) == 1


def test_summary_has_between_three_and_six_sentences():
    items = [item(id=n, text=f"memory number {n}") for n in range(1, 5)]
    sentences = [s for s in re.split(r"(?<=\.)\s+", memory_prompting.summarize_items(items)) if s]
    assert 3 <= len(sentences) <= 6


def test_a_single_item_still_gets_a_summary():
    summary = memory_prompting.summarize_items([item()])
    assert summary
    assert "1 memory item" in summary


def test_summary_reports_the_item_count():
    assert "3 memory items" in memory_prompting.summarize_items([item(id=n) for n in range(3)])


def test_summary_quotes_the_item_text():
    summary = memory_prompting.summarize_items([item(text="the coffee machine is broken")])
    assert "the coffee machine is broken" in summary


def test_summary_reports_the_item_type():
    assert "chunk" in memory_prompting.summarize_items([item(type="chunk")])


def test_summary_reports_the_scores():
    summary = memory_prompting.summarize_items([item(semantic_score=0.73, recency_score=0.88)])
    assert "0.73" in summary
    assert "0.88" in summary


def test_summary_only_uses_item_fields():
    # Nothing invented: every word of substance in the summary is either a
    # fixed template word or comes from an item. A token that appears in no
    # item must not appear in the summary.
    items = [item(text="alpha bravo charlie", type="note")]
    summary = memory_prompting.summarize_items(items)
    assert "delta" not in summary
    assert "echo" not in summary
    for token in ("alpha", "bravo", "charlie"):
        assert token in summary


def test_summary_changes_when_an_item_field_changes():
    # Proves the summary is derived from the fields rather than boilerplate.
    first = memory_prompting.summarize_items([item(text="original wording")])
    second = memory_prompting.summarize_items([item(text="different wording")])
    assert first != second


def test_summary_flags_stale_memory():
    fresh = memory_prompting.summarize_items([item(recency_score=0.95)])
    stale = memory_prompting.summarize_items([item(recency_score=0.05)])
    assert "old" not in fresh
    assert "old" in stale


def test_summary_counts_each_type():
    summary = memory_prompting.summarize_items(
        [item(id=1, type="note"), item(id=2, type="note"), item(id=3, type="chunk")]
    )
    assert "2 notes" in summary
    assert "1 chunk" in summary


def test_summary_handles_items_missing_scores():
    sparse = [{"id": 1, "type": "note", "text": "no scores at all"}]
    summary = memory_prompting.summarize_items(sparse)
    assert "no scores at all" in summary


def test_long_text_is_clipped():
    summary = memory_prompting.summarize_items([item(text="word " * 500)])
    assert len(summary) < 1200


def test_llm_summary_is_off_by_default(monkeypatch):
    # The default path must never call a model: it is on the critical path
    # of every turn, and a generated summary is neither deterministic nor
    # constrained to the item fields.
    called = []
    monkeypatch.setattr(
        memory_prompting, "summarize_items_with_llm", lambda items: called.append(1) or "generated"
    )
    memory_prompting.summarize_items([item()])
    assert called == []


def test_llm_summary_can_be_enabled(monkeypatch):
    monkeypatch.setenv(memory_prompting.ENV_LLM_SUMMARY, "1")
    monkeypatch.setattr(memory_prompting, "summarize_items_with_llm", lambda items: "generated")
    assert memory_prompting.summarize_items([item()]) == "generated"


def test_llm_summary_falls_back_when_it_returns_nothing(monkeypatch):
    monkeypatch.setenv(memory_prompting.ENV_LLM_SUMMARY, "1")
    monkeypatch.setattr(memory_prompting, "summarize_items_with_llm", lambda items: "")
    assert "memory item" in memory_prompting.summarize_items([item()])


# ======================================================
# build_system_prompt
# ======================================================
def test_system_prompt_keeps_the_base_prompt():
    assert memory_prompting.build_system_prompt(BASE_PROMPT, context()).startswith(BASE_PROMPT)


def test_system_prompt_includes_the_memory_section():
    prompt = memory_prompting.build_system_prompt(BASE_PROMPT, context())
    assert "### Memory Context" in prompt
    assert "Query: deployment schedule" in prompt
    assert "Summary: a summary" in prompt


def test_system_prompt_lists_items_with_every_score():
    prompt = memory_prompting.build_system_prompt(BASE_PROMPT, context())
    assert "the deployment pipeline runs nightly" in prompt
    assert "semantic=0.700" in prompt
    assert "keyword=0.400" in prompt
    assert "recency=0.900" in prompt
    assert "type=note" in prompt


def test_system_prompt_includes_routing_metadata():
    prompt = memory_prompting.build_system_prompt(BASE_PROMPT, context())
    assert "semantic_used=True" in prompt
    assert "hybrid_used=True" in prompt
    assert "ranking_used=True" in prompt
    assert "backend=test-backend" in prompt
    assert "embedding_dim=384" in prompt


def test_system_prompt_includes_self_knowledge():
    prompt = memory_prompting.build_system_prompt(BASE_PROMPT, context())
    assert "### Self Knowledge" in prompt
    assert "identity.name: ARIA Lite" in prompt


def test_system_prompt_includes_preferences():
    prompt = memory_prompting.build_system_prompt(BASE_PROMPT, context())
    assert "### User Preferences" in prompt
    assert "tone: concise" in prompt


def test_empty_sections_are_omitted_not_rendered_blank():
    # A heading with nothing under it invites the model to fill the gap.
    prompt = memory_prompting.build_system_prompt(
        BASE_PROMPT, context(items=[], summary="", self_knowledge={}, preferences={})
    )
    assert prompt == BASE_PROMPT
    assert "### Memory Context" not in prompt
    assert "### Self Knowledge" not in prompt


def test_system_prompt_is_deterministic():
    ctx = context()
    assert memory_prompting.build_system_prompt(BASE_PROMPT, ctx) == memory_prompting.build_system_prompt(
        BASE_PROMPT, ctx
    )


def test_mapping_order_does_not_depend_on_insertion_order():
    first = memory_prompting.build_system_prompt(BASE_PROMPT, context(preferences={"a": "1", "b": "2"}))
    second = memory_prompting.build_system_prompt(BASE_PROMPT, context(preferences={"b": "2", "a": "1"}))
    assert first == second


def test_system_prompt_tolerates_an_empty_context():
    assert memory_prompting.build_system_prompt(BASE_PROMPT, {}) == BASE_PROMPT


# ======================================================
# build_user_prompt
# ======================================================
def test_user_prompt_contains_the_message():
    prompt = memory_prompting.build_user_prompt("what runs nightly?", context())
    assert "### User Message" in prompt
    assert "what runs nightly?" in prompt


def test_user_prompt_includes_the_summary():
    prompt = memory_prompting.build_user_prompt("what runs nightly?", context())
    assert "### Relevant Memory Summary" in prompt
    assert "a summary" in prompt


def test_user_prompt_includes_the_items():
    prompt = memory_prompting.build_user_prompt("what runs nightly?", context())
    assert "### Relevant Items" in prompt
    assert "the deployment pipeline runs nightly" in prompt


def test_user_prompt_items_match_the_system_prompt_items():
    ctx = context()
    system = memory_prompting.build_system_prompt(BASE_PROMPT, ctx)
    user = memory_prompting.build_user_prompt("question", ctx)
    line = "the deployment pipeline runs nightly (semantic=0.700, keyword=0.400, recency=0.900, type=note)"
    assert line in system
    assert line in user


def test_user_prompt_without_memory_is_just_the_message():
    prompt = memory_prompting.build_user_prompt("plain question", context(items=[], summary=""))
    assert prompt.strip().endswith("plain question")
    assert "### Relevant Items" not in prompt


def test_user_prompt_is_deterministic():
    ctx = context()
    assert memory_prompting.build_user_prompt("q", ctx) == memory_prompting.build_user_prompt("q", ctx)


# ======================================================
# build_memory_context -- through the real stack
# ======================================================
def test_memory_context_has_every_key(db):
    ctx = memory_prompting.build_memory_context("anything")
    assert sorted(ctx) == [
        "items", "preferences", "query", "routing", "self_knowledge", "summary",
    ]


def test_memory_context_contains_ranked_items(db):
    note_id = notes_store.save_note("the deployment pipeline runs nightly")
    semantic.index_note(note_id)
    ctx = memory_prompting.build_memory_context("deployment pipeline")
    assert ctx["items"]
    assert all("combined_score" in row for row in ctx["items"])
    scores = [row["combined_score"] for row in ctx["items"]]
    assert scores == sorted(scores, reverse=True)


def test_memory_context_echoes_the_query(db):
    assert memory_prompting.build_memory_context("my question")["query"] == "my question"


def test_routing_metadata_is_correct(db):
    routing = memory_prompting.build_memory_context("anything")["routing"]
    assert routing["semantic_used"] is True
    assert routing["hybrid_used"] is True
    assert routing["ranking_used"] is True


def test_routing_reports_the_live_backend_and_dimension(db):
    routing = memory_prompting.build_memory_context("anything")["routing"]
    assert routing["backend"] == semantic_embeddings.backend_id()
    assert routing["embedding_dim"] == semantic_embeddings.dimension()


def test_memory_context_loads_self_knowledge(db):
    self_store.set_self("identity.name", "ARIA Lite")
    assert memory_prompting.build_memory_context("anything")["self_knowledge"] == {
        "identity.name": "ARIA Lite"
    }


def test_memory_context_loads_preferences(db):
    context_store.set_context("tone", "concise")
    assert memory_prompting.build_memory_context("anything")["preferences"] == {"tone": "concise"}


def test_memory_context_on_an_empty_store(db):
    ctx = memory_prompting.build_memory_context("nothing stored yet")
    assert ctx["items"] == []
    assert ctx["summary"] == ""
    assert ctx["self_knowledge"] == {}
    assert ctx["preferences"] == {}


def test_memory_context_respects_the_limit(db):
    for n in range(6):
        note_id = notes_store.save_note(f"deployment note number {n}")
        semantic.index_note(note_id)
    assert len(memory_prompting.build_memory_context("deployment note", limit=2)["items"]) <= 2


def test_memory_context_handles_mixed_types(db):
    note_id = notes_store.save_note("the deployment pipeline runs nightly")
    semantic.index_note(note_id)
    self_store.set_self("identity.name", "ARIA Lite")
    context_store.set_context("tone", "concise")

    ctx = memory_prompting.build_memory_context("deployment pipeline")
    prompt = memory_prompting.build_system_prompt(BASE_PROMPT, ctx)
    assert ctx["items"]
    assert "### Memory Context" in prompt
    assert "### Self Knowledge" in prompt
    assert "### User Preferences" in prompt


def test_a_mixed_item_list_summarizes(db):
    items = [
        item(id=1, type="note"),
        item(id=2, type="chunk", text="a fragment of a longer note"),
        item(id=3, type="self", text="ARIA Lite"),
    ]
    summary = memory_prompting.summarize_items(items)
    assert "3 memory items" in summary
    assert "chunk" in summary and "note" in summary and "self" in summary


# ======================================================
# llm_engine integration
# ======================================================
def test_engine_builds_a_system_prompt_with_memory(db):
    note_id = notes_store.save_note("the deployment pipeline runs nightly")
    semantic.index_note(note_id)
    prompt = llm_engine.build_system_prompt(BASE_PROMPT, query="deployment pipeline")
    assert prompt.startswith(BASE_PROMPT)
    assert "### Memory Context" in prompt


def test_engine_builds_a_user_prompt_with_memory(db):
    note_id = notes_store.save_note("the deployment pipeline runs nightly")
    semantic.index_note(note_id)
    prompt = llm_engine.build_user_prompt("what runs nightly?")
    assert "### User Message" in prompt
    assert "### Relevant Items" in prompt


def test_engine_returns_the_base_prompt_for_an_empty_query(db):
    assert llm_engine.build_system_prompt(BASE_PROMPT, query="") == BASE_PROMPT


def test_engine_can_be_disabled(db, monkeypatch):
    monkeypatch.setenv(llm_engine.ENV_MEMORY_ENABLED, "0")
    notes_store.save_note("the deployment pipeline runs nightly")
    assert llm_engine.build_system_prompt(BASE_PROMPT, query="deployment") == BASE_PROMPT
    assert llm_engine.build_user_prompt("hello") == "hello"


def test_engine_fails_open_when_retrieval_breaks(db, monkeypatch):
    # A memory failure must cost the user nothing but the memory.
    def explode(*_args, **_kwargs):
        raise RuntimeError("retrieval is down")

    monkeypatch.setattr(memory_prompting, "build_memory_context", explode)
    assert llm_engine.build_system_prompt(BASE_PROMPT, query="anything") == BASE_PROMPT
    assert llm_engine.build_user_prompt("hello") == "hello"


def test_build_turn_retrieves_once_and_shares_it(db, monkeypatch):
    note_id = notes_store.save_note("the deployment pipeline runs nightly")
    semantic.index_note(note_id)

    calls = []
    real = memory_prompting.build_memory_context
    monkeypatch.setattr(
        memory_prompting,
        "build_memory_context",
        lambda *a, **k: (calls.append(1), real(*a, **k))[1],
    )
    turn = llm_engine.build_turn(BASE_PROMPT, "what runs nightly?")
    assert len(calls) == 1
    assert turn["memory_context"] is not None
    assert "### Memory Context" in turn["system"]
    assert "### User Message" in turn["user"]


def test_conversation_manager_applies_memory(db):
    from backend.core import conversation_manager

    note_id = notes_store.save_note("the deployment pipeline runs nightly")
    semantic.index_note(note_id)

    messages = [{"role": "user", "content": "what runs nightly?"}]
    final, policy = conversation_manager.apply_history_policy(messages)
    assert final[0]["role"] == "system"
    assert "### Memory Context" in final[0]["content"]
    assert policy["memory_context_applied"] is True


def test_conversation_manager_still_works_without_memory(db, monkeypatch):
    from backend.core import conversation_manager

    monkeypatch.setenv(llm_engine.ENV_MEMORY_ENABLED, "0")
    messages = [{"role": "user", "content": "hello"}]
    final, policy = conversation_manager.apply_history_policy(messages)
    assert final[0]["content"] == conversation_manager.SYSTEM_PROMPT
    assert policy["memory_context_applied"] is False
    assert final[1:] == messages
