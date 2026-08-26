# backend/tests/test_phase7_long_context_synthesis.py
#
# Phase 7.4: long-context synthesis rules.
#
# This layer says how far an answer is reaching beyond what was asked. Its
# failure mode is not being wrong -- the inputs are already computed by
# Phases 6.2 through 7.3 -- but being noisy: a caveat printed on every turn
# is a caveat nobody reads, so most of this file is about the cases where
# each field must stay silent.
#
# The rules are a pure function, so nearly everything here runs on
# hand-built bundles with no database, no model and no clock.

from __future__ import annotations

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory
from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.aria_memory import memory_ranking
from backend.aria_synthesis.bundle_builder import build_evidence_bundle
from backend.aria_synthesis.conflict_detection import Conflict, detect_conflicts
from backend.aria_synthesis.long_context_rules import (
    GAP_TURNS,
    LongContextRules,
    apply_long_context_rules,
)
from backend.aria_synthesis.synthesis_engine import answer_with_evidence
from backend.aria_synthesis.synthesis_prompt import build_synthesis_prompt
from backend.context.evidence_selection import EvidenceTurn
from backend.context.goal_state import GoalState
from backend.files import file_ingestion as ingestion

RULES = LongContextRules()
SHADER_GOAL = "fix the shader error"
UNITY_GOAL = GoalState(goal=SHADER_GOAL, topic="unity", explicit=True)


def user(text: str, **extra) -> dict:
    return {"role": "user", "content": text, **extra}


def assistant(text: str, **extra) -> dict:
    return {"role": "assistant", "content": text, **extra}


def note_item(note_id: int, text: str, score: float = 0.8) -> dict:
    return {"type": "note", "id": note_id, "text": text, "combined_score": score}


def turn(text="a turn", *, topic="unity", goal=SHADER_GOAL, position=0,
         role="user", message_id=None) -> EvidenceTurn:
    return EvidenceTurn(
        message_id=message_id or str(position), role=role, text=text,
        timestamp=float(position), topic=topic, goal=goal, position=position,
    )


# A history whose evidence and turns both straddle a boundary.
MIXED = [
    user("Help me fix the shader error"),
    assistant("check the keyword set"),
    user("continue"),
]

ON_TOPIC = [note_item(1, "The shader keyword set is stale.")]
MIXED_EVIDENCE = [
    note_item(1, "The shader keyword set is stale."),
    note_item(2, "Book a flight to Oslo.", 0.5),
]


def bundle_for(items, conversation=MIXED, query="continue"):
    return build_evidence_bundle(query, items, conversation=conversation, now="FIXED")


# ======================================================
# 1. Topic focus
# ======================================================
def test_topic_focus_fires_on_a_multi_topic_bundle():
    focus = RULES.topic_focus(bundle_for(MIXED_EVIDENCE), "unity")
    assert focus == "Most evidence relates to unity; some comes from other topics (travel)."


def test_topic_focus_is_omitted_when_every_source_is_on_topic():
    assert RULES.topic_focus(bundle_for(ON_TOPIC), "unity") is None


def test_topic_focus_is_omitted_with_no_current_topic():
    bundle = build_evidence_bundle("q", MIXED_EVIDENCE, now="FIXED")
    assert RULES.topic_focus(bundle, "") is None


def test_topic_focus_falls_back_to_the_bundles_own_topic():
    bundle = bundle_for(MIXED_EVIDENCE)
    assert RULES.topic_focus(bundle, "") == RULES.topic_focus(bundle, bundle.current_topic)


def test_topic_focus_names_every_stray_topic():
    items = MIXED_EVIDENCE + [note_item(3, "The radar forecast shows rain.", 0.4)]
    focus = RULES.topic_focus(bundle_for(items), "unity")
    assert "travel" in focus and "weather" in focus


def test_topic_focus_is_deterministic():
    bundle = bundle_for(MIXED_EVIDENCE)
    assert RULES.topic_focus(bundle, "unity") == RULES.topic_focus(bundle, "unity")


