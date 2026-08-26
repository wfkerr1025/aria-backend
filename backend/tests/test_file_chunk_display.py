# backend/tests/test_file_chunk_display.py
#
# How a file chunk renders inside a ### File Context block.
#
# Display only: nothing here asserts what was retrieved or how it scored,
# only how the line reads once it has been. Most tests build item dicts by
# hand so a single formatting rule can be isolated; the pipeline tests at the
# bottom confirm the real path produces the same shape.

from __future__ import annotations

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.files import file_ingestion as ingestion
from backend.llm import llm_engine, memory_prompting

SNIPPET_CHARS = memory_prompting.SNIPPET_CHARS
ELLIPSIS = "…"

HANDBOOK = """# Deployment handbook

The release pipeline runs every night at 2am and publishes to staging.

## Passwords
To reset your account credentials, use the self-service portal.
Never share your login details with anyone.
"""


def chunk(**overrides) -> dict:
    base = {
        "type": "file_chunk",
        "file_id": 7,
        "chunk_index": 3,
        "text": "To reset your account credentials, use the self-service portal.",
        "semantic_score": 0.695,
        "recency_score": 1.0,
        "start_offset": 120,
        "end_offset": 240,
    }
    base.update(overrides)
    return base


def note(**overrides) -> dict:
    base = {
        "type": "note",
        "text": "the deployment pipeline runs nightly",
        "semantic_score": 0.7,
        "keyword_score": 0.4,
        "recency_score": 0.9,
    }
    base.update(overrides)
    return base


def line_for(item: dict) -> str:
    return memory_prompting._file_item_lines({"items": [item]})[0]


def snippet_of(line: str) -> str:
    """The text part of a rendered line, without the leading bullet."""
    return line[len("  - "):].rsplit(" (", 1)[0]


# ======================================================
# Type wording
# ======================================================
def test_the_type_reads_as_english():
    assert "type=file chunk" in line_for(chunk())


def test_the_underscore_form_is_gone():
    assert "type=file_chunk" not in line_for(chunk())


def test_the_type_is_the_last_field():
    assert line_for(chunk()).rstrip().endswith("type=file chunk)")


# ======================================================
# Provenance fields
# ======================================================
def test_the_file_id_is_shown():
    assert "file=7" in line_for(chunk())


def test_the_chunk_index_is_shown():
    assert "chunk=3" in line_for(chunk())


def test_chunk_index_zero_is_shown():
    # 0 is a real chunk index, not a missing one.
    assert "chunk=0" in line_for(chunk(chunk_index=0))


def test_the_offsets_are_shown_as_a_range():
    assert "offsets=120-240" in line_for(chunk())


def test_a_zero_start_offset_is_shown():
    assert "offsets=0-90" in line_for(chunk(start_offset=0, end_offset=90))


def test_the_scores_keep_three_decimals():
    rendered = line_for(chunk(semantic_score=0.695, recency_score=1.0))
    assert "semantic=0.695" in rendered
    assert "recency=1.000" in rendered


def test_no_keyword_score_is_shown():
    # File search is semantic only; keyword=0.000 would read as "no keyword
    # match" rather than "keyword was never measured".
    assert "keyword=" not in line_for(chunk())


def test_the_field_order_is_stable():
    rendered = line_for(chunk())
    positions = [rendered.index(token) for token in
                 ["file=", "chunk=", "semantic=", "recency=", "offsets=", "type="]]
    assert positions == sorted(positions)


# ======================================================
# Missing metadata is omitted, not printed as None
# ======================================================
def test_a_missing_file_id_is_omitted():
    rendered = line_for(chunk(file_id=None))
    assert "file=" not in rendered
    assert "None" not in rendered


def test_a_missing_chunk_index_is_omitted():
    rendered = line_for(chunk(chunk_index=None))
    assert "chunk=" not in rendered
    assert "None" not in rendered


def test_missing_offsets_are_omitted():
    rendered = line_for(chunk(start_offset=None, end_offset=None))
    assert "offsets=" not in rendered
    assert "None" not in rendered


def test_a_half_missing_offset_pair_is_omitted():
    # A range with one end is not a range.
    assert "offsets=" not in line_for(chunk(end_offset=None))


def test_an_item_with_no_metadata_still_renders():
    rendered = line_for({"type": "file_chunk", "text": "bare chunk"})
    assert "bare chunk" in rendered
    assert "type=file chunk" in rendered
    assert "None" not in rendered


# ======================================================
# Snippet truncation
# ======================================================
def test_short_text_is_untouched():
    text = "a short chunk of text"
    rendered = snippet_of(line_for(chunk(text=text)))
    assert rendered == text
    assert ELLIPSIS not in rendered


def test_text_at_the_limit_is_not_truncated():
    text = "x" * SNIPPET_CHARS
    assert snippet_of(line_for(chunk(text=text))) == text


def test_long_text_is_truncated():
    rendered = snippet_of(line_for(chunk(text="word " * 200)))
    assert rendered.endswith(ELLIPSIS)


