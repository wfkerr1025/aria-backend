# backend/tests/test_tool_result_structuring.py
#
# Keeping a tool's structure long enough to be useful.
#
# The tool layer renders a result to one line, and whatever the line does
# not carry is gone. For web_search that was almost everything:
# _search_handler returns {"raw": <the rich result>, "reply": <one string>}
# where the rich result holds the heading, the source URL and every
# related topic -- and summarize() read only `reply`.
#
# So a search that found
#
#     Microsoft Corporation
#     Microsoft is trading at $412.30, up 1.2%
#     https://investopedia.com/msft
#     ... plus four related results
#
# reached the model as one untitled, unattributed sentence, and the other
# four findings were never mentioned.
#
# Normalizing first and rendering second keeps them. What genuinely is not
# there stays None: DuckDuckGo's instant-answer API carries no timestamp,
# so a web_search result has none -- a fabricated date on a stale price is
# worse than no date at all.

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.core import evidence_routing as er
from backend.tools import tool_executor as te
from backend.tools.tool_executor import (
    NormalizedToolResult,
    normalize_tool_value,
    render_normalized,
    summarize,
)


# The real shape run_search_tool produces, as captured from the live tool.
RICH_SEARCH = {
    "reply": "Microsoft is trading at $412.30, up 1.2%.",
    "raw": {
        "status": "ok",
        "tool": "web_search",
        "query": "msft",
        "heading": "Microsoft Corporation",
        "summary": "Microsoft is trading at $412.30, up 1.2%.",
        "source_url": "https://investopedia.com/msft",
        "related": [
            {"text": "MSFT quarterly earnings beat estimates", "url": "https://example.com/earnings"},
            {"text": "Azure revenue up 30%", "url": "https://example.com/azure"},
        ],
    },
}


# ======================================================
# 1. Normalization at the tool layer
# ======================================================
def test_the_structured_fields_survive_normalization():
    primary = normalize_tool_value("web_search", RICH_SEARCH)[0]

    assert primary.tool == "web_search"
    assert primary.title == "Microsoft Corporation"
    assert primary.snippet == "Microsoft is trading at $412.30, up 1.2%."
    assert primary.url == "https://investopedia.com/msft"
    assert primary.rank == 1


def test_the_raw_result_is_kept_alongside_the_normalized_one():
    """Normalization must not be lossy: what it dropped is still reachable."""
    primary = normalize_tool_value("web_search", RICH_SEARCH)[0]
    assert primary.raw is RICH_SEARCH["raw"]


def test_a_field_the_tool_never_returned_stays_none():
    """DuckDuckGo carries no timestamp. Inventing one would date a price."""
    primary = normalize_tool_value("web_search", RICH_SEARCH)[0]
    assert primary.timestamp is None


def test_a_url_without_a_usable_scheme_is_dropped():
    value = {"raw": {"summary": "a finding", "source_url": "javascript:alert(1)", "related": []}}
    assert normalize_tool_value("web_search", value)[0].url is None


def test_a_long_snippet_is_cut_at_a_word_boundary():
    value = {"raw": {"summary": "word " * 300, "related": []}}
    snippet = normalize_tool_value("web_search", value)[0].snippet

    assert len(snippet) <= te.SNIPPET_CHARS + 1
    assert not snippet.rstrip("…").endswith("wor")


def test_a_result_with_nothing_in_it_normalizes_to_nothing():
    """An empty list means the tool ran and found nothing.

    Distinct from failing, and distinct again from succeeding -- the
    caller needs all three to be tellable apart.
    """
    assert normalize_tool_value("web_search", {"raw": {"summary": None, "related": []}}) == []


def test_an_unstructured_result_still_yields_its_reply():
    """A tool whose raw payload did not survive is not silently dropped."""
    items = normalize_tool_value("web_search", {"reply": "a plain finding", "raw": None})
    assert len(items) == 1 and items[0].snippet == "a plain finding"


