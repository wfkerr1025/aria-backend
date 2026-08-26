# backend/tests/test_phase7_goal_tracking.py
#
# Phase 7.2: goal tracking and intent continuity.
#
# The case that matters is the one where the user says almost nothing.
# "continue", "why is it still failing", "fix this" carry no subject at all,
# and every one of them has to resolve to the piece of work already in
# progress rather than starting a new one or falling back to a bare topic.
#
# The other half of the file is the opposite risk: not treating ordinary
# sentences as goal statements. "The shader is broken" is a report and "why
# is it still failing" is a question; a tracker that read either as a new
# goal would rewrite what the user is working on every time they spoke.
#
# No model is loaded anywhere here.

from __future__ import annotations

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory
from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.aria_memory import memory_ranking
from backend.aria_synthesis.bundle_builder import build_evidence_bundle
from backend.aria_synthesis.conflict_detection import detect_conflicts
from backend.aria_synthesis.conflict_summary import (
    CROSS_GOAL_LEAD,
    summarize_conflicts,
)
from backend.aria_synthesis.synthesis_engine import answer_with_evidence
from backend.context.continuity_markers import (
    CONTINUATION_MARKERS,
    detect_continuity,
    implies_continuation,
)
from backend.context.goal_classifier import classify_goal
from backend.context.goal_state import (
    SOURCE_CARRY_FORWARD,
    SOURCE_EXPLICIT,
    SOURCE_IMPLICIT,
    GoalStack,
    GoalState,
)
from backend.context.goal_tracking import (
    current_goal_of,
    track_goals,
    update_goal_stack,
)
from backend.aria_synthesis.template_classifier import TemplateType, template_for_goal
from backend.files import file_ingestion as ingestion


def user(text: str, **extra) -> dict:
    return {"role": "user", "content": text, **extra}


def assistant(text: str, **extra) -> dict:
    return {"role": "assistant", "content": text, **extra}


SHADER_GOAL = "fix the shader error"

# The flagship history: a goal, an answer, and a follow-up that names nothing.
CONTINUED = [
    user("Help me fix the shader error"),
    assistant("check the keyword set"),
    user("why is it still failing"),
]


# ======================================================
# 1. Explicit goal detection
# ======================================================
@pytest.mark.parametrize("text,goal", [
    ("Help me fix the shader error", "fix the shader error"),
    ("Let's implement conflict detection", "implement conflict detection"),
    ("Explain Phase 7", "explain phase 7"),
    ("I want to build the release pipeline", "build the release pipeline"),
    ("Could you please fix the shader", "fix the shader"),
    ("we need to migrate the database", "migrate the database"),
    ("write the tests for the parser", "write the tests for the parser"),
])
def test_a_stated_goal_is_extracted(text, goal):
    assert classify_goal(text) == goal


def test_framing_is_stripped_but_the_verb_is_kept():
    assert classify_goal("help me fix the shader") == "fix the shader"
    assert classify_goal("fix the shader") == "fix the shader"


def test_stacked_framing_comes_off_completely():
    assert classify_goal("could you please help me fix the shader") == "fix the shader"


def test_two_phrasings_normalize_to_one_goal():
    assert classify_goal("Help me fix the shader error!") == classify_goal(
        "Could you fix the shader error?"
    )


def test_normalization_lowercases_and_flattens_punctuation():
    assert classify_goal("Fix  the   Shader-Error.") == "fix the shader error"


@pytest.mark.parametrize("text", [
    "the shader is broken",              # a report, not a request
    "why is it still failing",           # a question
    "continue",                          # a continuity marker
    "thanks, that worked",
    "the build takes ten minutes",
    "",
])
def test_text_that_states_no_goal_returns_none(text):
    assert classify_goal(text) is None


@pytest.mark.parametrize("text", ["fix this", "fix it", "fix that", "fix the rest"])
def test_a_verb_with_no_object_is_not_a_goal(text):
    # It begins with an action but names nothing to act on, so it points at
    # work already in progress. continuity_markers handles it.
    assert classify_goal(text) is None
    assert detect_continuity(text) is not None