def test_the_snippet_never_exceeds_the_limit():
    for words in (40, 200, 1000):
        rendered = snippet_of(line_for(chunk(text="alpha bravo charlie delta " * words)))
        assert len(rendered) <= SNIPPET_CHARS


def test_truncation_does_not_cut_mid_word():
    text = " ".join(f"word{n}" for n in range(200))
    rendered = snippet_of(line_for(chunk(text=text)))
    body = rendered.rstrip(ELLIPSIS)
    # Every whole token that survived must be a token from the source.
    assert all(token in text.split() for token in body.split())


def test_the_ellipsis_appears_only_when_truncated():
    assert ELLIPSIS not in snippet_of(line_for(chunk(text="short")))
    assert ELLIPSIS in snippet_of(line_for(chunk(text="long " * 500)))


def test_one_giant_word_is_cut_hard():
    # A single token longer than the whole budget has no boundary to break
    # on, so it is clipped rather than dropped entirely.
    rendered = snippet_of(line_for(chunk(text="x" * 500)))
    assert len(rendered) <= SNIPPET_CHARS
    assert rendered.endswith(ELLIPSIS)


def test_whitespace_is_flattened():
    rendered = snippet_of(line_for(chunk(text="line one\n\n   line two\ttabbed")))
    assert rendered == "line one line two tabbed"


def test_the_snippet_falls_back_to_the_chunk_column():
    rendered = snippet_of(line_for({"type": "file_chunk", "chunk": "from the chunk column"}))
    assert rendered == "from the chunk column"


def test_an_empty_text_renders_as_empty():
    assert snippet_of(line_for(chunk(text=""))) == ""


# ======================================================
# Memory items are untouched
# ======================================================
def test_memory_items_keep_their_own_line_format():
    rendered = memory_prompting._item_lines({"items": [note()]})[0]
    assert "keyword=" in rendered
    assert "type=note" in rendered
    assert "file=" not in rendered
    assert "offsets=" not in rendered


def test_memory_items_keep_the_original_truncation():
    # _snippet clips at the character limit wherever it falls; only file
    # chunks were given word-boundary clipping.
    long_note = note(text="w" * 500)
    rendered = memory_prompting._item_lines({"items": [long_note]})[0]
    assert ELLIPSIS in rendered


def test_a_mixed_bundle_uses_memory_display_for_the_memory_section(db):
    note_id = notes_store.save_note("the deployment pipeline runs nightly")
    semantic.index_note(note_id)
    context = memory_prompting.build_memory_context("deployment pipeline")
    prompt = memory_prompting.build_system_prompt("base", context)
    assert "keyword=" in prompt
    assert "type=file chunk" not in prompt


def test_a_mixed_bundle_keeps_memory_summary_wording():
    summary = memory_prompting.summarize_items([note(), chunk()])
    assert "memory item" in summary
    assert "relevant file chunk" not in summary


# ======================================================
# Determinism
# ======================================================
def test_the_line_is_deterministic():
    item = chunk()
    assert line_for(item) == line_for(item)


def test_the_section_is_deterministic():
    context = {"query": "q", "summary": "s", "items": [chunk(), chunk(chunk_index=4)], "routing": {}}
    assert memory_prompting.build_file_section(context) == memory_prompting.build_file_section(
        context
    )


def test_rendering_does_not_mutate_the_item():
    item = chunk()
    before = dict(item)
    line_for(item)
    assert item == before


# ======================================================
# Through the real pipeline
# ======================================================
@pytest.fixture
def indexed(db, tmp_path):
    path = tmp_path / "handbook.md"
    path.write_text(HANDBOOK, encoding="utf-8")
    return ingestion.ingest_and_index(str(path), max_chars=120)


def test_retrieval_carries_the_offsets(db, indexed):
    hit = ingestion.search_files_semantic("credentials", min_score=0.0)[0]
    assert hit["start_offset"] is not None
    assert hit["end_offset"] is not None


def test_the_injected_section_shows_provenance(db, indexed):
    prompt = llm_engine.build_system_prompt(
        "base", query="account credentials", use_memory=False, use_files=True
    )
    file_lines = [line for line in prompt.splitlines() if "type=file chunk" in line]
    assert file_lines
    for line in file_lines:
        assert "file=" in line
        assert "chunk=" in line
        assert "offsets=" in line


def test_the_user_prompt_shows_the_same_lines(db, indexed):
    context = llm_engine.get_file_context("account credentials")
    system = llm_engine.build_system_prompt(
        "base", use_memory=False, use_files=True, file_context=context
    )
    user = llm_engine.build_user_prompt(
        "question", use_memory=False, use_files=True, file_context=context
    )
    lines = [line for line in system.splitlines() if "type=file chunk" in line]
    assert lines
    for line in lines:
        assert line in user


def test_offsets_locate_the_snippet_in_the_file(db, indexed, tmp_path):
    hit = ingestion.search_files_semantic("credentials", min_score=0.0)[0]
    source = (tmp_path / "handbook.md").read_text(encoding="utf-8")
    span = source[hit["start_offset"]:hit["end_offset"]]
    assert hit["text"] in span
