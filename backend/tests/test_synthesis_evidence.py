# backend/tests/test_synthesis_evidence.py
#
# A prompt must not tell a model it has nothing while handing it something.
#
# The Evidence Summary line described the evidence bundle alone -- notes and
# files -- while tool results were rendered further down under their own
# heading. So a turn that searched the web and got an answer opened with
#
#     Evidence Summary: No evidence was retrieved for this query.
#
# directly above
#
#     Tool Results:
#     - web_search (step1): Microsoft (MSFT) is trading at $412.30 ...
#
# and then instructed the model to "use ONLY the evidence above" and, by
# rule 4, to say "not found" and stop if the evidence did not answer the
# question.
#
# Capable models resolved the contradiction by believing the section they
# could see. Small ones obeyed the sentence. Measured on this machine,
# same query, same stubbed web_search:
#
#     gpt-4         "Microsoft (MSFT) is currently trading at $412.30 ..."
#     phi-3-mini    the answer, wrapped in echoed scaffolding
#     qwen-0.5b     "not found\nnot found\nnot found..." to max_tokens
#
# These tests pin the sentence to the condition it describes: it appears
# when there is genuinely nothing, and never when there is something.

from __future__ import annotations

import os

import pytest

from backend.aria_synthesis.bundle_builder import build_evidence_bundle
from backend.aria_synthesis.synthesis_prompt import build_synthesis_prompt
from backend.tools.tool_registry import (
    STATUS_ERROR,
    STATUS_NOT_EXECUTED,
    STATUS_OK,
    ToolResult,
)


NO_EVIDENCE = "No evidence was retrieved for this query."

SEARCH_HIT = ToolResult(
    "web_search", "step1", STATUS_OK,
    "Microsoft (MSFT) is trading at $412.30, up 1.2% (source: example.com)",
)


def chunk(file_id: int, path: str, score: float = 0.8) -> dict:
    return {"type": "file_chunk", "file_id": file_id, "path": path,
            "section": "Pipeline", "text": "content", "combined_score": score}


def bundle_of(items, query="how much is MSFT trading at"):
    return build_evidence_bundle(query, items, now="FIXED")


def prompt_for(items, tool_results=None, query="how much is MSFT trading at"):
    return build_synthesis_prompt(
        query, bundle_of(items, query), [], None, None, None, tool_results,
    )


# ======================================================
# The fix
# ======================================================
def test_a_turn_with_tool_evidence_is_never_told_it_has_none():
    """The reported bug, stated directly."""
    prompt = prompt_for([], [SEARCH_HIT])

    assert NO_EVIDENCE not in prompt
    assert "$412.30" in prompt


def test_a_turn_with_no_evidence_at_all_still_says_so():
    """The sentence is being made conditional, not deleted.

    A confident answer from nothing is the failure the sentence exists to
    prevent, and it still has to fire when it is true.
    """
    assert NO_EVIDENCE in prompt_for([])


def test_retrieved_evidence_alone_is_unchanged():
    """No tool results: the summary is exactly what it always was."""
    prompt = prompt_for([chunk(1, "docs/build.md")])

    assert NO_EVIDENCE not in prompt
    assert "1 file excerpt retrieved" in prompt


def test_both_kinds_of_evidence_are_counted():
    prompt = prompt_for([chunk(1, "docs/build.md")], [SEARCH_HIT])

    assert NO_EVIDENCE not in prompt
    assert "1 file excerpt retrieved" in prompt
    assert "1 tool result" in prompt


# ======================================================
# What counts as evidence
# ======================================================
@pytest.mark.parametrize("status", [STATUS_ERROR, STATUS_NOT_EXECUTED])
def test_a_tool_that_produced_nothing_is_not_evidence(status):
    """A lookup that failed or never ran leaves the turn with nothing.

    Saying otherwise would swap this bug for its mirror image: a model
    told it has evidence, finding none, and filling the gap itself.
    """
    result = ToolResult("web_search", "step1", status, "failed")
    assert NO_EVIDENCE in prompt_for([], [result])


