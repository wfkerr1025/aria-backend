# backend/tests/test_evidence_bundling.py
#
# What a lookup returned, before it is put in front of a model.
#
# The defect this arc fixes, measured across the seven ways web_search can
# come back. Five of them produced a turn that reported success and handed
# the model nothing:
#
#     tool outcome        evidence in the prompt        before
#     normal result       "MSFT $412.30"                ok
#     empty list          ""                            reported as evidence
#     None                ""                            reported as evidence
#     malformed shape     "search returned no summary"  reported as evidence
#     empty reply string  "search returned no summary"  reported as evidence
#     serialized envelope {"Abstract":"","Results":[]}  reported as evidence
#     tool raised         (none)                        correctly refused
#
# Each of those prompts still carried "Use ONLY the evidence above", so the
# model was told to answer from an empty string. That is not a model that
# hallucinates; it is a prompt that asks it to.
#
# The rule now is that evidence is what a lookup actually said. A blank
# line is not evidence, a placeholder is not evidence, and an empty API
# envelope is not evidence however well-formed its JSON is.

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.core import evidence_routing as er
from backend.core.evidence_routing import (
    NO_USABLE_TEXT,
    SNIPPET_MAX_CHARS,
    TITLE_MAX_CHARS,
    NormalizedEvidenceItem,
    format_evidence_block,
    has_usable_evidence,
    normalize_evidence,
    normalize_tool_line,
)
from backend.core.turn_orchestrator import EVIDENCE_MISSING_ANSWER, orchestrate_turn
from backend.core.turn_types import KIND_INFERENCE, KIND_TEXT, SessionState, TurnRequest


QUERY = "What is the stock price of Microsoft?"
MODEL = "mistral-7b-q4km"   # allowlisted, so capability routing stays out of the way


def tool_returning(value):
    """Register a web_search double; `value` may be a value or an exception."""
    from backend.core import tool_registry as core_reg

    def run(query):
        if isinstance(value, Exception):
            raise value
        return value

    core_reg.register_tool(
        core_reg.ToolSchema(
            name="web_search", description="test double",
            parameters={"query": {"type": "string", "required": True}},
            permission=core_reg.PERMISSION_NETWORK,
        ),
        run,
    )


@pytest.fixture(autouse=True)
def restore_tools():
    yield
    from backend.core import tool_registry as core_reg
    core_reg.register_builtin_tools()


def run_turn(debug=False):
    return orchestrate_turn(
        TurnRequest(
            messages=[{"role": "user", "content": QUERY}],
            latest_user_text=QUERY,
            conversation_id="c1",
            session=SessionState(mode="local", explicit_model_override=MODEL),
            skip_safety_check=True,
            debug_trace=debug,
        ),
        default_local_model=lambda: MODEL,
    )


# ======================================================
# 1. Normalization
# ======================================================
def test_a_rendered_line_becomes_the_unified_shape():
    item = normalize_tool_line("web_search (step3): MSFT is at $412.30 https://example.com/msft")

    assert item.tool == "web_search"
    assert item.rank == 3
    assert item.snippet == "MSFT is at $412.30"
    assert item.url == "https://example.com/msft"
    assert item.usable is True

    # source is provenance -- where the finding came from -- and falls
    # back to the tool name only when nothing better is known. It was
    # always the tool name while URLs were being dropped upstream; now
    # that they survive, repeating the tool name here would waste the
    # field, since `tool` already holds it.
    assert item.source == "https://example.com/msft"
    assert normalize_tool_line("web_search (step1): no link here").source == "web_search"


def test_every_field_is_always_present():
    """Total by construction, so no consumer has to guess at a missing key."""
    item = normalize_tool_line("web_search (step1): something")

    for field in ("source", "title", "snippet", "url", "timestamp", "raw"):
        assert hasattr(item, field)
    assert set(item.as_dict()) == {
        "tool", "rank", "source", "title", "snippet", "url", "timestamp",
    }


def test_absent_fields_are_none_not_invented():
    """A rendered line carries no title and no timestamp.

    Deriving a title from the snippet would produce something that reads
    exactly like a real one -- the same class of error as inventing an
    answer.
    """
    item = normalize_tool_line("web_search (step1): a finding with no title")
    assert item.title is None
    assert item.timestamp is None


def test_a_snippet_is_trimmed_at_a_word_boundary():
    long_text = "word " * 200
    item = normalize_tool_line(f"web_search (step1): {long_text}")

    assert len(item.snippet) <= SNIPPET_MAX_CHARS + 1   # +1 for the ellipsis
    assert "wor…" not in item.snippet, "cut mid-word"


def test_a_title_is_trimmed_at_a_word_boundary():
    item = NormalizedEvidenceItem(source="t", title="word " * 100, snippet="x")
    block = format_evidence_block([item])
    title_line = block.splitlines()[0]

    assert len(title_line) <= TITLE_MAX_CHARS + 4
    assert title_line.endswith("…")


