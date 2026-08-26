# backend/tests/test_phase7_evidence_selection.py
#
# Phase 7.3: long-context evidence selection.
#
# The claim being tested is that a long history can be cut down without
# losing the turn that mattered. The case that proves it is a goal stated
# early, buried under a detour, and still selected -- which is exactly the
# turn a "keep the last N messages" window throws away first.
#
# The weights are tested as an ordering rather than as numbers: what has to
# hold is that a turn on the current topic beats one that is merely recent
# and wordy, however many weak signals the second one collects.
#
# No model is loaded anywhere here, and no clock is read.

from __future__ import annotations

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory
from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.aria_memory import memory_ranking
from backend.aria_synthesis.bundle_builder import build_evidence_bundle
from backend.aria_synthesis.synthesis_engine import answer_with_evidence
from backend.context.current_topic import current_topic_of
from backend.context.evidence_filters import (
    RECENCY_WINDOW_SECONDS,
    goal_filter,
    recency_filter,
    semantic_filter,
    topic_filter,
)
from backend.context.evidence_selection import (
    DEFAULT_TOP_K,
    SCORE_THRESHOLD,
    WEIGHT_GOAL,
    WEIGHT_RECENCY,
    WEIGHT_SEMANTIC,
    WEIGHT_TOPIC,
    EvidenceSelector,
    EvidenceTurn,
    score_turn,
    select_turns,
)
from backend.context.goal_state import GoalState
from backend.context.goal_tracking import current_goal_of, goal_timeline
from backend.files import file_ingestion as ingestion


def user(text: str, **extra) -> dict:
    return {"role": "user", "content": text, **extra}


def assistant(text: str, **extra) -> dict:
    return {"role": "assistant", "content": text, **extra}


def turn(text="a turn", *, topic="unity", goal="fix the shader error",
         timestamp=0.0, role="user", message_id="m") -> EvidenceTurn:
    return EvidenceTurn(
        message_id=message_id, role=role, text=text,
        timestamp=timestamp, topic=topic, goal=goal,
    )


SHADER_GOAL = "fix the shader error"
UNITY_GOAL = GoalState(goal=SHADER_GOAL, topic="unity", explicit=True)

# A goal stated early, buried under a detour, then resumed.
BURIED = (
    [user("Help me fix the shader error"), assistant("check the keyword set")]
    + [user(f"what is the forecast on day {n}") for n in range(6)]
    + [user("Help me fix the shader error"), user("continue")]
)


# ======================================================
# 1. Topic filter
# ======================================================
def test_the_topic_filter_is_one_on_a_match():
    assert topic_filter(turn(topic="unity"), "unity") == 1


def test_the_topic_filter_is_zero_otherwise():
    assert topic_filter(turn(topic="weather"), "unity") == 0


def test_a_turn_with_no_topic_scores_zero():
    assert topic_filter(turn(topic=""), "unity") == 0


def test_the_topic_filter_is_deterministic():
    item = turn(topic="unity")
    assert all(topic_filter(item, "unity") == 1 for _ in range(5))


def test_the_topic_filter_separates_a_multi_topic_history():
    topics = [segment.topic for segment, _goal in goal_timeline(BURIED)]
    assert topic_filter(turn(topic=topics[0]), "unity") == 1
    assert topic_filter(turn(topic=topics[2]), "unity") == 0


# ======================================================
# 2. Goal filter
# ======================================================
def test_the_goal_filter_is_one_on_a_match():
    assert goal_filter(turn(goal=SHADER_GOAL), UNITY_GOAL) == 1


def test_the_goal_filter_is_zero_on_a_different_goal():
    assert goal_filter(turn(goal="book a flight"), UNITY_GOAL) == 0


def test_a_turn_with_no_goal_scores_zero():
    assert goal_filter(turn(goal=None), UNITY_GOAL) == 0


def test_no_current_goal_scores_zero():
    assert goal_filter(turn(goal=SHADER_GOAL), None) == 0