@pytest.mark.parametrize("tool,value,expected_snippet", [
    ("read_file", {"path": "docs/build.md", "text": "the pipeline compiles shaders"},
     "the pipeline compiles shaders"),
    ("weather", {"location": "Tokyo", "temperature": "12C", "conditions": "Clear",
                 "provider": "open-meteo"}, "12C, Clear"),
])
def test_the_other_tools_normalize_too(tool, value, expected_snippet):
    items = normalize_tool_value(tool, value)
    assert len(items) == 1
    assert items[0].snippet == expected_snippet
    assert items[0].tool == tool


def test_search_notes_yields_one_item_per_note():
    value = {"results": [
        {"id": "n1", "title": "Build", "text": "shaders compile first"},
        {"id": "n2", "title": "Deploy", "text": "then it packages"},
        {"id": "n3", "title": "Empty", "text": ""},
    ]}
    items = normalize_tool_value("search_notes", value)

    assert [item.title for item in items] == ["Build", "Deploy"]
    assert [item.rank for item in items] == [1, 2]


def test_a_tool_with_no_normalizer_is_not_guessed_at():
    assert normalize_tool_value("run_tests", {"passed": True}) == []


def test_a_normalizer_that_raises_does_not_fail_the_turn(monkeypatch):
    """The rendered summary is the fallback, not an exception."""
    def explode(value):
        raise RuntimeError("bad shape")

    monkeypatch.setitem(te._NORMALIZERS, "web_search", explode)
    assert normalize_tool_value("web_search", RICH_SEARCH) == []
    assert summarize("web_search", RICH_SEARCH)   # falls back to `reply`


# ======================================================
# 2. Multi-item results
# ======================================================
def test_every_related_topic_becomes_its_own_finding():
    items = normalize_tool_value("web_search", RICH_SEARCH)
    assert len(items) == 3


def test_ordering_and_rank_follow_the_tool():
    """The instant answer is rank 1; related topics follow in order."""
    items = normalize_tool_value("web_search", RICH_SEARCH)

    assert [item.rank for item in items] == [1, 2, 3]
    assert items[0].title == "Microsoft Corporation"
    assert items[1].snippet.startswith("MSFT quarterly")
    assert items[2].snippet.startswith("Azure revenue")


def test_each_item_keeps_its_own_provenance():
    """One line used to mean one URL. Three findings have three."""
    urls = [item.url for item in normalize_tool_value("web_search", RICH_SEARCH)]
    assert urls == [
        "https://investopedia.com/msft",
        "https://example.com/earnings",
        "https://example.com/azure",
    ]


def test_a_blank_related_topic_is_skipped_not_counted():
    value = {"raw": {"summary": "a finding", "related": [
        {"text": "", "url": "https://example.com/blank"},
        {"text": "a real one", "url": "https://example.com/real"},
    ]}}
    items = normalize_tool_value("web_search", value)

    assert len(items) == 2
    assert items[1].snippet == "a real one"


def test_related_topics_are_capped():
    """A hundred footnotes is not evidence, it is a denial of attention."""
    value = {"raw": {"summary": "a finding",
                     "related": [{"text": f"topic {n}", "url": None} for n in range(50)]}}
    assert len(normalize_tool_value("web_search", value)) <= te.MAX_RELATED_ITEMS + 1


# ======================================================
# 5. Flattening happens after normalization
# ======================================================
def test_the_rendered_line_carries_title_url_and_the_extras():
    line = summarize("web_search", RICH_SEARCH)

    assert "Microsoft Corporation" in line
    assert "https://investopedia.com/msft" in line
    assert "MSFT quarterly earnings" in line
    assert "Azure revenue" in line


def test_flattening_never_produces_an_empty_line_for_a_real_finding():
    assert summarize("web_search", RICH_SEARCH).strip()


def test_a_result_with_nothing_in_it_is_not_flattened_into_success():
    """It renders as a placeholder that the evidence layer treats as unusable."""
    line = summarize("web_search", {"reply": "", "raw": {"summary": None, "related": []}})
    assert er.normalize_tool_line(f"web_search (step1): {line}").usable is False


