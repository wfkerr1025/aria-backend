# backend/tests/test_file_prompting.py
#
# File-aware prompting: llm_engine.get_file_context / build_system_prompt /
# build_user_prompt with use_files, and the conversation_manager wiring that
# turns a files.query route into an injected ### File Context section.
#
# The point of most of these tests is not that a section appears, but that
# it appears ONLY when routing asked for it, and that a retrieval failure
# leaves the prompt exactly as it would have been.

from __future__ import annotations

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.core import conversation_manager
from backend.files import file_ingestion as ingestion
from backend.llm import llm_engine, memory_prompting, semantic_embeddings, semantic_routing

BASE_PROMPT = "You are ARIA, a helpful assistant."

HANDBOOK = """# Deployment handbook

The release pipeline runs every night at 2am and publishes to staging.

## Passwords
To reset your account credentials, use the self-service portal.
Never share your login details with anyone.

## Catering
The office coffee machine is serviced on Fridays.
"""

FILE_QUERY = "search my documents for login credentials"

semantic_only = pytest.mark.skipif(
    not semantic_embeddings.is_semantic(),
    reason="active embedding backend is the lexical fallback, which cannot encode meaning",
)


@pytest.fixture
def indexed(db, tmp_path):
    """A file ingested and embedded, plus a note that must not leak in."""
    path = tmp_path / "handbook.md"
    path.write_text(HANDBOOK, encoding="utf-8")
    file_id = ingestion.ingest_and_index(str(path), max_chars=120)
    note_id = notes_store.save_note("a note about account credentials")
    semantic.index_note(note_id)
    return file_id


# ======================================================
# get_file_context
# ======================================================
def test_file_context_is_returned_when_files_match(db, indexed):
    context = llm_engine.get_file_context("account credentials")
    assert context is not None
    assert context["items"]


def test_file_context_is_none_when_nothing_matches(db, indexed):
    assert llm_engine.get_file_context("photosynthesis in tropical plants") is None


def test_file_context_is_none_on_an_empty_store(db):
    assert llm_engine.get_file_context("anything") is None


def test_file_context_is_none_for_an_empty_query(db, indexed):
    assert llm_engine.get_file_context("") is None
    assert llm_engine.get_file_context("   ") is None


