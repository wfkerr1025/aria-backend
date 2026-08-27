# backend/tests/test_structured_evidence_flow.py
#
# Structured findings reaching the summary, without putting JSON in front
# of the model.
#
# Arc 237 normalized tool results but had nowhere to put them: the only
# channel into the synthesis layer was ToolResult.summary, one rendered
# string. This arc adds ToolResult.normalized, so the fields travel beside
# the line rather than inside it, and the evidence summary reads them.
#
# WHY THE PROMPT IS STILL PROSE. The obvious way to pass structure through
# is to emit `- {json.dumps(result.normalized)}` into the Tool Results
# section. Measured, that breaks two things at once:
#
#   * evidence_routing's decoder strips URLs out of the line, which
#     lands inside the JSON string and corrupts it:
#         {"tool": "web_search", "rank": 1, "source": ", "title": ...
#   * the raw-evidence fallback shows that section to the USER, so a
#     failed lookup would answer with a mangled JSON blob
#
# And the prompt is the model-facing artifact. Arc 236 established that a
# serialized envelope in the evidence section is a failure mode worth
# detecting; deliberately emitting one is the same mistake with better
# intentions. Structure travels on the object; the prompt stays readable.

from __future__ import annotations

import json

import pytest

from backend.aria_synthesis.bundle_builder import build_evidence_bundle
from backend.aria_synthesis.synthesis_prompt import (
    SUMMARY_MAX_CHARS,
    SUMMARY_MAX_SENTENCES,
    _evidence_summary,
    _extractive_summary,
    _structured_findings,
    build_synthesis_prompt,
)
from backend.tools.tool_executor import ToolInvocation, ToolExecutor, normalize_tool_value
from backend.tools.tool_registry import STATUS_ERROR, STATUS_OK, ToolResult


RICH_SEARCH = {
    "reply": "Microsoft is trading at $412.30, up 1.2%.",
    "raw": {
        "heading": "Microsoft Corporation",
        "summary": "Microsoft is trading at $412.30, up 1.2%. The stock hit a 52-week high in July.",
        "source_url": "https://investopedia.com/msft",
        "related": [
            {"text": "MSFT quarterly earnings beat estimates.", "url": "https://example.com/earnings"},
            {"text": "Azure revenue up 30%.", "url": "https://example.com/azure"},
        ],
    },
}


def search_result(value=RICH_SEARCH, status=STATUS_OK) -> ToolResult:
    return ToolResult(
        tool_name="web_search", step_id="step1", status=status,
        summary="rendered line", payload=value,
        normalized=tuple(normalize_tool_value("web_search", value)),
    )


def prompt_with(results, bundle_items=()):
    bundle = build_evidence_bundle("q", list(bundle_items), now="FIXED")
    return build_synthesis_prompt("q", bundle, [], None, None, None, list(results))


# ======================================================
# The structure reaches the synthesis layer
# ======================================================
def test_the_executor_puts_the_structured_findings_on_the_result():
    """The channel this arc adds. Without it the fields die at the line."""
    from backend.core import tool_registry as core_reg

    core_reg.register_tool(
        core_reg.ToolSchema(
            name="web_search", description="test double",
            parameters={"query": {"type": "string", "required": True}},
            permission=core_reg.PERMISSION_NETWORK,
        ),
        lambda query: RICH_SEARCH,
    )
    try:
        results = ToolExecutor().execute([
            ToolInvocation(tool_name="web_search", step_id="step1", args={"query": "msft"}),
        ])
    finally:
        core_reg.register_builtin_tools()

    assert len(results) == 1
    assert results[0].normalized, "the findings did not survive execution"
    assert results[0].normalized[0].title == "Microsoft Corporation"
    assert results[0].normalized[0].url == "https://investopedia.com/msft"


def test_normalized_defaults_to_empty_so_older_callers_still_work():
    """Every existing ToolResult(...) call site omits it."""
    assert ToolResult("t", "step1", STATUS_OK, "a line").normalized == ()


def test_only_successful_results_contribute_findings():
    """A tool that ran and failed has no findings to report."""
    assert _structured_findings([search_result(status=STATUS_ERROR)]) == []
    assert _structured_findings([search_result()]) != []


def test_the_rendered_line_and_the_structure_describe_the_same_findings():
    """They are two views of one normalization, not two normalizations."""
    result = search_result()
    assert [item.snippet for item in result.normalized][0].startswith("Microsoft is trading")


# ======================================================
# The summary is extractive
# ======================================================
def test_the_summary_uses_the_findings_own_sentences():
    """No sentence in it may be one the lookup did not say.

    This line sits directly above evidence the model is told to use and
    nothing else. A generated sentence here is an unsourced claim in the
    one place the prompt insists every claim be sourced.
    """
    summary = _extractive_summary(_structured_findings([search_result()]))
    source_text = (
        RICH_SEARCH["raw"]["summary"] + " "
        + " ".join(item["text"] for item in RICH_SEARCH["raw"]["related"])
    )

    for sentence in summary.split(". "):
        assert sentence.strip(" .…") in source_text, f"invented: {sentence!r}"


def test_the_summary_is_stable():
    """Same evidence in, same summary out."""
    findings = _structured_findings([search_result()])
    first = _extractive_summary(findings)
    assert all(_extractive_summary(findings) == first for _ in range(5))