def test_the_goal_filter_accepts_a_bare_string():
    assert goal_filter(turn(goal=SHADER_GOAL), SHADER_GOAL) == 1


def test_turns_kept_by_a_continuity_marker_share_the_goal():
    # "continue" does not restate the goal, so the only thing tying its turn
    # to the earlier work is the goal the tracker carried across it.
    goals = [goal.goal if goal else None for _segment, goal in goal_timeline(BURIED)]
    assert goals[0] == SHADER_GOAL
    assert goals[-1] == SHADER_GOAL


def test_an_explicit_goal_overrides_an_inferred_one():
    history = [user("my prefab broke"), user("Help me fix the shader error")]
    goal = current_goal_of(history)
    assert goal.explicit
    assert goal.goal == SHADER_GOAL


# ======================================================
# 3. Recency filter
# ======================================================
def test_a_turn_at_the_reference_time_is_fully_recent():
    assert recency_filter(turn(timestamp=100.0), 100.0) == 1.0


def test_recency_decays_linearly():
    half = RECENCY_WINDOW_SECONDS / 2
    assert recency_filter(turn(timestamp=0.0), half) == pytest.approx(0.5)


def test_recency_reaches_zero_at_the_window():
    assert recency_filter(turn(timestamp=0.0), RECENCY_WINDOW_SECONDS) == 0.0


def test_recency_never_goes_negative():
    assert recency_filter(turn(timestamp=0.0), RECENCY_WINDOW_SECONDS * 10) == 0.0


def test_a_turn_stamped_after_the_reference_is_fully_recent():
    # Clock skew from a client, not a message from the future.
    assert recency_filter(turn(timestamp=200.0), 100.0) == 1.0


def test_recency_is_deterministic():
    item = turn(timestamp=0.0)
    assert all(recency_filter(item, 300.0) == recency_filter(item, 300.0) for _ in range(5))


# ======================================================
# 4. Semantic filter
# ======================================================
def test_full_overlap_scores_one():
    assert semantic_filter(turn("the shader keyword set is stale"), "shader keyword") == 1.0


def test_partial_overlap_is_a_fraction():
    assert semantic_filter(turn("the shader is fine"), "shader keyword") == pytest.approx(0.5)


def test_no_overlap_scores_zero():
    assert semantic_filter(turn("zebra giraffe"), "shader keyword") == 0.0


def test_matching_is_whole_word():
    assert semantic_filter(turn("the rebuild finished"), "build") == 0.0
    assert semantic_filter(turn("the build finished"), "build") == 1.0


def test_matching_is_case_insensitive():
    assert semantic_filter(turn("The SHADER broke"), "shader") == 1.0


def test_stopwords_do_not_dilute_the_query():
    assert semantic_filter(turn("the shader"), "the shader") == 1.0


def test_a_query_with_no_content_words_abstains():
    assert semantic_filter(turn("the shader"), "the and of") == 0.0
    assert semantic_filter(turn("the shader"), "") == 0.0


def test_the_semantic_filter_is_deterministic():
    item = turn("the shader keyword set")
    assert all(semantic_filter(item, "shader keyword") == 1.0 for _ in range(5))


def test_the_semantic_filter_uses_no_embeddings(monkeypatch):
    from backend.llm import semantic_embeddings

    def explode(*args, **kwargs):
        raise AssertionError("evidence selection must not embed anything")

    monkeypatch.setattr(semantic_embeddings, "embed_texts_semantic", explode)
    monkeypatch.setattr(semantic_embeddings, "embed_text_semantic", explode)
    assert semantic_filter(turn("the shader keyword set"), "shader") == 1.0


# ======================================================
# 5. Scoring
# ======================================================
def test_the_weights_are_the_specified_ones():
    assert (WEIGHT_TOPIC, WEIGHT_GOAL, WEIGHT_RECENCY, WEIGHT_SEMANTIC) == (4.0, 3.0, 2.0, 1.0)


