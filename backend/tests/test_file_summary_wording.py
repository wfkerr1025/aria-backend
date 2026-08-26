# backend/tests/test_file_summary_wording.py
#
# How summarize_items() words itself for different kinds of item.
#
# The structural properties of the summarizer are covered in
# test_memory_prompting.py; this file is about the prose. A bundle that is
# entirely file chunks is describing a document, so calling its contents
# "memory items" inside a ### File Context block reads wrong. A mixed bundle
# is still a memory bundle and keeps the memory wording, with each kind named
# in the breakdown.

from __future__ import annotations

import re

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.files import file_ingestion as ingestion
from backend.llm import llm_engine, memory_prompting

HANDBOOK = """# Deployment handbook

The release pipeline runs every night at 2am and publishes to staging.

## Passwords
To reset your account credentials, use the self-service portal.
Never share your login details with anyone.
"""


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


def file_chunk(**overrides) -> dict:
    return item(type="file_chunk", file_id=overrides.pop("file_id", 1), **overrides)


def sentences_in(summary: str) -> list[str]:
    return [s for s in re.split(r"(?<=\.)\s+", summary) if s]


# ======================================================
# File wording
# ======================================================
def test_a_single_file_chunk_uses_file_wording():
    summary = memory_prompting.summarize_items([file_chunk()])
    assert "file chunk" in summary
    assert "memory item" not in summary


def test_several_file_chunks_are_pluralized():
    summary = memory_prompting.summarize_items(
        [file_chunk(id=1), file_chunk(id=2), file_chunk(id=3)]
    )
    assert "3 relevant file chunks" in summary
    assert "memory item" not in summary


def test_a_single_chunk_is_not_pluralized():
    assert "1 relevant file chunk from" in memory_prompting.summarize_items([file_chunk()])


def test_the_file_count_is_reported():
    summary = memory_prompting.summarize_items(
        [file_chunk(id=1, file_id=1), file_chunk(id=2, file_id=1), file_chunk(id=3, file_id=2)]
    )
    assert "from 2 files" in summary


def test_one_file_is_not_pluralized():
    summary = memory_prompting.summarize_items([file_chunk(id=1), file_chunk(id=2)])
    assert "from 1 file." in summary


def test_file_chunks_without_a_file_id_still_get_file_wording():
    chunk = item(type="file_chunk")
    summary = memory_prompting.summarize_items([chunk])
    assert "file chunk" in summary
    assert "from" not in summary.split(".")[0]


def test_the_underscore_type_never_reaches_the_prose():
    # "file_chunk" is a column value, not English.
    summary = memory_prompting.summarize_items([file_chunk()])
    assert "file_chunk" not in summary


def test_the_body_describes_the_item_as_a_file_chunk():
    summary = memory_prompting.summarize_items([file_chunk(text="reset your credentials")])
    assert "a file chunk (semantic" in summary


# ======================================================
# Memory wording is unchanged
# ======================================================
def test_notes_still_use_memory_wording():
    summary = memory_prompting.summarize_items([item(), item(id=2)])
    assert "Recalled 2 memory items" in summary
    assert "file chunk" not in summary


def test_a_single_note_is_not_pluralized():
    assert "Recalled 1 memory item:" in memory_prompting.summarize_items([item()])


def test_note_chunks_still_use_memory_wording():
    summary = memory_prompting.summarize_items([item(type="chunk")])
    assert "memory item" in summary
    assert "file chunk" not in summary


def test_self_knowledge_still_uses_memory_wording():
    summary = memory_prompting.summarize_items([item(type="self")])
    assert "memory item" in summary


def test_the_type_breakdown_is_unchanged_for_memory():
    summary = memory_prompting.summarize_items(
        [item(id=1, type="note"), item(id=2, type="note"), item(id=3, type="chunk")]
    )
    assert "2 notes" in summary
    assert "1 chunk" in summary


# ======================================================
# Mixed lists
# ======================================================
def test_a_mixed_list_keeps_memory_wording():
    # A bundle containing anything other than file chunks is a memory
    # bundle, so the headline stays generic.
    summary = memory_prompting.summarize_items([item(type="note"), file_chunk(id=2)])
    assert "Recalled 2 memory items" in summary


def test_a_mixed_list_names_file_chunks_in_the_breakdown():
    summary = memory_prompting.summarize_items([item(type="note"), file_chunk(id=2)])
    assert "1 file chunk" in summary
    assert "1 note" in summary