# ======================================================
# 2. Goal focus
# ======================================================
def test_goal_focus_fires_on_a_multi_goal_bundle():
    focus = RULES.goal_focus(bundle_for(MIXED_EVIDENCE), UNITY_GOAL)
    assert focus == (
        f"Most evidence supports the goal: {SHADER_GOAL}; some relates to other goals."
    )


def test_goal_focus_is_omitted_when_every_source_supports_the_goal():
    assert RULES.goal_focus(bundle_for(ON_TOPIC), UNITY_GOAL) is None


def test_goal_focus_is_omitted_with_no_goal():
    bundle = build_evidence_bundle("q", MIXED_EVIDENCE, now="FIXED")
    assert RULES.goal_focus(bundle, None) is None


def test_goal_focus_accepts_a_bare_goal_string():
    bundle = bundle_for(MIXED_EVIDENCE)
    assert RULES.goal_focus(bundle, UNITY_GOAL) == RULES.goal_focus(bundle, None)


def test_goal_focus_is_deterministic():
    bundle = bundle_for(MIXED_EVIDENCE)
    assert RULES.goal_focus(bundle, UNITY_GOAL) == RULES.goal_focus(bundle, UNITY_GOAL)


# ======================================================
# 3 & 4. Cross-topic and cross-goal conflicts
# ======================================================
CROSS_TOPIC_ITEMS = [
    note_item(1, "Book a flight to Oslo. The cache is enabled.", 0.9),
    note_item(2, "The shader compiles fine. The cache is disabled.", 0.8),
]
SAME_TOPIC_ITEMS = [
    note_item(1, "The shader is fine. The cache is enabled.", 0.9),
    note_item(2, "The shader is fine. The cache is disabled.", 0.8),
]


def test_a_cross_topic_conflict_is_summarized():
    bundle = build_evidence_bundle("the cache", CROSS_TOPIC_ITEMS, now="FIXED")
    conflicts = detect_conflicts(bundle)
    summaries = RULES.cross_topic_conflicts(conflicts)
    assert summaries
    assert summaries[0].startswith("Sources from different topics disagree on ")


def test_a_same_topic_conflict_is_not_summarized():
    bundle = build_evidence_bundle("the cache", SAME_TOPIC_ITEMS, now="FIXED")
    assert RULES.cross_topic_conflicts(detect_conflicts(bundle)) == []


def test_a_cross_goal_conflict_is_summarized():
    bundle = build_evidence_bundle(
        "the cache", CROSS_TOPIC_ITEMS, conversation=MIXED, now="FIXED"
    )
    conflicts = detect_conflicts(bundle, bundle.goal_topic)
    summaries = RULES.cross_goal_conflicts(conflicts)
    assert summaries
    assert summaries[0].startswith("Sources outside the current goal disagree on ")


def test_a_conflict_inside_the_goal_is_not_summarized():
    bundle = build_evidence_bundle(
        "the cache", SAME_TOPIC_ITEMS, conversation=MIXED, now="FIXED"
    )
    assert RULES.cross_goal_conflicts(detect_conflicts(bundle, bundle.goal_topic)) == []


def test_no_conflicts_summarize_to_nothing():
    assert RULES.cross_topic_conflicts([]) == []
    assert RULES.cross_goal_conflicts(None) == []


def test_a_plain_conflict_object_is_tolerated():
    # A Conflict built without the cross-boundary flags must not raise.
    plain = Conflict(topic="the cache", statements=[])
    assert RULES.cross_topic_conflicts([plain]) == []
    assert RULES.cross_goal_conflicts([plain]) == []


def test_conflict_summaries_are_deterministic():
    bundle = build_evidence_bundle("the cache", CROSS_TOPIC_ITEMS, now="FIXED")
    conflicts = detect_conflicts(bundle)
    assert RULES.cross_topic_conflicts(conflicts) == RULES.cross_topic_conflicts(conflicts)


# ======================================================
# 5. Continuity warnings
# ======================================================
def test_a_large_gap_is_warned_about():
    turns = [turn(position=0), turn(position=GAP_TURNS + 1)]
    warnings = RULES.continuity_warnings(turns, UNITY_GOAL)
    assert any("Large gap in conversation history" in warning for warning in warnings)