def test_the_total_is_the_weighted_sum():
    item = turn("the shader keyword set", topic="unity", goal=SHADER_GOAL, timestamp=100.0)
    total, parts = score_turn(item, "unity", UNITY_GOAL, 100.0, "shader keyword")
    assert parts == {"topic": 1, "goal": 1, "recency": 1.0, "semantic": 1.0}
    assert total == pytest.approx(10.0)


def test_a_topic_match_alone_clears_the_threshold():
    item = turn(topic="unity", goal=None, timestamp=0.0)
    total, _ = score_turn(item, "unity", UNITY_GOAL, RECENCY_WINDOW_SECONDS, "zebra")
    assert total == pytest.approx(WEIGHT_TOPIC)
    assert total >= SCORE_THRESHOLD


def test_a_goal_match_alone_clears_the_threshold():
    item = turn(topic="weather", goal=SHADER_GOAL, timestamp=0.0)
    total, _ = score_turn(item, "unity", UNITY_GOAL, RECENCY_WINDOW_SECONDS, "zebra")
    assert total == pytest.approx(WEIGHT_GOAL)
    assert total >= SCORE_THRESHOLD


def test_recency_alone_does_not_clear_the_threshold():
    item = turn(topic="weather", goal=None, timestamp=100.0)
    total, _ = score_turn(item, "unity", UNITY_GOAL, 100.0, "zebra")
    assert total == pytest.approx(WEIGHT_RECENCY)
    assert total < SCORE_THRESHOLD


def test_topic_outranks_everything_else_combined():
    # The property the weights exist for: a turn on the current topic beats
    # one that is merely fresh, wordy, and about something else.
    on_topic = turn("zebra", topic="unity", goal=None, timestamp=0.0)
    off_topic = turn("shader keyword", topic="weather", goal=None, timestamp=100.0)
    on_score, _ = score_turn(on_topic, "unity", UNITY_GOAL, 100.0, "shader keyword")
    off_score, _ = score_turn(off_topic, "unity", UNITY_GOAL, 100.0, "shader keyword")
    assert on_score > off_score


def test_goal_outranks_recency_and_semantic_combined():
    on_goal = turn("zebra", topic="weather", goal=SHADER_GOAL, timestamp=0.0)
    off_goal = turn("shader keyword", topic="weather", goal=None, timestamp=100.0)
    on_score, _ = score_turn(on_goal, "unity", UNITY_GOAL, 100.0, "shader keyword")
    off_score, _ = score_turn(off_goal, "unity", UNITY_GOAL, 100.0, "shader keyword")
    assert on_score > off_score


def test_recency_outranks_semantic():
    # The weight claim compares full advantage on each axis: entirely fresh
    # and wordless beats entirely stale and word-perfect.
    fresh = turn("zebra", topic="weather", goal=None, timestamp=RECENCY_WINDOW_SECONDS)
    stale = turn("shader keyword", topic="weather", goal=None, timestamp=0.0)
    fresh_score, _ = score_turn(
        fresh, "unity", UNITY_GOAL, RECENCY_WINDOW_SECONDS, "shader keyword"
    )
    stale_score, _ = score_turn(
        stale, "unity", UNITY_GOAL, RECENCY_WINDOW_SECONDS, "shader keyword"
    )
    assert fresh_score > stale_score


def test_a_partial_recency_edge_can_be_outweighed_by_word_overlap():
    # The other side of the same arithmetic, worth stating because the
    # weights alone suggest recency always wins: a turn only slightly older
    # keeps most of its recency, so a full word match can still overtake a
    # brand-new turn that shares nothing. Only the two strong signals --
    # topic and goal -- are unconditional.
    fresh = turn("zebra", topic="weather", goal=None, timestamp=100.0)
    wordy = turn("shader keyword", topic="weather", goal=None, timestamp=0.0)
    fresh_score, _ = score_turn(fresh, "unity", UNITY_GOAL, 100.0, "shader keyword")
    wordy_score, _ = score_turn(wordy, "unity", UNITY_GOAL, 100.0, "shader keyword")
    assert wordy_score > fresh_score
    # Neither reaches the threshold, so neither is carried on this alone.
    assert max(fresh_score, wordy_score) < SCORE_THRESHOLD