def test_a_mixed_list_pluralizes_each_kind():
    summary = memory_prompting.summarize_items(
        [item(id=1), item(id=2), file_chunk(id=3), file_chunk(id=4)]
    )
    assert "2 file chunks" in summary
    assert "2 notes" in summary


def test_one_non_file_item_is_enough_to_keep_memory_wording():
    summary = memory_prompting.summarize_items(
        [file_chunk(id=1), file_chunk(id=2), item(id=3, type="note")]
    )
    assert "memory item" in summary
    assert "relevant file chunk" not in summary


# ======================================================
# Structure is preserved
# ======================================================
def test_file_summaries_keep_the_sentence_budget():
    for count in (1, 2, 4, 8):
        chunks = [file_chunk(id=n) for n in range(count)]
        assert 3 <= len(sentences_in(memory_prompting.summarize_items(chunks))) <= 6


def test_file_summaries_still_quote_the_chunk_text():
    summary = memory_prompting.summarize_items([file_chunk(text="the coffee machine is broken")])
    assert "the coffee machine is broken" in summary


def test_file_summaries_still_report_scores():
    summary = memory_prompting.summarize_items(
        [file_chunk(semantic_score=0.73, recency_score=0.88)]
    )
    assert "0.73" in summary
    assert "0.88" in summary


def test_file_summaries_still_flag_stale_chunks():
    assert "old" in memory_prompting.summarize_items([file_chunk(recency_score=0.05)])
    assert "old" not in memory_prompting.summarize_items([file_chunk(recency_score=0.95)])


def test_an_empty_list_is_still_an_empty_summary():
    assert memory_prompting.summarize_items([]) == ""


def test_file_summaries_are_still_extractive():
    summary = memory_prompting.summarize_items([file_chunk(text="alpha bravo charlie")])
    for token in ("alpha", "bravo", "charlie"):
        assert token in summary
    assert "delta" not in summary


# ======================================================
# Determinism
# ======================================================
def test_file_wording_is_deterministic():
    chunks = [file_chunk(id=1), file_chunk(id=2, file_id=2)]
    assert memory_prompting.summarize_items(chunks) == memory_prompting.summarize_items(chunks)


def test_file_wording_is_stable_across_repeated_calls():
    chunks = [file_chunk(id=n, file_id=n % 2) for n in range(5)]
    summaries = {memory_prompting.summarize_items(chunks) for _ in range(5)}
    assert len(summaries) == 1


def test_memory_wording_is_still_deterministic():
    items = [item(id=1), item(id=2, type="chunk")]
    assert memory_prompting.summarize_items(items) == memory_prompting.summarize_items(items)


# ======================================================
# Through the real pipeline
# ======================================================
@pytest.fixture
def indexed(db, tmp_path):
    path = tmp_path / "handbook.md"
    path.write_text(HANDBOOK, encoding="utf-8")
    return ingestion.ingest_and_index(str(path), max_chars=120)


def test_the_file_context_summary_uses_file_wording(db, indexed):
    context = memory_prompting.load_file_context("account credentials")
    assert context["items"]
    assert "file chunk" in context["summary"]
    assert "memory item" not in context["summary"]


def test_the_injected_file_section_uses_file_wording(db, indexed):
    prompt = llm_engine.build_system_prompt(
        "base", query="account credentials", use_memory=False, use_files=True
    )
    assert llm_engine.FILE_SECTION in prompt
    assert "relevant file chunk" in prompt
    assert "memory item" not in prompt


def test_the_memory_context_summary_is_unchanged(db, indexed):
    note_id = notes_store.save_note("the deployment pipeline runs nightly")
    semantic.index_note(note_id)
    context = memory_prompting.build_memory_context("deployment pipeline")
    assert context["items"]
    assert "memory item" in context["summary"]
    assert "relevant file chunk" not in context["summary"]


def test_the_llm_summary_path_is_untouched(monkeypatch):
    # summarize_items_with_llm was explicitly out of scope for the rewording.
    monkeypatch.setenv(memory_prompting.ENV_LLM_SUMMARY, "1")
    monkeypatch.setattr(memory_prompting, "summarize_items_with_llm", lambda items: "generated")
    assert memory_prompting.summarize_items([file_chunk()]) == "generated"