def test_the_gap_warning_reports_how_much_was_skipped():
    turns = [turn(position=0), turn(position=25)]
    warning = RULES.continuity_warnings(turns, UNITY_GOAL)[0]
    assert "24 turns not carried" in warning


def test_a_small_gap_is_not_warned_about():
    turns = [turn(position=0), turn(position=3)]
    assert RULES.continuity_warnings(turns, UNITY_GOAL) == []


def test_the_gap_is_measured_at_its_widest():
    turns = [turn(position=0), turn(position=1), turn(position=GAP_TURNS + 5)]
    assert any("Large gap" in warning for warning in RULES.continuity_warnings(turns, UNITY_GOAL))


def test_the_gap_is_measured_in_conversation_order_not_score_order():
    # Selection returns turns best-first, so the positions arrive shuffled.
    shuffled = [turn(position=GAP_TURNS + 1), turn(position=0)]
    assert RULES.continuity_warnings(shuffled, UNITY_GOAL)


def test_turns_from_a_suspended_goal_are_warned_about():
    turns = [turn(position=0), turn(position=1, goal="book a flight")]
    warnings = RULES.continuity_warnings(turns, UNITY_GOAL)
    assert any("earlier goals" in warning for warning in warnings)


def test_a_single_goal_selection_is_not_warned_about():
    turns = [turn(position=0), turn(position=1)]
    assert RULES.continuity_warnings(turns, UNITY_GOAL) == []


def test_turns_with_no_goal_are_not_treated_as_a_different_goal():
    turns = [turn(position=0), turn(position=1, goal=None)]
    assert RULES.continuity_warnings(turns, UNITY_GOAL) == []


def test_both_warnings_can_fire_at_once():
    turns = [turn(position=0), turn(position=GAP_TURNS + 2, goal="book a flight")]
    assert len(RULES.continuity_warnings(turns, UNITY_GOAL)) == 2


def test_no_turns_means_no_warnings():
    assert RULES.continuity_warnings([], UNITY_GOAL) == []
    assert RULES.continuity_warnings(None, UNITY_GOAL) == []


def test_the_gap_threshold_is_configurable():
    turns = [turn(position=0), turn(position=5)]
    assert LongContextRules(gap_turns=3).continuity_warnings(turns, UNITY_GOAL)


def test_continuity_warnings_are_deterministic():
    turns = [turn(position=0), turn(position=GAP_TURNS + 2, goal="book a flight")]
    assert RULES.continuity_warnings(turns, UNITY_GOAL) == RULES.continuity_warnings(
        turns, UNITY_GOAL
    )


# ======================================================
# apply()
# ======================================================
def test_apply_returns_every_field():
    analysis = RULES.apply(bundle_for(ON_TOPIC), [], "unity", UNITY_GOAL, [])
    assert set(analysis) == {
        "topic_focus", "goal_focus",
        "cross_topic_conflicts", "cross_goal_conflicts", "continuity_warnings",
    }


def test_apply_is_empty_when_nothing_is_out_of_scope():
    analysis = RULES.apply(bundle_for(ON_TOPIC), [], "unity", UNITY_GOAL, [])
    assert analysis["topic_focus"] is None
    assert analysis["goal_focus"] is None
    assert analysis["cross_topic_conflicts"] == []
    assert analysis["continuity_warnings"] == []


def test_apply_falls_back_to_the_bundles_own_turns():
    bundle = bundle_for(ON_TOPIC)
    assert RULES.apply(bundle, None, "unity", UNITY_GOAL, []) == RULES.apply(
        bundle, bundle.turns, "unity", UNITY_GOAL, []
    )


def test_apply_makes_no_model_calls(monkeypatch):
    from backend.llm import semantic_embeddings

    def explode(*args, **kwargs):
        raise AssertionError("long-context rules must not call a model")

    monkeypatch.setattr(semantic_embeddings, "embed_texts_semantic", explode)
    monkeypatch.setattr(semantic_embeddings, "embed_text_semantic", explode)
    assert apply_long_context_rules(bundle_for(MIXED_EVIDENCE), [], "unity", UNITY_GOAL, [])