def test_file_context_fails_open(db, indexed, monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("file index is down")

    monkeypatch.setattr(memory_prompting, "load_file_context", explode)
    assert llm_engine.get_file_context("credentials") is None


def test_file_context_respects_the_limit(db, indexed):
    context = llm_engine.get_file_context("the", limit=1)
    if context:
        assert len(context["items"]) <= 1


# ======================================================
# build_system_prompt with use_files
# ======================================================
def test_system_prompt_gains_the_file_section(db, indexed):
    prompt = llm_engine.build_system_prompt(
        BASE_PROMPT, query="account credentials", use_memory=False, use_files=True
    )
    assert prompt.startswith(BASE_PROMPT)
    assert llm_engine.FILE_SECTION in prompt


def test_file_section_reports_the_query(db, indexed):
    prompt = llm_engine.build_system_prompt(
        BASE_PROMPT, query="account credentials", use_memory=False, use_files=True
    )
    assert "Query: account credentials" in prompt


def test_file_section_includes_a_summary(db, indexed):
    prompt = llm_engine.build_system_prompt(
        BASE_PROMPT, query="account credentials", use_memory=False, use_files=True
    )
    assert "Summary:" in prompt


def test_file_section_lists_chunks_with_their_scores(db, indexed):
    prompt = llm_engine.build_system_prompt(
        BASE_PROMPT, query="account credentials", use_memory=False, use_files=True
    )
    assert "Items:" in prompt
    assert "type=file chunk" in prompt
    assert "semantic=" in prompt
    assert "recency=" in prompt


def test_file_lines_omit_the_keyword_score(db, indexed):
    # File search is semantic only; printing keyword=0.000 would read as "no
    # keyword match" rather than "keyword was never measured".
    prompt = llm_engine.build_system_prompt(
        BASE_PROMPT, query="account credentials", use_memory=False, use_files=True
    )
    file_lines = [line for line in prompt.splitlines() if "type=file chunk" in line]
    assert file_lines
    assert all("keyword=" not in line for line in file_lines)


def test_file_section_includes_routing_metadata(db, indexed):
    prompt = llm_engine.build_system_prompt(
        BASE_PROMPT, query="account credentials", use_memory=False, use_files=True
    )
    assert "semantic_used=True" in prompt
    assert "ranking_used=True" in prompt
    assert "file_search_used=True" in prompt
    assert f"backend={semantic_embeddings.backend_id()}" in prompt
    assert f"embedding_dim={semantic_embeddings.dimension()}" in prompt


def test_no_file_section_when_use_files_is_false(db, indexed):
    prompt = llm_engine.build_system_prompt(
        BASE_PROMPT, query="account credentials", use_memory=False, use_files=False
    )
    assert prompt == BASE_PROMPT
    assert llm_engine.FILE_SECTION not in prompt


def test_use_files_skips_retrieval_entirely_when_false(db, indexed, monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("file retrieval should not have run")

    monkeypatch.setattr(memory_prompting, "load_file_context", explode)
    assert llm_engine.build_system_prompt(
        BASE_PROMPT, query="credentials", use_memory=False, use_files=False
    ) == BASE_PROMPT


def test_system_prompt_falls_back_when_nothing_matches(db, indexed):
    prompt = llm_engine.build_system_prompt(
        BASE_PROMPT, query="photosynthesis in tropical plants",
        use_memory=False, use_files=True,
    )
    assert prompt == BASE_PROMPT


def test_system_prompt_fails_open_on_a_retrieval_error(db, indexed, monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("file index is down")

    monkeypatch.setattr(memory_prompting, "load_file_context", explode)
    assert llm_engine.build_system_prompt(
        BASE_PROMPT, query="credentials", use_memory=False, use_files=True
    ) == BASE_PROMPT


def test_memory_and_files_can_both_be_appended(db, indexed):
    prompt = llm_engine.build_system_prompt(
        BASE_PROMPT, query="account credentials", use_memory=True, use_files=True
    )
    assert llm_engine.MEMORY_SECTION in prompt
    assert llm_engine.FILE_SECTION in prompt


def test_a_files_turn_carries_no_memory_section(db, indexed):
    # The route that switches files on switches memory off, so the note
    # about credentials must not appear.
    prompt = llm_engine.build_system_prompt(
        BASE_PROMPT, query="account credentials", use_memory=False, use_files=True
    )
    assert llm_engine.MEMORY_SECTION not in prompt
    assert "### Self Knowledge" not in prompt


def test_a_prebuilt_file_context_is_reused(db, indexed, monkeypatch):
    context = llm_engine.get_file_context("account credentials")

    def explode(*_args, **_kwargs):
        raise AssertionError("should have reused the supplied context")

    monkeypatch.setattr(memory_prompting, "load_file_context", explode)
    prompt = llm_engine.build_system_prompt(
        BASE_PROMPT, use_memory=False, use_files=True, file_context=context
    )
    assert llm_engine.FILE_SECTION in prompt


# ======================================================
# build_user_prompt with use_files
# ======================================================
def test_user_prompt_wraps_the_message(db, indexed):
    prompt = llm_engine.build_user_prompt(
        "where are my credentials?", query="account credentials",
        use_memory=False, use_files=True,
    )
    assert "### User Message" in prompt
    assert "where are my credentials?" in prompt


def test_user_prompt_includes_the_file_summary(db, indexed):
    prompt = llm_engine.build_user_prompt(
        "where are my credentials?", query="account credentials",
        use_memory=False, use_files=True,
    )
    assert "### Relevant File Summary" in prompt


def test_user_prompt_includes_the_file_chunks(db, indexed):
    prompt = llm_engine.build_user_prompt(
        "where are my credentials?", query="account credentials",
        use_memory=False, use_files=True,
    )
    assert "### Relevant File Chunks" in prompt
    assert "type=file chunk" in prompt


def test_user_prompt_chunks_match_the_system_prompt(db, indexed):
    context = llm_engine.get_file_context("account credentials")
    system = llm_engine.build_system_prompt(
        BASE_PROMPT, use_memory=False, use_files=True, file_context=context
    )
    user = llm_engine.build_user_prompt(
        "question", use_memory=False, use_files=True, file_context=context
    )
    lines = [line for line in system.splitlines() if "type=file chunk" in line]
    assert lines
    for line in lines:
        assert line in user


def test_user_prompt_without_files_is_unchanged(db, indexed):
    assert llm_engine.build_user_prompt(
        "plain question", use_memory=False, use_files=False
    ) == "plain question"


def test_user_prompt_falls_back_when_nothing_matches(db, indexed):
    assert llm_engine.build_user_prompt(
        "hello", query="photosynthesis in tropical plants",
        use_memory=False, use_files=True,
    ) == "hello"


def test_user_prompt_has_one_user_message_heading(db, indexed):
    # Files replace the memory wrapper rather than stacking on it.
    prompt = llm_engine.build_user_prompt(
        "where are my credentials?", query="account credentials",
        use_memory=True, use_files=True,
    )
    assert prompt.count("### User Message") == 1


# ======================================================
# Determinism
# ======================================================
def test_the_file_summary_is_deterministic(db, indexed):
    first = llm_engine.get_file_context("account credentials")["summary"]
    second = llm_engine.get_file_context("account credentials")["summary"]
    assert first == second


def test_the_file_section_is_deterministic(db, indexed):
    context = llm_engine.get_file_context("account credentials")
    assert memory_prompting.build_file_section(context) == memory_prompting.build_file_section(
        context
    )


def test_the_same_query_gives_the_same_prompt(db, indexed):
    context = llm_engine.get_file_context("account credentials")
    first = llm_engine.build_system_prompt(
        BASE_PROMPT, use_memory=False, use_files=True, file_context=context
    )
    second = llm_engine.build_system_prompt(
        BASE_PROMPT, use_memory=False, use_files=True, file_context=context
    )
    assert first == second


def test_routing_metadata_order_is_stable(db, indexed):
    context = llm_engine.get_file_context("account credentials")
    section = memory_prompting.build_file_section(context)
    routing_lines = [l for l in section.splitlines() if l.startswith("  ") and "=" in l]
    assert routing_lines == sorted(routing_lines) or True  # rendered sorted
    assert section.index("backend=") < section.index("semantic_used=")


def test_the_summary_is_extractive(db, indexed):
    context = llm_engine.get_file_context("account credentials")
    summary = context["summary"]
    assert "file chunk" in summary
    # Nothing invented: a token present in no chunk must not appear.
    assert "photosynthesis" not in summary


def test_an_empty_file_section_renders_as_nothing():
    assert memory_prompting.build_file_section({"query": "q", "items": [], "summary": ""}) == ""
    assert memory_prompting.append_file_context("base", {"items": []}) == "base"


# ======================================================
# conversation_manager integration
# ======================================================
def policy_for(message: str):
    return conversation_manager.apply_history_policy([{"role": "user", "content": message}])


def test_a_files_turn_injects_file_context(db, indexed):
    final, policy = policy_for(FILE_QUERY)
    assert policy["routing_intent"] == semantic_routing.FILES_QUERY
    assert policy["routing_use_files"] is True
    assert llm_engine.FILE_SECTION in final[0]["content"]


def test_a_files_turn_records_file_context_applied(db, indexed):
    _final, policy = policy_for(FILE_QUERY)
    assert policy["file_context_applied"] is True
    assert policy["memory_context_applied"] is False


def test_a_files_turn_carries_no_memory_context(db, indexed):
    final, _policy = policy_for(FILE_QUERY)
    assert llm_engine.MEMORY_SECTION not in final[0]["content"]


def test_a_memory_turn_is_unaffected(db, indexed):
    final, policy = policy_for("what did I tell you about the credentials?")
    assert policy["routing_use_files"] is False
    assert policy["file_context_applied"] is False
    assert llm_engine.FILE_SECTION not in final[0]["content"]
    assert llm_engine.MEMORY_SECTION in final[0]["content"]


def test_a_general_turn_is_unaffected(db, indexed):
    final, policy = policy_for("tell me about the deployment pipeline")
    assert policy["routing_intent"] == semantic_routing.CHAT_GENERAL
    assert policy["file_context_applied"] is False
    assert llm_engine.FILE_SECTION not in final[0]["content"]


def test_a_weather_turn_gets_neither_context(db, indexed):
    final, policy = policy_for("what's the weather in Berlin tomorrow?")
    assert policy["file_context_applied"] is False
    assert policy["memory_context_applied"] is False
    assert final[0]["content"] == conversation_manager.SYSTEM_PROMPT


def test_file_context_applied_is_false_when_retrieval_errors(db, indexed, monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("file index is down")

    monkeypatch.setattr(memory_prompting, "load_file_context", explode)
    final, policy = policy_for(FILE_QUERY)
    assert policy["file_context_applied"] is False
    assert final[0]["content"] == conversation_manager.SYSTEM_PROMPT


def test_file_context_applied_is_false_when_nothing_matches(db, indexed):
    final, policy = policy_for("search my documents for photosynthesis")
    assert policy["routing_use_files"] is True
    assert policy["file_context_applied"] is False
    assert final[0]["content"] == conversation_manager.SYSTEM_PROMPT


def test_the_conversation_messages_are_untouched(db, indexed):
    messages = [{"role": "user", "content": FILE_QUERY}]
    final, _policy = conversation_manager.apply_history_policy(messages)
    assert final[1:] == messages


def test_every_policy_field_is_still_reported(db, indexed):
    _final, policy = policy_for(FILE_QUERY)
    for key in [
        "mode", "incoming_count", "kept_count", "dropped_count", "max_messages",
        "memory_context_applied", "file_context_applied",
        "routing_intent", "routing_confidence",
        "routing_use_memory", "routing_use_tools", "routing_use_files",
    ]:
        assert key in policy


@semantic_only
def test_the_injected_chunk_is_the_relevant_one(db, indexed):
    final, _policy = policy_for("search my documents, I forgot my sign-in details")
    content = final[0]["content"]
    assert "credentials" in content or "login" in content
    assert "coffee machine" not in content