@pytest.mark.parametrize("raw,expected", [
    ("https://example.com/x", "https://example.com/x"),
    ("http://example.com", "http://example.com"),
    ("ftp://example.com/x", None),
    ("example.com/x", None),
    ("javascript:alert(1)", None),
])
def test_only_a_real_scheme_counts_as_a_url(raw, expected):
    item = normalize_tool_line(f"web_search (step1): see {raw} for more")
    assert item.url == expected


def test_a_timestamp_is_parsed_when_one_is_present():
    stamp = er._parse_timestamp("2026-08-26T12:00:00Z")
    assert stamp is not None and stamp.year == 2026
    assert er._parse_timestamp("not a date") is None
    assert er._parse_timestamp(None) is None


def test_a_url_is_not_printed_twice():
    """It gets its own line, so leaving it in the snippet duplicates it."""
    item = normalize_tool_line("web_search (step1): MSFT at $412.30 https://example.com/msft")
    assert "https://" not in item.snippet
    assert format_evidence_block([item]).count("https://example.com/msft") == 1


# ------------------------------------------------------
# What is not evidence
# ------------------------------------------------------
@pytest.mark.parametrize("body", ["", "   ", "search returned no summary", "none", "N/A"])
def test_a_lookup_that_found_nothing_is_not_usable(body):
    assert normalize_tool_line(f"web_search (step1): {body}").usable is False


def test_a_serialized_api_envelope_is_not_usable():
    """The real shape of a DuckDuckGo miss.

    It is well-formed JSON with every field empty. Printed under "what the
    lookup returned" it tells the model a blob is a finding.
    """
    blob = '{"Abstract":"","AbstractText":"","RelatedTopics":[],"Results":[]}'
    assert normalize_tool_line(f"web_search (step1): {blob}").usable is False


def test_json_that_actually_contains_text_is_kept():
    """The rule is "no text in it", not "starts with a brace"."""
    blob = '{"Abstract":"Microsoft is trading at $412.30"}'
    assert normalize_tool_line(f"web_search (step1): {blob}").usable is True


# ======================================================
# 2. Merging
# ======================================================
def test_the_same_url_twice_is_one_finding():
    items = normalize_evidence([
        "web_search (step1): Microsoft at $412 https://example.com/msft",
        "web_search (step2): Microsoft is at $412 https://example.com/msft",
    ])
    assert len(items) == 1


def test_the_same_text_twice_is_one_finding():
    items = normalize_evidence([
        "web_search (step1): Microsoft is trading at $412.30",
        "web_search (step2): Microsoft is trading at $412.30",
    ])
    assert len(items) == 1


def test_different_findings_are_both_kept():
    items = normalize_evidence([
        "web_search (step1): Microsoft is at $412.30",
        "web_search (step2): Apple is at $189.50",
    ])
    assert len(items) == 2


def test_merging_is_deterministic():
    """Same evidence in, same list out -- every time and in any order."""
    lines = [
        "news_search (step2): Apple earnings beat estimates",
        "web_search (step1): Microsoft is at $412.30",
        "file_lookup (step3): from the notes",
    ]
    first = [item.as_dict() for item in normalize_evidence(lines)]
    for _ in range(5):
        assert [item.as_dict() for item in normalize_evidence(lines)] == first
    # Order of arrival must not change the order of merge.
    assert [item.as_dict() for item in normalize_evidence(list(reversed(lines)))] == first


def test_ordering_is_by_tool_priority_then_recency_then_title():
    items = normalize_evidence([
        "file_lookup (step1): from a file",
        "news_search (step2): from the news",
        "web_search (step3): from the web",
    ])
    assert [item.tool for item in items] == ["web_search", "news_search", "file_lookup"]


def test_a_timestamped_item_sorts_ahead_of_an_undated_one():
    """"Unknown when" is not "just now"."""
    dated = NormalizedEvidenceItem(source="web_search", tool="web_search", snippet="a",
                                   timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc))
    undated = NormalizedEvidenceItem(source="web_search", tool="web_search", snippet="b")
    assert er._sort_key(dated) < er._sort_key(undated)


# ======================================================
# 3 + 4. The block that reaches the model
# ======================================================
def test_evidence_present_but_all_of_it_empty_says_so():
    items = normalize_evidence([
        "web_search (step1): ",
        "web_search (step2): search returned no summary",
    ])
    assert format_evidence_block(items) == NO_USABLE_TEXT


def test_items_are_separated_by_a_blank_line():
    items = normalize_evidence([
        "web_search (step1): Microsoft is at $412.30",
        "web_search (step2): Apple is at $189.50",
    ])
    assert "\n\n" in format_evidence_block(items)


def test_the_fields_appear_in_order_title_snippet_url_timestamp():
    item = NormalizedEvidenceItem(
        source="web_search", tool="web_search", title="Microsoft stock",
        snippet="Trading at $412.30", url="https://example.com/msft",
        timestamp=datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
    )
    lines = [line.strip("- ").strip() for line in format_evidence_block([item]).splitlines()]

    assert lines[0] == "Microsoft stock"
    assert lines[1] == "Trading at $412.30"
    assert lines[2] == "https://example.com/msft"
    assert lines[3].startswith("2026-08-26T12:00:00")


