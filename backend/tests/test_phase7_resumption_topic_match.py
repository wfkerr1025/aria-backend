# backend/tests/test_phase7_resumption_topic_match.py
#
# The widened resumption gate.
#
# Before this change, resumption fired only on a message that named no topic
# at all. "Anyway, the prefab is broken too" names Unity -- which is exactly
# where the suspended work was left -- and was read as a fresh subject.
#
# Widening a gate is the kind of change that quietly makes a rule fire on
# things it should not, so most of this file is the boundary: the named topic
# has to belong to a goal the user *stated* and *suspended*, and every other
# combination must still fall through to the ordinary topic rules.

from __future__ import annotations

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory
from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.aria_memory import memory_ranking
from backend.context.goal_state import (
    SOURCE_EXPLICIT,
    SOURCE_RESUMPTION,
    GoalStack,
    GoalState,
)
from backend.context.goal_tracking import track_goals, update_goal_stack
from backend.files import file_ingestion as ingestion

SHADER_GOAL = "fix the shader error"
API_GOAL = "build the api"
DB_GOAL = "migrate the database"


def user(text: str, **extra) -> dict:
    return {"role": "user", "content": text, **extra}


def assistant(text: str, **extra) -> dict:
    return {"role": "assistant", "content": text, **extra}


def explicit(goal: str, topic: str) -> GoalState:
    return GoalState(goal=goal, topic=topic, explicit=True)


def implicit(topic: str) -> GoalState:
    return GoalState(goal=topic, topic=topic, explicit=False)


def detoured(tail: str) -> list[dict]:
    """A stated Unity goal, an answer, a weather detour, then `tail`."""
    return [
        user("Help me fix the shader error"),
        assistant("check the keyword set"),
        user("what is the forecast tomorrow"),
        assistant("rain in the afternoon"),
        user(tail),
    ]


def goal_after(tail: str) -> GoalState:
    return track_goals(detoured(tail)).current


# ======================================================
# 1. Topic-naming resumption
# ======================================================
def test_naming_the_suspended_goals_topic_resumes_it():
    resumed = goal_after("anyway, the prefab is broken too")
    assert resumed.goal == SHADER_GOAL
    assert resumed.topic == "unity"
    assert resumed.continuity_source == SOURCE_RESUMPTION


@pytest.mark.parametrize("tail", [
    "anyway, the prefab is broken too",
    "as I was saying, the shader still fails",
    "back to that, the gameobject lost its reference",
    "returning to that, the monobehaviour is missing",
])
def test_every_resumption_marker_works_with_a_named_topic(tail):
    assert goal_after(tail).goal == SHADER_GOAL


def test_the_detour_is_dropped_from_the_stack():
    stack = track_goals(detoured("anyway, the prefab is broken too"))
    assert stack.goals == [SHADER_GOAL]


def test_the_resumed_goal_stays_explicit():
    assert goal_after("anyway, the prefab is broken too").explicit


def test_the_resumed_goal_is_refreshed():
    stack = track_goals(detoured("anyway, the prefab is broken too"))
    assert stack.current.last_updated == 4.0
    assert stack.current.created_at == 0.0


def test_the_nearest_matching_goal_is_resumed():
    history = [
        user("Help me fix the shader error"),
        user("Help me build the api"),
        user("what is the forecast tomorrow"),
        user("anyway, the prefab is broken too"),
    ]
    stack = track_goals(history)
    assert stack.current.goal == SHADER_GOAL
    # The api goal was on another topic, so it stays suspended below.
    assert API_GOAL in stack.goals


# ======================================================
# 2. Bare resumption still works
# ======================================================
def test_a_topicless_resumption_still_resumes():
    resumed = goal_after("anyway, why is it still failing")
    assert resumed.goal == SHADER_GOAL
    assert resumed.continuity_source == SOURCE_RESUMPTION


def test_a_bare_marker_still_resumes():
    assert goal_after("anyway").goal == SHADER_GOAL


def test_a_topicless_resumption_with_no_stated_goal_is_a_no_op():
    history = [user("my prefab keeps breaking"), user("what is the forecast"), user("anyway")]
    assert track_goals(history).current.goal != SHADER_GOAL


# ======================================================
# 3. Topic-naming resumption must NOT fire
# ======================================================
def test_it_does_not_fire_on_the_current_implicit_goals_topic():
    # "anyway, what is the forecast" names weather, and weather is only an
    # implicit goal made from the detour itself. Returning to a guess is not
    # a return to anything.
    resumed = goal_after("anyway, what is the forecast tomorrow")
    assert resumed.goal != SHADER_GOAL
    assert resumed.topic == "weather"