def test_a_bare_verb_is_not_a_goal():
    assert classify_goal("fix") is None
    assert classify_goal("implement") is None


def test_goal_extraction_is_deterministic():
    assert all(
        classify_goal("Help me fix the shader error") == SHADER_GOAL for _ in range(5)
    )


def test_goal_extraction_makes_no_model_calls(monkeypatch):
    from backend.llm import semantic_embeddings

    def explode(*args, **kwargs):
        raise AssertionError("goal classification must not call a model")

    monkeypatch.setattr(semantic_embeddings, "embed_texts_semantic", explode)
    monkeypatch.setattr(semantic_embeddings, "embed_text_semantic", explode)
    assert classify_goal("Help me fix the shader error") == SHADER_GOAL


# ======================================================
# 2. Continuity markers
# ======================================================
@pytest.mark.parametrize("text,marker", [
    ("continue", "continue"),
    ("next please", "next"),
    ("keep going", "keep going"),
    ("fix this", "fix this"),
    ("fix the rest", "fix the rest"),
    ("why is it still failing", "why is it still failing"),
    ("anyway, what about the shader", "anyway"),
    ("okay, now the other one", "okay now"),
])
def test_each_marker_is_detected(text, marker):
    assert detect_continuity(text) == marker


def test_the_longest_marker_wins():
    # "why is it still failing" contains "still failing"; the longer phrase
    # is the more specific description of what the user did.
    assert detect_continuity("why is it still failing") == "why is it still failing"


@pytest.mark.parametrize("text", [
    "help me fix the shader error",
    "the build is slow",
    "what is a prefab",
    "",
])
def test_text_with_no_marker_returns_none(text):
    assert detect_continuity(text) is None


def test_a_marker_does_not_fire_inside_a_longer_word():
    assert detect_continuity("we use nextcloud for storage") is None
    assert detect_continuity("the continuous integration job") is None


def test_marker_detection_is_case_insensitive():
    assert detect_continuity("CONTINUE") == "continue"


def test_continuation_markers_are_a_subset():
    assert implies_continuation("continue")
    assert implies_continuation("next")
    assert not implies_continuation("anyway")
    assert not implies_continuation(None)
    assert CONTINUATION_MARKERS <= set(detect_continuity(m) or m for m in CONTINUATION_MARKERS)


def test_marker_detection_is_deterministic():
    assert all(detect_continuity("keep going") == "keep going" for _ in range(5))


# ======================================================
# 3. Goal stack behaviour
# ======================================================
def test_a_stated_goal_pushes():
    stack = track_goals([user("Help me fix the shader error")])
    assert len(stack) == 1
    assert stack.current.goal == SHADER_GOAL
    assert stack.current.explicit
    assert stack.current.continuity_source == SOURCE_EXPLICIT


def test_the_goal_takes_the_topic_of_the_message_that_stated_it():
    assert track_goals([user("Help me fix the shader error")]).current.topic == "unity"


def test_a_second_stated_goal_pushes_on_top():
    stack = track_goals([
        user("Help me fix the shader error"),
        user("Let's implement conflict detection"),
    ])
    assert stack.goals == [SHADER_GOAL, "implement conflict detection"]
    assert stack.current.goal == "implement conflict detection"


def test_a_suspended_goal_is_kept_not_discarded():
    stack = track_goals([
        user("Help me fix the shader error"),
        user("Let's implement conflict detection"),
    ])
    assert SHADER_GOAL in stack.goals
    assert stack.find("unity").goal == SHADER_GOAL


def test_restating_the_same_goal_refreshes_rather_than_duplicating():
    stack = track_goals([
        user("Help me fix the shader error"),
        user("Could you fix the shader error?"),
    ])
    assert len(stack) == 1


def test_a_continuity_marker_updates_the_current_goal():
    stack = track_goals(CONTINUED)
    assert len(stack) == 1
    assert stack.current.goal == SHADER_GOAL
    assert stack.current.continuity_source == "why is it still failing"