def test_scoring_is_deterministic():
    item = turn("the shader keyword set", timestamp=50.0)
    first = score_turn(item, "unity", UNITY_GOAL, 100.0, "shader")
    second = score_turn(item, "unity", UNITY_GOAL, 100.0, "shader")
    assert first == second


# ======================================================
# 6. Selection
# ======================================================
def test_off_topic_off_goal_turns_are_excluded():
    selected = select_turns(BURIED, "unity", current_goal_of(BURIED), query="shader")
    assert selected
    assert not any("forecast" in item.text for item in selected)


def test_the_turn_that_stated_the_goal_survives_a_detour():
    # The headline claim: a window over the last N turns would have dropped
    # this one, because six weather turns were said after it.
    selected = select_turns(BURIED, "unity", current_goal_of(BURIED), query="shader")
    assert any("Help me fix the shader error" in item.text for item in selected)


def test_selection_is_capped_at_top_k():
    history = [user("my prefab broke") for _ in range(30)]
    selected = select_turns(history, "unity", current_goal_of(history), query="prefab")
    assert len(selected) == DEFAULT_TOP_K


def test_the_cap_is_configurable():
    history = [user("my prefab broke") for _ in range(30)]
    selected = EvidenceSelector(top_k=3).select(history, "unity", current_goal_of(history))
    assert len(selected) == 3


def test_results_come_back_best_first():
    selected = select_turns(BURIED, "unity", current_goal_of(BURIED), query="shader")
    scores = [item.score for item in selected]
    assert scores == sorted(scores, reverse=True)


def test_ties_keep_conversation_order():
    history = [user(f"my prefab number {n} broke") for n in range(4)]
    selected = select_turns(history, "unity", current_goal_of(history), query="zebra")
    ordered = [item for item in selected if item.score == selected[0].score]
    assert [item.message_id for item in ordered] == sorted(
        (item.message_id for item in ordered), key=int
    )


def test_every_selected_turn_carries_its_score():
    for item in select_turns(BURIED, "unity", current_goal_of(BURIED), query="shader"):
        assert item.score >= SCORE_THRESHOLD


def test_an_empty_history_selects_nothing():
    assert select_turns([], "unity", None) == []
    assert select_turns(None, "unity", None) == []


def test_selection_does_not_mutate_the_messages():
    history = [dict(message) for message in BURIED]
    before = [dict(message) for message in history]
    select_turns(history, "unity", current_goal_of(history), query="shader")
    assert history == before


def test_the_reference_time_comes_from_the_conversation():
    # Not from a clock: the same history selects the same turns whenever it
    # is replayed.
    history = [user("my prefab broke", timestamp=1000.0), user("continue", timestamp=1001.0)]
    first = select_turns(history, "unity", current_goal_of(history))
    second = select_turns(history, "unity", current_goal_of(history))
    assert [item.message_id for item in first] == [item.message_id for item in second]


def test_an_explicit_reference_time_is_honoured():
    history = [user("my prefab broke", timestamp=0.0)]
    stale = select_turns(history, "weather", None, now=RECENCY_WINDOW_SECONDS * 5)
    assert stale == []


def test_selection_is_deterministic():
    runs = [
        [item.message_id for item in select_turns(
            BURIED, "unity", current_goal_of(BURIED), query="shader"
        )]
        for _ in range(5)
    ]
    assert all(run == runs[0] for run in runs)


def test_selection_makes_no_model_calls(monkeypatch):
    from backend.llm import semantic_embeddings

    def explode(*args, **kwargs):
        raise AssertionError("evidence selection must not call a model")

    monkeypatch.setattr(semantic_embeddings, "embed_texts_semantic", explode)
    monkeypatch.setattr(semantic_embeddings, "embed_text_semantic", explode)
    assert select_turns(BURIED, "unity", current_goal_of(BURIED), query="shader")