def test_it_does_not_fire_on_an_unrelated_new_subject():
    resumed = goal_after("anyway, the routing provider fell back")
    assert resumed.goal != SHADER_GOAL
    assert resumed.topic == "backend"


def test_it_does_not_fire_on_a_topic_no_goal_was_stated_on():
    resumed = goal_after("anyway, book me a flight to Oslo")
    assert resumed.goal != SHADER_GOAL


def test_it_does_not_fire_when_the_matching_goal_is_implicit():
    # An implicit Unity goal is a guess from a topic change. Naming Unity
    # again must not promote that guess into a resumption.
    history = [
        user("my prefab keeps breaking"),
        user("what is the forecast tomorrow"),
        user("anyway, the shader is broken too"),
    ]
    assert track_goals(history).current.continuity_source != SOURCE_RESUMPTION


def test_it_does_not_fire_on_the_top_goals_own_topic():
    # The goal in play is what a resumption moves away from. Matching it
    # would make the marker a no-op that reported itself as a return.
    history = [user("Help me fix the shader error"), user("anyway, the prefab is broken too")]
    assert track_goals(history).current.continuity_source != SOURCE_RESUMPTION


def test_a_named_topic_without_a_marker_is_an_ordinary_topic_change():
    resumed = goal_after("the prefab is broken too")
    assert resumed.continuity_source != SOURCE_RESUMPTION


# ======================================================
# 4. Continuity markers still win
# ======================================================
@pytest.mark.parametrize("tail,source", [
    ("anyway, continue", "continue"),
    ("anyway, fix this", "fix this"),
    ("anyway, next", None),
    ("anyway, keep going", "keep going"),
])
def test_an_immediate_marker_suppresses_resumption(tail, source):
    resumed = goal_after(tail)
    assert resumed.goal != SHADER_GOAL
    assert resumed.continuity_source != SOURCE_RESUMPTION
    if source:
        assert resumed.continuity_source == source


def test_an_immediate_marker_suppresses_topic_matching_resumption_too():
    # Both routes into resumption are closed by the same guard.
    resumed = goal_after("anyway, continue with the prefab")
    assert resumed.continuity_source != SOURCE_RESUMPTION


# ======================================================
# 5. Explicit goals still win
# ======================================================
def test_a_stated_goal_in_the_same_turn_wins():
    resumed = goal_after("anyway, help me build the api")
    assert resumed.goal == API_GOAL
    assert resumed.continuity_source == SOURCE_EXPLICIT


def test_a_stated_goal_on_the_suspended_topic_pushes_rather_than_resuming():
    # Naming Unity *and* stating new Unity work is new work, not a return.
    resumed = goal_after("anyway, help me fix the prefab importer")
    assert resumed.goal == "fix the prefab importer"
    assert resumed.continuity_source == SOURCE_EXPLICIT


def test_restating_the_suspended_goal_is_recognised_as_the_same_goal():
    stack = track_goals(detoured("anyway, help me fix the shader error"))
    assert stack.current.goal == SHADER_GOAL
    assert SHADER_GOAL in stack.goals


# ======================================================
# 6. find_suspended_explicit_goal_by_topic
# ======================================================
def test_the_finder_returns_a_suspended_explicit_goal():
    stack = GoalStack([explicit(SHADER_GOAL, "unity"), implicit("weather")])
    assert stack.find_suspended_explicit_goal_by_topic("unity").goal == SHADER_GOAL


def test_the_finder_skips_the_top_of_the_stack():
    stack = GoalStack([explicit(DB_GOAL, "backend"), explicit(SHADER_GOAL, "unity")])
    assert stack.find_suspended_explicit_goal_by_topic("unity") is None


def test_the_finder_ignores_implicit_goals():
    stack = GoalStack([implicit("unity"), implicit("weather")])
    assert stack.find_suspended_explicit_goal_by_topic("unity") is None


def test_the_finder_takes_the_nearest_match():
    older = explicit("fix the old shader", "unity")
    nearer = explicit(SHADER_GOAL, "unity")
    stack = GoalStack([older, nearer, implicit("weather")])
    assert stack.find_suspended_explicit_goal_by_topic("unity") is nearer


def test_the_finder_returns_none_for_an_unknown_topic():
    stack = GoalStack([explicit(SHADER_GOAL, "unity"), implicit("weather")])
    assert stack.find_suspended_explicit_goal_by_topic("travel") is None