def test_apply_does_not_mutate_the_bundle():
    bundle = bundle_for(MIXED_EVIDENCE)
    before = (list(bundle.notes), list(bundle.turns), dict(bundle.meta))
    RULES.apply(bundle, bundle.turns, "unity", UNITY_GOAL, [])
    assert (bundle.notes, bundle.turns, bundle.meta) == before


def test_apply_is_deterministic():
    bundle = bundle_for(MIXED_EVIDENCE)
    assert RULES.apply(bundle, bundle.turns, "unity", UNITY_GOAL, []) == RULES.apply(
        bundle, bundle.turns, "unity", UNITY_GOAL, []
    )


# ======================================================
# 6. Prompt integration
# ======================================================
def test_the_section_appears_when_there_is_something_to_say():
    prompt = build_synthesis_prompt(
        "q", bundle_for(MIXED_EVIDENCE), [], None,
        RULES.apply(bundle_for(MIXED_EVIDENCE), [], "unity", UNITY_GOAL, []),
    )
    assert "Long-Context Analysis:" in prompt
    assert "Most evidence relates to unity" in prompt


def test_the_section_is_omitted_when_every_field_is_empty():
    bundle = bundle_for(ON_TOPIC)
    prompt = build_synthesis_prompt(
        "q", bundle, [], None, RULES.apply(bundle, [], "unity", UNITY_GOAL, [])
    )
    assert "Long-Context Analysis" not in prompt


def test_the_section_is_omitted_without_an_analysis():
    assert "Long-Context Analysis" not in build_synthesis_prompt("q", bundle_for(ON_TOPIC))


def test_empty_fields_are_omitted_individually():
    bundle = bundle_for(MIXED_EVIDENCE)
    analysis = RULES.apply(bundle, [], "unity", UNITY_GOAL, [])
    prompt = build_synthesis_prompt("q", bundle, [], None, analysis)
    assert "Most evidence relates to" in prompt
    assert "Large gap" not in prompt


def test_the_prompt_is_unchanged_without_the_new_argument():
    bundle = bundle_for(MIXED_EVIDENCE)
    assert build_synthesis_prompt("q", bundle, [], None) == build_synthesis_prompt(
        "q", bundle, [], None, None
    )