# ======================================================
# 7. Retrieval routing
# ======================================================
def test_retrieval_still_routes_by_the_current_work(db):
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    items = {i["id"]: i for i in aria_memory.search_hybrid(
        "prefab", min_score=0.0, conversation=BURIED
    )}
    assert items[note_id]["keyword_score"] > 0.0


def test_output_is_unchanged_for_a_single_topic_history(db):
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    short = aria_memory.search_hybrid(
        "prefab", min_score=0.0, conversation=[user("my prefab keeps breaking")]
    )
    long = aria_memory.search_hybrid(
        "prefab", min_score=0.0,
        conversation=[user("my prefab keeps breaking")] * 12,
    )
    assert [i["id"] for i in short] == [i["id"] for i in long]
    assert [i["keyword_score"] for i in short] == [i["keyword_score"] for i in long]


def test_a_long_detour_does_not_change_the_result(db):
    # The point of selection: forty irrelevant turns must retrieve the same
    # thing as none.
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    clean = [user("Help me fix the shader error"), user("continue")]
    polluted = (
        [user("Help me fix the shader error")]
        + [user(f"what is the forecast on day {n}") for n in range(20)]
        + [user("Help me fix the shader error"), user("continue")]
    )
    assert [i["keyword_score"] for i in aria_memory.search_hybrid(
        "prefab", min_score=0.0, conversation=clean
    )] == [i["keyword_score"] for i in aria_memory.search_hybrid(
        "prefab", min_score=0.0, conversation=polluted
    )]


def test_no_conversation_leaves_retrieval_untouched(db):
    semantic.index_note(notes_store.save_note("the release pipeline runs nightly"))
    plain = aria_memory.search_hybrid("release pipeline", min_score=0.0)
    explicit = aria_memory.search_hybrid(
        "release pipeline", min_score=0.0, conversation=None
    )
    assert [i["keyword_score"] for i in plain] == [i["keyword_score"] for i in explicit]


def test_the_file_searches_accept_a_long_history(db, tmp_path):
    path = tmp_path / "unity.md"
    path.write_text("the gameobject was saved as a reusable asset", encoding="utf-8")
    ingestion.ingest_and_index(str(path))
    assert ingestion.search_files_semantic(
        "prefab", min_score=0.0, conversation=BURIED
    ) is not None


def test_retrieval_is_deterministic_under_a_long_history(db):
    semantic.index_note(notes_store.save_note("the gameobject was a reusable asset"))
    runs = [
        [i["keyword_score"] for i in aria_memory.search_hybrid(
            "prefab", min_score=0.0, conversation=BURIED
        )]
        for _ in range(3)
    ]
    assert all(run == runs[0] for run in runs)


# ======================================================
# 8. Synthesis routing
# ======================================================
def note_item(note_id: int, text: str, score: float = 0.8) -> dict:
    return {"type": "note", "id": note_id, "text": text, "combined_score": score}


def test_the_bundle_records_the_selected_turn_ids():
    bundle = build_evidence_bundle(
        "why is it still failing", [note_item(1, "The shader keyword set is stale.")],
        conversation=BURIED, now="FIXED",
    )
    assert bundle.meta["selected_turns"]
    assert bundle.meta["selected_turns"] == [turn.message_id for turn in bundle.turns]


def test_the_bundle_carries_the_turns_themselves():
    bundle = build_evidence_bundle(
        "why is it still failing", [note_item(1, "x")], conversation=BURIED, now="FIXED",
    )
    assert any("Help me fix the shader error" in turn.text for turn in bundle.turns)
    assert not any("forecast" in turn.text for turn in bundle.turns)


def test_no_conversation_means_no_turns():
    bundle = build_evidence_bundle("q", [note_item(1, "x")], now="FIXED")
    assert bundle.turns == []
    assert bundle.meta["selected_turns"] == []