def test_the_finder_returns_none_for_an_empty_topic():
    stack = GoalStack([explicit(SHADER_GOAL, "unity"), implicit("weather")])
    assert stack.find_suspended_explicit_goal_by_topic("") is None


def test_the_finder_handles_an_empty_stack():
    assert GoalStack().find_suspended_explicit_goal_by_topic("unity") is None


def test_the_finder_does_not_mutate():
    stack = GoalStack([explicit(SHADER_GOAL, "unity"), implicit("weather")])
    before = list(stack.stack)
    stack.find_suspended_explicit_goal_by_topic("unity")
    assert stack.stack == before


# ======================================================
# 7. resumed_to with a timestamp
# ======================================================
def test_resumed_to_refreshes_the_goal_when_given_a_time():
    target = explicit(SHADER_GOAL, "unity")
    stack = GoalStack([target, implicit("weather")])
    resumed = stack.resumed_to(target, 42.0)
    assert resumed.current.last_updated == 42.0
    assert resumed.current.continuity_source == SOURCE_RESUMPTION


def test_resumed_to_without_a_time_only_truncates():
    target = explicit(SHADER_GOAL, "unity")
    stack = GoalStack([target, implicit("weather")])
    assert stack.resumed_to(target).stack == [target]


def test_resumed_to_does_not_mutate_the_original_state():
    target = explicit(SHADER_GOAL, "unity")
    stack = GoalStack([target, implicit("weather")])
    stack.resumed_to(target, 42.0)
    assert target.last_updated == 0.0
    assert stack.stack[0] is target


# ======================================================
# 8. Determinism and immutability
# ======================================================
def test_topic_matching_resumption_is_deterministic():
    assert all(
        goal_after("anyway, the prefab is broken too").goal == SHADER_GOAL
        for _ in range(5)
    )


def test_the_same_history_gives_the_same_stack():
    history = detoured("anyway, the prefab is broken too")
    assert track_goals(history) == track_goals(history)


def test_the_input_stack_is_not_mutated():
    stack = GoalStack([explicit(SHADER_GOAL, "unity"), implicit("weather")])
    snapshot = list(stack.stack)
    update_goal_stack(stack, user("anyway, the prefab is broken too"), "unity")
    assert stack.stack == snapshot


def test_the_messages_are_not_mutated():
    history = [dict(message) for message in detoured("anyway, the prefab is broken too")]
    before = [dict(message) for message in history]
    track_goals(history)
    assert history == before


def test_resumption_makes_no_model_calls(monkeypatch):
    from backend.llm import semantic_embeddings

    def explode(*args, **kwargs):
        raise AssertionError("goal tracking must not call a model")

    monkeypatch.setattr(semantic_embeddings, "embed_texts_semantic", explode)
    monkeypatch.setattr(semantic_embeddings, "embed_text_semantic", explode)
    assert goal_after("anyway, the prefab is broken too").goal == SHADER_GOAL


# ======================================================
# 9. Routing follows the resumed goal
# ======================================================
def test_retrieval_routes_by_the_topic_matched_resumed_goal(db):
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    items = {i["id"]: i for i in aria_memory.search_hybrid(
        "prefab", min_score=0.0, conversation=detoured("anyway, the prefab is broken too")
    )}
    assert items[note_id]["keyword_score"] > 0.0


def test_routing_is_deterministic_under_topic_matched_resumption(db):
    semantic.index_note(notes_store.save_note("the gameobject was a reusable asset"))
    runs = [
        [i["keyword_score"] for i in aria_memory.search_hybrid(
            "prefab", min_score=0.0,
            conversation=detoured("anyway, the prefab is broken too"),
        )]
        for _ in range(3)
    ]
    assert all(run == runs[0] for run in runs)


# ======================================================
# 10. Nothing else moved
# ======================================================
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
    track_goals(detoured("anyway, the prefab is broken too"))
    after = aria_memory.search_hybrid("release pipeline", min_score=0.0)
    assert [i["keyword_score"] for i in before] == [i["keyword_score"] for i in after]
    assert [i["semantic_score"] for i in before] == [i["semantic_score"] for i in after]


def test_a_conversation_with_no_marker_is_untouched():
    history = [user("Help me fix the shader error"), user("why is it still failing")]
    current = track_goals(history).current
    assert current.goal == SHADER_GOAL
    assert current.continuity_source == "why is it still failing"