def test_a_continuity_marker_keeps_the_goal_topic():
    # The follow-up names no topic. Reading that as a move would strip the
    # goal of the topic it was created with, exactly when work continues.
    assert track_goals(CONTINUED).current.topic == "unity"


def test_a_continuation_marker_keeps_its_topic_across_a_real_change():
    stack = track_goals([
        user("Help me fix the shader error"),
        user("what is the forecast"),
        user("Help me fix the shader error"),
        user("continue"),
    ])
    assert stack.current.goal == SHADER_GOAL
    assert stack.current.topic == "unity"


def test_a_resumption_marker_accepts_an_identified_new_topic():
    stack = track_goals([
        user("Help me fix the shader error"),
        user("anyway what does the radar forecast say"),
    ])
    assert stack.current.goal == SHADER_GOAL
    assert stack.current.topic == "weather"
    assert stack.current.continuity_source == "anyway"


def test_a_marker_with_no_goal_in_play_creates_nothing():
    assert len(track_goals([user("continue")])) == 0


def test_carry_forward_updates_on_the_same_topic():
    stack = track_goals([
        user("Help me fix the shader error"),
        user("the prefab also looks wrong"),
    ])
    assert len(stack) == 1
    assert stack.current.continuity_source == SOURCE_CARRY_FORWARD


def test_a_topic_change_pushes_an_implicit_goal():
    stack = track_goals([
        user("Help me fix the shader error"),
        user("what is the forecast tomorrow"),
    ])
    assert len(stack) == 2
    assert stack.current.goal == "weather"
    assert not stack.current.explicit
    assert stack.current.continuity_source == SOURCE_IMPLICIT


def test_an_unclassifiable_message_does_not_start_new_work():
    stack = track_goals([user("Help me fix the shader error"), user("thanks, noted")])
    assert len(stack) == 1
    assert stack.current.goal == SHADER_GOAL


def test_assistant_messages_create_no_goals():
    assert len(track_goals([assistant("Help me fix the shader error")])) == 0


def test_assistant_messages_do_not_change_an_existing_goal():
    before = track_goals([user("Help me fix the shader error")])
    after = track_goals([
        user("Help me fix the shader error"),
        assistant("Let's implement conflict detection instead"),
    ])
    assert before.goals == after.goals
    assert after.current.goal == SHADER_GOAL


def test_an_empty_history_has_no_goal():
    assert current_goal_of([]) is None
    assert current_goal_of(None) is None


# --- immutability ---
def test_the_input_stack_is_never_mutated():
    stack = track_goals([user("Help me fix the shader error")])
    snapshot = list(stack.stack)
    update_goal_stack(stack, user("continue"), "unity")
    assert stack.stack == snapshot


def test_updating_creates_a_new_state_rather_than_editing():
    stack = track_goals([user("Help me fix the shader error")])
    original = stack.current
    updated = update_goal_stack(stack, user("continue"), "unity", at=99.0)
    assert original.last_updated != updated.current.last_updated
    assert original.last_updated == stack.current.last_updated


def test_the_messages_are_not_mutated():
    history = [dict(message) for message in CONTINUED]
    before = [dict(message) for message in history]
    track_goals(history)
    assert history == before


def test_a_goal_state_can_be_built_directly():
    state = GoalState(goal="fix it", topic="unity")
    assert GoalStack().pushed(state).current is state


# ======================================================
# 4. Retrieval routing
# ======================================================
def test_retrieval_uses_the_goal_topic(db):
    # "prefab" only expands toward "gameobject" in the Unity domain. The
    # last message names no topic at all, so only the goal can supply it.
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    items = {i["id"]: i for i in aria_memory.search_hybrid(
        "prefab", min_score=0.0, conversation=CONTINUED
    )}
    assert items[note_id]["keyword_score"] > 0.0