def test_the_prompt_shows_the_selected_turns():
    seen = []
    answer_with_evidence(
        "why is it still failing", [note_item(1, "The shader keyword set is stale.")],
        conversation=BURIED,
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert "Conversation Context" in seen[0]
    assert "Help me fix the shader error" in seen[0]


def test_the_prompt_omits_the_turns_that_were_not_selected():
    seen = []
    answer_with_evidence(
        "why is it still failing", [note_item(1, "The shader keyword set is stale.")],
        conversation=BURIED,
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert "forecast" not in seen[0]


def test_the_prompt_has_no_context_section_without_a_conversation():
    seen = []
    answer_with_evidence(
        "why is it still failing", [note_item(1, "x")],
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert "Conversation Context" not in seen[0]


def test_the_context_section_follows_the_evidence():
    seen = []
    answer_with_evidence(
        "why is it still failing", [note_item(1, "x")], conversation=BURIED,
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert seen[0].index("Notes:") < seen[0].index("Conversation Context")


def test_cross_topic_turns_are_hedged_not_dropped():
    # A turn kept on goal but from another topic is background, not noise.
    history = [
        user("Help me fix the shader error"),
        user("anyway what is the radar forecast"),
        user("continue"),
    ]
    seen = []
    answer_with_evidence(
        "continue", [note_item(1, "x")], conversation=history,
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    if "another topic" in seen[0]:
        assert "treat those as background" in seen[0]


def test_a_single_topic_selection_is_not_hedged():
    seen = []
    answer_with_evidence(
        "continue", [note_item(1, "x")],
        conversation=[user("Help me fix the shader error"), user("continue")],
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert "another goal" not in seen[0]


def test_the_bundle_reports_the_span_of_its_turns():
    bundle = build_evidence_bundle(
        "q", [note_item(1, "x")], conversation=BURIED, now="FIXED",
    )
    assert "unity" in bundle.turn_topics
    assert bundle.turn_goals


# ======================================================
# 9. Determinism and no drift
# ======================================================
def test_the_same_history_gives_the_same_bundle():
    first = build_evidence_bundle("q", [note_item(1, "x")], conversation=BURIED, now="F")
    second = build_evidence_bundle("q", [note_item(1, "x")], conversation=BURIED, now="F")
    assert first == second
    assert first.meta["selected_turns"] == second.meta["selected_turns"]


def test_the_same_history_gives_the_same_prompt():
    seen = []
    for _ in range(3):
        answer_with_evidence(
            "why is it still failing", [note_item(1, "The shader keyword set is stale.")],
            conversation=BURIED,
            generate=lambda prompt: (seen.append(prompt), "x")[1],
        )
    assert all(prompt == seen[0] for prompt in seen)


def test_ranking_weights_are_unchanged():
    assert memory_ranking.WEIGHT_SEMANTIC == 0.45
    assert memory_ranking.WEIGHT_KEYWORD == 0.25
    assert memory_ranking.WEIGHT_RECENCY == 0.20
    assert memory_ranking.WEIGHT_TYPE == 0.10


def test_score_floors_are_unchanged():
    assert semantic.DEFAULT_MIN_SCORE == 0.6
    assert ingestion.DEFAULT_MIN_SCORE == 0.55


def test_selection_weights_are_distinct_from_ranking_weights():
    # Two different scoring systems that must never be confused: one orders
    # retrieved evidence, the other picks conversation turns.
    assert WEIGHT_TOPIC != memory_ranking.WEIGHT_SEMANTIC


def test_hybrid_output_is_unchanged_by_selection(db):
    semantic.index_note(notes_store.save_note("the release pipeline runs nightly"))
    before = aria_memory.search_hybrid("release pipeline", min_score=0.0)
    select_turns(BURIED, "unity", current_goal_of(BURIED), query="shader")
    after = aria_memory.search_hybrid("release pipeline", min_score=0.0)
    assert [i["keyword_score"] for i in before] == [i["keyword_score"] for i in after]
    assert [i["semantic_score"] for i in before] == [i["semantic_score"] for i in after]