def test_the_section_follows_the_conflicts():
    seen = []
    answer_with_evidence(
        "continue", CROSS_TOPIC_ITEMS, conversation=MIXED,
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert seen[0].index("Conflicts Detected:") < seen[0].index("Long-Context Analysis:")


def test_the_section_precedes_the_answer_structure():
    seen = []
    answer_with_evidence(
        "continue", MIXED_EVIDENCE, conversation=MIXED,
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert seen[0].index("Long-Context Analysis:") < seen[0].index("Answer Structure:")


def test_cross_boundary_conflicts_are_pointed_at_not_repeated():
    # Every one of them is already printed, worded, in the conflicts
    # section. Saying each twice would read as two separate problems.
    seen = []
    answer_with_evidence(
        "continue", CROSS_TOPIC_ITEMS, conversation=MIXED,
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    analysis = seen[0].split("Long-Context Analysis:")[1].split("Answer Structure:")[0]
    assert "of the disagreements above" in analysis
    assert "Sources from different topics disagree on" not in analysis


def test_formatting_is_deterministic():
    bundle = bundle_for(MIXED_EVIDENCE)
    analysis = RULES.apply(bundle, bundle.turns, "unity", UNITY_GOAL, [])
    prompts = [build_synthesis_prompt("q", bundle, [], None, analysis) for _ in range(5)]
    assert all(prompt == prompts[0] for prompt in prompts)


# ======================================================
# 7. Synthesis behaviour
# ======================================================
LONG_HISTORY = (
    [user("Help me fix the shader error"), assistant("check the keyword set")]
    + [user(f"book a flight for day {n}") for n in range(GAP_TURNS + 2)]
    + [user("Help me fix the shader error"), user("continue")]
)


def prompt_seen(query, items, conversation):
    seen = []
    answer_with_evidence(
        query, items, conversation=conversation,
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    return seen[0]


def test_the_model_is_told_the_evidence_reaches_past_the_topic():
    assert "Most evidence relates to unity" in prompt_seen(
        "continue", MIXED_EVIDENCE, MIXED
    )


def test_the_model_is_warned_about_a_large_gap():
    assert "Large gap in conversation history" in prompt_seen(
        "continue", ON_TOPIC, LONG_HISTORY
    )


def test_the_model_is_not_warned_about_a_short_history():
    assert "Large gap" not in prompt_seen("continue", ON_TOPIC, MIXED)


def test_no_continuity_is_claimed_for_turns_that_were_dropped():
    # The dropped turns must be absent and their absence acknowledged --
    # the two halves of not hallucinating continuity.
    prompt = prompt_seen("continue", ON_TOPIC, LONG_HISTORY)
    assert "book a flight" not in prompt
    assert "continuity may be limited" in prompt


def test_no_topic_switch_is_invented():
    prompt = prompt_seen("continue", MIXED_EVIDENCE, MIXED)
    assert "Most evidence relates to unity" in prompt
    assert "Most evidence relates to travel" not in prompt


def test_no_goal_switch_is_invented():
    prompt = prompt_seen("continue", MIXED_EVIDENCE, MIXED)
    assert f"Most evidence supports the goal: {SHADER_GOAL}" in prompt
    assert "supports the goal: book" not in prompt.lower()


def test_the_analysis_stays_silent_without_a_conversation():
    seen = []
    answer_with_evidence(
        "continue", MIXED_EVIDENCE,
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert "Long-Context Analysis" not in seen[0]


def test_the_no_hallucination_rules_survive():
    prompt = prompt_seen("continue", MIXED_EVIDENCE, MIXED)
    assert "Use ONLY the evidence above." in prompt
    assert "Never invent an answer." in prompt


def test_the_engine_returns_the_answer_unchanged():
    assert answer_with_evidence(
        "continue", ON_TOPIC, conversation=MIXED, generate=lambda prompt: "an answer"
    ) == "an answer"


# ======================================================
# 8. Determinism and no drift
# ======================================================
def test_the_same_history_gives_the_same_prompt():
    prompts = [prompt_seen("continue", MIXED_EVIDENCE, LONG_HISTORY) for _ in range(3)]
    assert all(prompt == prompts[0] for prompt in prompts)


def test_the_same_history_gives_the_same_analysis():
    bundle = bundle_for(MIXED_EVIDENCE, conversation=LONG_HISTORY)
    first = RULES.apply(bundle, bundle.turns, bundle.current_topic, UNITY_GOAL, [])
    second = RULES.apply(bundle, bundle.turns, bundle.current_topic, UNITY_GOAL, [])
    assert first == second


def test_ranking_weights_are_unchanged():
    assert memory_ranking.WEIGHT_SEMANTIC == 0.45
    assert memory_ranking.WEIGHT_KEYWORD == 0.25
    assert memory_ranking.WEIGHT_RECENCY == 0.20
    assert memory_ranking.WEIGHT_TYPE == 0.10


def test_score_floors_are_unchanged():
    assert semantic.DEFAULT_MIN_SCORE == 0.6
    assert ingestion.DEFAULT_MIN_SCORE == 0.55


def test_hybrid_output_is_unchanged_by_the_analysis(db):
    semantic.index_note(notes_store.save_note("the release pipeline runs nightly"))
    before = aria_memory.search_hybrid("release pipeline", min_score=0.0)
    RULES.apply(bundle_for(MIXED_EVIDENCE), [], "unity", UNITY_GOAL, [])
    after = aria_memory.search_hybrid("release pipeline", min_score=0.0)
    assert [i["keyword_score"] for i in before] == [i["keyword_score"] for i in after]
    assert [i["semantic_score"] for i in before] == [i["semantic_score"] for i in after]