def test_a_goal_outlives_an_intervening_topic(db):
    # The user asked about the weather, then said "continue". A continuation
    # marker holds the goal on its own topic, so the search stays on Unity.
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    history = [
        user("Help me fix the shader error"),
        user("Help me fix the shader error"),
        user("continue"),
    ]
    items = {i["id"]: i for i in aria_memory.search_hybrid(
        "prefab", min_score=0.0, conversation=history
    )}
    assert items[note_id]["keyword_score"] > 0.0


def test_cross_goal_history_does_not_steer_the_search(db):
    # A finished weather goal must not colour a Unity one.
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    history = [
        user("what is the forecast tomorrow"),
        user("Help me fix the shader error"),
        user("why is it still failing"),
    ]
    items = {i["id"]: i for i in aria_memory.search_hybrid(
        "prefab", min_score=0.0, conversation=history
    )}
    assert items[note_id]["keyword_score"] > 0.0


def test_an_explicit_routing_intent_still_wins(db):
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    items = {i["id"]: i for i in aria_memory.search_hybrid(
        "prefab", min_score=0.0, conversation=CONTINUED, routing_intent="weather.query"
    )}
    assert items.get(note_id, {}).get("keyword_score", 0.0) == 0.0


def test_output_is_unchanged_for_a_single_goal_history(db):
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    plain = aria_memory.search_hybrid(
        "prefab", min_score=0.0, conversation=[user("my prefab keeps breaking")]
    )
    with_goal = aria_memory.search_hybrid(
        "prefab", min_score=0.0, conversation=[user("Help me fix the shader error")]
    )
    assert [i["id"] for i in plain] == [i["id"] for i in with_goal]
    assert [i["keyword_score"] for i in plain] == [i["keyword_score"] for i in with_goal]


def test_no_conversation_leaves_retrieval_untouched(db):
    note_id = notes_store.save_note("the release pipeline runs nightly")
    semantic.index_note(note_id)

    plain = aria_memory.search_hybrid("release pipeline", min_score=0.0)
    explicit = aria_memory.search_hybrid(
        "release pipeline", min_score=0.0, conversation=None
    )
    assert [i["keyword_score"] for i in plain] == [i["keyword_score"] for i in explicit]
    assert [i["semantic_score"] for i in plain] == [i["semantic_score"] for i in explicit]


def test_search_ranked_and_file_search_accept_a_conversation(db, tmp_path):
    semantic.index_note(notes_store.save_note("the gameobject was a reusable asset"))
    path = tmp_path / "unity.md"
    path.write_text("the gameobject was saved as a reusable asset", encoding="utf-8")
    ingestion.ingest_and_index(str(path))

    assert aria_memory.search_ranked("prefab", conversation=CONTINUED) is not None
    assert ingestion.search_files_semantic(
        "prefab", min_score=0.0, conversation=CONTINUED
    ) is not None


def test_retrieval_is_deterministic_under_a_goal(db):
    semantic.index_note(notes_store.save_note("the gameobject was a reusable asset"))
    runs = [
        [i["keyword_score"] for i in aria_memory.search_hybrid(
            "prefab", min_score=0.0, conversation=CONTINUED
        )]
        for _ in range(3)
    ]
    assert all(run == runs[0] for run in runs)


# ======================================================
# 5. Synthesis routing
# ======================================================
def note_item(note_id: int, text: str, score: float = 0.8) -> dict:
    return {"type": "note", "id": note_id, "text": text, "combined_score": score}


def test_the_bundle_records_the_goal():
    bundle = build_evidence_bundle(
        "why is it still failing", [note_item(1, "The shader keyword set is stale.")],
        conversation=CONTINUED, now="FIXED",
    )
    assert bundle.meta["current_goal"] == SHADER_GOAL
    assert bundle.meta["goal_topic"] == "unity"
    assert bundle.meta["goal_explicit"] is True
    assert bundle.current_goal == SHADER_GOAL


def test_an_inferred_goal_is_marked_as_inferred():
    bundle = build_evidence_bundle(
        "q", [note_item(1, "x")],
        conversation=[user("my prefab keeps breaking")], now="FIXED",
    )
    assert bundle.meta["goal_explicit"] is False