def test_a_missing_field_is_omitted_not_labelled():
    """A label with nothing after it invites the model to fill the gap."""
    block = format_evidence_block(normalize_evidence(["web_search (step1): just a snippet"]))

    assert "just a snippet" in block
    assert "None" not in block
    assert "title" not in block.lower()
    assert "url" not in block.lower()


def test_the_unusable_items_do_not_appear_beside_the_usable_one():
    items = normalize_evidence([
        "web_search (step1): Microsoft is at $412.30",
        "web_search (step2): ",
        "web_search (step3): search returned no summary",
    ])
    block = format_evidence_block(items)

    assert "$412.30" in block
    assert "no summary" not in block
    assert block.count("-") >= 1


# ======================================================
# 6. Evidence-missing detection, across every tool outcome
# ======================================================
TOOL_OUTCOMES = {
    "empty list": [],
    "None": None,
    "malformed shape": {"unexpected": True},
    "empty reply string": {"raw": {}, "reply": ""},
    "serialized envelope": {"raw": {}, "reply": '{"Abstract":"","Results":[]}'},
    "tool raised": RuntimeError("network down"),
}


@pytest.mark.parametrize("label", sorted(TOOL_OUTCOMES))
def test_a_lookup_that_produced_nothing_is_reported_as_missing(label):
    """Five of these used to reach the model with an empty evidence block."""
    tool_returning(TOOL_OUTCOMES[label])
    result = run_turn()

    assert result.evidence_missing is True, f"{label} was reported as having evidence"
    assert result.kind == KIND_TEXT
    assert result.text == EVIDENCE_MISSING_ANSWER
    assert result.inference_request is None, "no model may be asked to fill the gap"


def test_a_real_result_still_reaches_the_model():
    tool_returning({"raw": {}, "reply": "Microsoft (MSFT) is trading at $412.30"})
    result = run_turn()

    assert result.evidence_missing is False
    assert result.kind == KIND_INFERENCE
    assert "$412.30" in result.inference_request.messages[-1].content


def test_partial_results_count_as_evidence():
    """One usable finding among several empty ones is still a finding."""
    from backend.core import tool_registry as core_reg

    calls = {"n": 0}

    def run(query):
        calls["n"] += 1
        return {"raw": {}, "reply": "Microsoft is trading at $412.30" if calls["n"] == 1 else ""}

    core_reg.register_tool(
        core_reg.ToolSchema(
            name="web_search", description="test double",
            parameters={"query": {"type": "string", "required": True}},
            permission=core_reg.PERMISSION_NETWORK,
        ),
        run,
    )
    result = run_turn()
    assert result.evidence_missing is False
    assert result.kind == KIND_INFERENCE


def test_a_turn_backed_by_notes_is_not_refused_for_a_thin_search(monkeypatch):
    """The regression this detection could easily have caused.

    A turn whose evidence came from the note store has evidence even when
    the lookup returned nothing, and must not be refused because the search
    was thin.
    """
    from backend.core import turn_orchestrator as orch

    tool_returning({"raw": {}, "reply": ""})

    real = orch.apply_history_policy

    def with_notes(*args, **kwargs):
        messages, policy = real(*args, **kwargs)
        policy = {**policy, "retrieved_items": [{"type": "note", "text": "a stored note"}]}
        return messages, policy

    monkeypatch.setattr(orch, "apply_history_policy", with_notes)
    result = run_turn()

    assert result.evidence_missing is False


# ======================================================
# 7. The evidence trace
# ======================================================
def test_the_trace_shows_all_four_stages_in_debug_mode():
    tool_returning({"raw": {}, "reply": "Microsoft is trading at $412.30"})
    trace = run_turn(debug=True).evidence_trace

    assert set(trace) == {"raw_tool_results", "normalized", "merged", "bundle"}
    assert "web_search" in trace["raw_tool_results"]
    assert trace["normalized"], "nothing was normalized"


def test_the_trace_is_absent_by_default():
    tool_returning({"raw": {}, "reply": "Microsoft is trading at $412.30"})
    assert run_turn(debug=False).evidence_trace == {}


def test_the_trace_separates_what_ran_from_what_was_usable():
    """The distinction that makes a bad turn diagnosable.

    "no evidence" alone cannot tell you whether the tool never ran, ran
    and returned blank, or returned a blob that normalized away.
    """
    tool_returning({"raw": {}, "reply": '{"Abstract":"","Results":[]}'})
    trace = run_turn(debug=True).evidence_trace

    assert trace["raw_tool_results"] == ["web_search"], "the tool did run"
    assert trace["normalized"], "and a line was rendered"
    assert trace["merged"] == [], "but none of it was usable"