def test_the_summary_agrees_with_the_section_below_it():
    """The count and the list cannot disagree.

    Both are derived from the same predicate, so a result printed under
    Tool Results is one the summary counted, and vice versa.
    """
    mixed = [SEARCH_HIT, ToolResult("web_search", "step2", STATUS_ERROR, "timed out")]
    prompt = prompt_for([], mixed)

    assert "1 tool result" in prompt      # only the successful one
    assert "Tool Results:" in prompt
    assert NO_EVIDENCE not in prompt


def test_the_summary_points_at_where_the_evidence_is():
    """"Use ONLY the evidence above" has to resolve to something.

    With no notes and no files, the only evidence is in the Tool Results
    section, so the summary names it rather than leaving the model to
    infer that a separate heading counts.
    """
    prompt = prompt_for([], [SEARCH_HIT])
    summary = next(l for l in prompt.splitlines() if l.startswith("Evidence Summary:"))

    assert "Tool Results" in summary


def test_no_sentence_in_the_prompt_denies_the_evidence():
    """The requirement in its general form, not just the one sentence.

    Asserted over the whole prompt rather than the summary line, so a
    denial reintroduced anywhere else -- a rule, a template, a caveat --
    fails here too.
    """
    prompt = prompt_for([], [SEARCH_HIT]).lower()

    for denial in ("no evidence was retrieved", "no evidence is available",
                   "you have no evidence", "there is no evidence"):
        assert denial not in prompt


# ======================================================
# End to end, against a real small model
# ======================================================
# Opt-in: this loads a GGUF and generates, which is slow and depends on
# what is installed. Off by default so the suite stays deterministic;
# ARIA_LIVE_MODEL_TESTS=1 runs it. It is the only test here that observes
# the behaviour the fix was actually for, so it is worth keeping runnable.
LIVE = os.environ.get("ARIA_LIVE_MODEL_TESTS") == "1"


@pytest.mark.skipif(not LIVE, reason="set ARIA_LIVE_MODEL_TESTS=1 to run a real model")
def test_a_small_model_answers_from_the_lookup_instead_of_looping():
    import asyncio
    import json

    from backend.core import tool_registry as core_reg
    from backend.core.mode_manager import ModeManager
    from backend.websocket.handlers import WebSocketHandler

    core_reg.register_tool(
        core_reg.ToolSchema(
            name="web_search", description="test double",
            parameters={"query": {"type": "string", "required": True}},
            permission=core_reg.PERMISSION_NETWORK,
        ),
        lambda query: {"raw": {}, "reply": "Microsoft (MSFT) is trading at $412.30, "
                                           "up 1.2% (source: example.com)"},
    )

    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, raw):
            self.sent.append(json.loads(raw) if isinstance(raw, str) else raw)

    async def scenario():
        ws = FakeWebSocket()
        handler = WebSocketHandler(ws)
        await handler._dispatch({
            "type": "chat_request",
            "payload": {
                "messages": [{"role": "user", "content": "how much is MSFT trading at"}],
                "conversationId": "live-evidence",
                "multiTurn": True,
                "skipSafetyCheck": True,
                "modelId": "qwen2.5-0.5b-instruct-q4_k_m",
            },
        })
        await handler.wait_for_turns()
        await asyncio.sleep(0.2)
        return "".join(p.get("token", "") for p in ws.sent
                       if p.get("type") == "stream_token")

    mode_manager = ModeManager()
    original = mode_manager.get_mode()
    try:
        mode_manager.set_mode("local")
        answer = asyncio.run(scenario())
    finally:
        mode_manager.set_mode(original)
        core_reg.register_builtin_tools()

    assert "$412.30" in answer, f"the lookup did not reach the answer: {answer!r}"
    assert "not found" not in answer.lower(), (
        f"the model still refused despite having evidence: {answer!r}"
    )