def test_no_conversation_records_no_goal():
    bundle = build_evidence_bundle("q", [note_item(1, "x")], now="FIXED")
    assert bundle.meta["current_goal"] is None
    assert bundle.current_goal == ""


def test_the_prompt_names_the_goal():
    seen = []
    answer_with_evidence(
        "why is it still failing", [note_item(1, "The shader keyword set is stale.")],
        conversation=CONTINUED,
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert f"Current Goal (stated): {SHADER_GOAL}." in seen[0]


def test_the_prompt_marks_an_inferred_goal():
    seen = []
    answer_with_evidence(
        "why", [note_item(1, "The prefab is broken.")],
        conversation=[user("my prefab keeps breaking")],
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert "Current Goal (inferred):" in seen[0]


def test_the_prompt_flags_evidence_from_outside_the_goal():
    seen = []
    answer_with_evidence(
        "why is it still failing",
        [note_item(1, "The shader keyword set is stale."),
         note_item(2, "Book a flight to Oslo.", 0.5)],
        conversation=CONTINUED,
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    # Phase 7.4 moved this into the Long-Context Analysis section.
    assert "some relates to other goals" in seen[0]


def test_the_prompt_says_nothing_about_goals_without_a_conversation():
    seen = []
    answer_with_evidence(
        "why is it still failing", [note_item(1, "The shader keyword set is stale.")],
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert "Current Goal" not in seen[0]


def test_a_conflict_reaching_outside_the_goal_is_worded_differently():
    bundle = build_evidence_bundle(
        "the cache",
        [note_item(1, "Book a flight to Oslo. The cache is enabled.", 0.9),
         note_item(2, "Another trip booking. The cache is disabled.", 0.8)],
        conversation=CONTINUED, now="FIXED",
    )
    conflicts = detect_conflicts(bundle, bundle.goal_topic)
    assert conflicts and conflicts[0].cross_goal
    assert summarize_conflicts(conflicts)[0].startswith(CROSS_GOAL_LEAD)


def test_a_conflict_inside_the_goal_keeps_the_plain_wording():
    bundle = build_evidence_bundle(
        "the cache",
        [note_item(1, "The shader is fine. The cache is enabled.", 0.9),
         note_item(2, "The shader is fine. The cache is disabled.", 0.8)],
        conversation=CONTINUED, now="FIXED",
    )
    conflicts = detect_conflicts(bundle, bundle.goal_topic)
    assert conflicts and not conflicts[0].cross_goal
    assert not summarize_conflicts(conflicts)[0].startswith(CROSS_GOAL_LEAD)


def test_conflicts_without_a_goal_behave_as_before():
    bundle = build_evidence_bundle(
        "the cache",
        [note_item(1, "The shader is fine. The cache is enabled.", 0.9),
         note_item(2, "The shader is fine. The cache is disabled.", 0.8)],
        now="FIXED",
    )
    conflicts = detect_conflicts(bundle)
    assert conflicts and not conflicts[0].cross_goal


def test_the_goal_shapes_the_answer_when_the_query_does_not():
    # "continue" asks for no particular shape. The goal is the only thing
    # left that says what kind of answer is wanted.
    seen = []
    answer_with_evidence(
        "continue", [note_item(1, "Conflict detection compares sentences.")],
        conversation=[user("Let's implement conflict detection"), user("continue")],
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert "numbered sequence of steps" in seen[0]


@pytest.mark.parametrize("goal,fragment", [
    ("fix the shader error", "possible causes"),
    ("implement conflict detection", "numbered sequence of steps"),
    ("compare addressables and assetbundles", "comparison of the alternatives"),
    ("review the release checklist", "checklist of required items"),
    ("summarize the migration", "short overview"),
])
def test_each_goal_verb_chooses_a_shape(goal, fragment):
    seen = []
    answer_with_evidence(
        "continue", [note_item(1, "Some relevant evidence about the work.")],
        conversation=[user(f"Let's {goal}"), user("continue")],
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert fragment in seen[0]


def test_an_explaining_goal_asks_for_no_structure():
    seen = []
    answer_with_evidence(
        "continue", [note_item(1, "Some relevant evidence about the work.")],
        conversation=[user("Explain phase 7"), user("continue")],
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert "Answer Structure" not in seen[0]


def test_an_unknown_goal_verb_falls_back_to_reading_the_goal():
    assert template_for_goal("wibble the frobnicator") is None


def test_the_goal_verb_map_only_reads_the_first_word():
    # "the build error" is the object of the work, not a request for steps.
    assert template_for_goal("fix the build error") is TemplateType.DIAGNOSTIC


def test_a_query_that_asks_for_a_shape_still_wins():
    seen = []
    answer_with_evidence(
        "compare the two approaches",
        [note_item(1, "Conflict detection compares sentences.")],
        conversation=[user("Let's implement conflict detection"), user("continue")],
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert "comparison of the alternatives" in seen[0]


def test_the_prompt_never_invents_a_goal_switch():
    seen = []
    answer_with_evidence(
        "why is it still failing",
        [note_item(1, "The shader keyword set is stale."),
         note_item(2, "Book a flight to Oslo.", 0.5)],
        conversation=CONTINUED,
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert f"Current Goal (stated): {SHADER_GOAL}" in seen[0]
    assert "Current Goal (stated): book" not in seen[0].lower()


# ======================================================
# 6. Determinism and no drift
# ======================================================
def test_the_same_history_gives_the_same_stack():
    first, second = track_goals(CONTINUED), track_goals(CONTINUED)
    assert first == second
    assert first.goals == second.goals


def test_the_same_history_gives_the_same_prompt():
    seen = []
    for _ in range(3):
        answer_with_evidence(
            "why is it still failing",
            [note_item(1, "The shader keyword set is stale."),
             note_item(2, "Book a flight to Oslo.", 0.5)],
            conversation=CONTINUED,
            generate=lambda prompt: (seen.append(prompt), "x")[1],
        )
    assert all(prompt == seen[0] for prompt in seen)


def test_the_same_history_gives_the_same_bundle():
    first = build_evidence_bundle("q", [note_item(1, "x")], conversation=CONTINUED, now="F")
    second = build_evidence_bundle("q", [note_item(1, "x")], conversation=CONTINUED, now="F")
    assert first == second


def test_goal_tracking_makes_no_model_calls(monkeypatch):
    from backend.llm import semantic_embeddings

    def explode(*args, **kwargs):
        raise AssertionError("goal tracking must not call a model")

    monkeypatch.setattr(semantic_embeddings, "embed_texts_semantic", explode)
    monkeypatch.setattr(semantic_embeddings, "embed_text_semantic", explode)
    assert track_goals(CONTINUED).current.goal == SHADER_GOAL


def test_ranking_weights_are_unchanged():
    assert memory_ranking.WEIGHT_SEMANTIC == 0.45
    assert memory_ranking.WEIGHT_KEYWORD == 0.25
    assert memory_ranking.WEIGHT_RECENCY == 0.20
    assert memory_ranking.WEIGHT_TYPE == 0.10


def test_score_floors_are_unchanged():
    assert semantic.DEFAULT_MIN_SCORE == 0.6
    assert ingestion.DEFAULT_MIN_SCORE == 0.55


def test_hybrid_output_is_unchanged_by_goal_tracking(db):
    semantic.index_note(notes_store.save_note("the release pipeline runs nightly"))

    before = aria_memory.search_hybrid("release pipeline", min_score=0.0)
    track_goals(CONTINUED)
    after = aria_memory.search_hybrid("release pipeline", min_score=0.0)

    assert [i["id"] for i in before] == [i["id"] for i in after]
    assert [i["keyword_score"] for i in before] == [i["keyword_score"] for i in after]
    assert [i["semantic_score"] for i in before] == [i["semantic_score"] for i in after]