def test_a_malformed_result_is_not_flattened_into_success():
    line = summarize("web_search", {"unexpected": True})
    assert er.normalize_tool_line(f"web_search (step1): {line}").usable is False


# ======================================================
# The round trip -- the coupling that makes this work
# ======================================================
# The tool layer encodes structure into one string because the prompt
# builder emits one bullet per tool result and is not ours to change. The
# evidence layer decodes it. They are a pair, and a change to either
# delimiter breaks the other silently -- so the round trip is asserted
# here rather than each half in isolation.
def test_structure_survives_the_round_trip_into_the_evidence_layer():
    line = "web_search (step1): " + render_normalized(normalize_tool_value("web_search", RICH_SEARCH))
    items = er.normalize_evidence([line])

    assert len(items) == 3, "the related topics did not survive"
    assert items[0].title == "Microsoft Corporation"
    assert items[0].url == "https://investopedia.com/msft"
    assert items[1].url == "https://example.com/earnings"
    assert all(item.usable for item in items)


def test_the_direct_answer_is_not_buried_beneath_its_own_footnotes():
    """Ordering is by rank before title, and this is why.

    The related topics carry no title, and "" sorts ahead of every real
    one -- so sorting on title first put the footnotes above the answer.
    """
    line = "web_search (step1): " + render_normalized(normalize_tool_value("web_search", RICH_SEARCH))
    items = er.normalize_evidence([line])

    assert items[0].title == "Microsoft Corporation"
    assert [item.rank for item in items] == [1, 2, 3]


def test_a_timestamp_survives_the_round_trip():
    item = NormalizedToolResult(
        tool="search_notes", source="n1", title="Build", snippet="shaders compile first",
        url=None, timestamp=datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc), rank=1,
    )
    line = "search_notes (step1): " + render_normalized([item])
    recovered = er.normalize_evidence([line])[0]

    assert recovered.timestamp is not None
    assert recovered.timestamp.year == 2026
    assert recovered.title == "Build"


def test_a_plain_line_still_reads_correctly():
    """Nothing here may break the shape that was there before."""
    items = er.normalize_evidence(["web_search (step1): Microsoft is trading at $412.30"])

    assert len(items) == 1
    assert items[0].snippet == "Microsoft is trading at $412.30"
    assert items[0].title is None


# ======================================================
# 6. tool_result_trace
# ======================================================
def _turn(debug):
    from backend.core import tool_registry as core_reg
    from backend.core.turn_orchestrator import orchestrate_turn
    from backend.core.turn_types import SessionState, TurnRequest

    core_reg.register_tool(
        core_reg.ToolSchema(
            name="web_search", description="test double",
            parameters={"query": {"type": "string", "required": True}},
            permission=core_reg.PERMISSION_NETWORK,
        ),
        lambda query: RICH_SEARCH,
    )
    try:
        query = "What is the stock price of Microsoft?"
        return orchestrate_turn(
            TurnRequest(
                messages=[{"role": "user", "content": query}],
                latest_user_text=query, conversation_id="c1",
                session=SessionState(mode="local", explicit_model_override="mistral-7b-q4km"),
                skip_safety_check=True, debug_trace=debug,
            ),
            default_local_model=lambda: "mistral-7b-q4km",
        )
    finally:
        core_reg.register_builtin_tools()


def test_the_trace_shows_all_four_stages_in_debug_mode():
    trace = _turn(debug=True).tool_result_trace

    assert set(trace) == {"raw", "normalized", "flattened", "bundle_ready"}
    assert "web_search" in trace["raw"]
    assert len(trace["normalized"]) >= 3, "the related topics are missing from the trace"
    assert trace["bundle_ready"]


def test_the_trace_is_absent_by_default():
    assert _turn(debug=False).tool_result_trace == {}


def test_the_whole_search_reaches_the_model():
    """The end the arc is for: all three findings, with their sources."""
    prompt = _turn(debug=False).inference_request.messages[-1].content

    assert "Microsoft Corporation" in prompt
    assert "investopedia.com/msft" in prompt
    assert "MSFT quarterly earnings" in prompt
    assert "Azure revenue" in prompt