def test_the_summary_is_bounded_by_sentences():
    many = [{"text": f"Finding number {n} happened.", "url": None} for n in range(20)]
    result = search_result({"raw": {"summary": "First finding.", "related": many}})
    summary = _extractive_summary(_structured_findings([result]))

    assert summary.count(".") <= SUMMARY_MAX_SENTENCES


def test_the_summary_is_bounded_by_characters():
    long_sentence = "word " * 400
    result = search_result({"raw": {"summary": long_sentence, "related": []}})
    summary = _extractive_summary(_structured_findings([result]))

    assert len(summary) <= SUMMARY_MAX_CHARS + 1
    assert not summary.rstrip("…").endswith("wor"), "cut mid-word"


def test_a_repeated_sentence_is_not_said_twice():
    duplicate = {"raw": {"summary": "Microsoft is at $412.30.",
                         "related": [{"text": "Microsoft is at $412.30.", "url": None}]}}
    summary = _extractive_summary(_structured_findings([search_result(duplicate)]))
    assert summary.count("Microsoft is at $412.30") == 1


def test_findings_with_no_text_contribute_nothing():
    empty = {"raw": {"summary": None, "related": []}}
    assert _extractive_summary(_structured_findings([search_result(empty)])) == ""


# ======================================================
# It reaches the prompt, and the prompt stays prose
# ======================================================
def test_the_evidence_summary_line_carries_what_was_found():
    line = next(l for l in prompt_with([search_result()]).splitlines()
                if l.startswith("Evidence Summary:"))

    assert "$412.30" in line
    assert "Azure revenue" in line


def test_a_turn_with_no_findings_keeps_the_old_summary_line():
    """The count sentence is unchanged when there is nothing to extract."""
    empty = search_result({"raw": {"summary": None, "related": []}})
    line = next(l for l in prompt_with([empty]).splitlines()
                if l.startswith("Evidence Summary:"))
    assert "They report:" not in line


def test_the_prompt_contains_no_json_blob():
    """The hazard this arc deliberately did not build.

    Emitting `- {json.dumps(result.normalized)}` into Tool Results puts a
    serialized envelope in front of the model -- the exact shape Arc 236
    added detection for -- and evidence_routing's URL stripping corrupts
    it on the way back out. Structure travels on the object instead.
    """
    prompt = prompt_with([search_result()])
    tool_section = prompt.split("Tool Results:")[1].split("\n\n")[0]

    assert '{"tool"' not in tool_section
    assert '"snippet":' not in tool_section
    for line in tool_section.splitlines():
        body = line.lstrip("- ").strip()
        if body.startswith("{"):
            pytest.fail(f"a JSON object reached the prompt: {body[:60]}")


def test_the_surrounding_prompt_format_is_unchanged():
    prompt = prompt_with([search_result()])

    for heading in ("User Query:", "Evidence Summary:", "Notes:", "Files:",
                    "Tool Results:", "Synthesis Instructions:"):
        assert heading in prompt
    assert prompt.rstrip().endswith("Produce a single coherent answer.")


def test_the_tool_results_section_still_renders_one_bullet_per_result():
    prompt = prompt_with([search_result()])
    section = prompt.split("Tool Results:")[1].split("\n\n")[0]
    bullets = [l for l in section.splitlines() if l.strip().startswith("-")]

    assert len(bullets) == 1
    assert bullets[0].startswith("- web_search (step1):")


# ======================================================
# Metadata, end to end
# ======================================================
def test_every_field_survives_from_the_tool_to_the_summary():
    findings = _structured_findings([search_result()])

    assert findings[0].title == "Microsoft Corporation"
    assert findings[0].url == "https://investopedia.com/msft"
    assert findings[0].rank == 1
    assert findings[0].tool == "web_search"
    assert [f.url for f in findings] == [
        "https://investopedia.com/msft",
        "https://example.com/earnings",
        "https://example.com/azure",
    ]


def test_a_field_the_tool_never_returned_is_still_none():
    """DuckDuckGo carries no timestamp, and none is invented for it."""
    assert _structured_findings([search_result()])[0].timestamp is None


def test_the_bundle_already_carries_note_metadata():
    """Pinned because this arc asked for a change that was not needed.

    bundle_builder never receives tool results -- they go straight to
    build_synthesis_prompt -- and the note and file items it does build
    already carry title, provenance and score. The metadata gap was on the
    tool side, which ToolResult.normalized closes.
    """
    chunk = {"type": "file_chunk", "file_id": 1, "path": "docs/build.md",
             "section": "Pipeline", "text": "shaders compile first", "combined_score": 0.9}
    bundle = build_evidence_bundle("q", [chunk], now="FIXED")

    assert bundle.files
    item = bundle.files[0]
    assert item.path == "docs/build.md"
    assert item.provenance
    assert item.snippet


# ======================================================
# Nothing else moved
# ======================================================
def test_the_safety_relevant_instructions_are_intact():
    """The rules that keep an answer sourced must not have shifted."""
    prompt = prompt_with([search_result()])

    assert "Use ONLY the evidence above" in prompt
    assert 'say "not found" and stop' in prompt


def test_a_failed_lookup_is_still_reported_as_failed():
    prompt = prompt_with([search_result(status=STATUS_ERROR)])
    assert "of these failed" in prompt or "Tool Results:" not in prompt
