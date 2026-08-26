# backend/tests/test_phase7_resumption.py
#
# Resumption marker handling -- the Phase 7.2 refinement.
#
# The bug this fixes: "anyway, why is it still failing" after a detour
# resolved to the detour's goal, because the message was read as continuing
# whatever came immediately before -- which was exactly the thing the user
# had just signalled they were done with.
#
# Most of this file is the guard rails rather than the fix. Resumption is a
# rule that reaches backwards past the most recent turn, which is a powerful
# and easily-wrong thing to do, so the cases where it must NOT fire are
# tested more heavily than the case where it must.
#
# No model, no clock, no database.

from __future__ import annotations

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory
from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.aria_memory import memory_ranking
from backend.context.continuity_markers import (
    IMMEDIATE_MARKERS,
    detect_continuity,
    points_at_immediate_context,
)
from backend.context.goal_state import (
    SOURCE_EXPLICIT,
    SOURCE_RESUMPTION,
    GoalStack,
    GoalState,
)
from backend.context.goal_tracking import current_goal_of, track_goals, update_goal_stack
from backend.context.resumption_markers import MARKERS, detect_resumption
from backend.files import file_ingestion as ingestion

SHADER_GOAL = "fix the shader error"
API_GOAL = "build the api"


def user(text: str, **extra) -> dict:
    return {"role": "user", "content": text, **extra}


def assistant(text: str, **extra) -> dict:
    return {"role": "assistant", "content": text, **extra}


def detoured(tail: str) -> list[dict]:
    """A stated goal, an answer, a weather detour, then `tail`."""
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
# 0. The reported bug
# ======================================================
def test_the_reported_case_now_resumes():
    # "anyway, why is it still failing" used to resolve to the weather goal.
    resumed = goal_after("anyway, why is it still failing")
    assert resumed.goal == SHADER_GOAL
    assert resumed.topic == "unity"
    assert resumed.continuity_source == SOURCE_RESUMPTION


# ======================================================
# 1. Marker detection
# ======================================================
@pytest.mark.parametrize("text,marker", [
    ("anyway", "anyway"),
    ("anyway, where were we", "where were we"),
    ("back to that", "back to that"),
    ("back to this", "back to this"),
    ("as I was saying", "as i was saying"),
    ("returning to that", "returning to that"),
    ("returning to this", "returning to this"),
])
def test_each_resumption_marker_is_detected(text, marker):
    assert detect_resumption(text) == marker


def test_the_longest_marker_wins():
    assert detect_resumption("as I was saying earlier") == "as i was saying"


@pytest.mark.parametrize("text", [
    "continue",
    "why is it still failing",
    "help me fix the shader",
    "",
])
def test_text_with_no_resumption_marker_returns_none(text):
    assert detect_resumption(text) is None


def test_a_marker_does_not_fire_inside_a_longer_word():
    assert detect_resumption("the runaway process") is None
    assert detect_resumption("anyways") is None


def test_marker_detection_is_case_insensitive():
    assert detect_resumption("ANYWAY") == "anyway"


def test_punctuation_does_not_hide_a_marker():
    assert detect_resumption("anyway, back to it") == "anyway"


def test_marker_detection_is_deterministic():
    assert all(detect_resumption("anyway") == "anyway" for _ in range(5))


def test_every_listed_marker_detects_itself():
    for marker in MARKERS:
        assert detect_resumption(marker) is not None


def test_detection_makes_no_model_calls(monkeypatch):
    from backend.llm import semantic_embeddings

    def explode(*args, **kwargs):
        raise AssertionError("resumption detection must not call a model")

    monkeypatch.setattr(semantic_embeddings, "embed_texts_semantic", explode)
    monkeypatch.setattr(semantic_embeddings, "embed_text_semantic", explode)
    assert detect_resumption("anyway") == "anyway"


# ======================================================
# 2. GoalStack.resume()
# ======================================================
def explicit(goal: str, topic: str) -> GoalState:
    return GoalState(goal=goal, topic=topic, explicit=True)


def implicit(topic: str) -> GoalState:
    return GoalState(goal=topic, topic=topic, explicit=False)


def test_resume_finds_the_explicit_goal_under_a_detour():
    stack = GoalStack([explicit(SHADER_GOAL, "unity"), implicit("weather")])
    assert stack.resume().goal == SHADER_GOAL


def test_resume_ignores_the_top_of_the_stack():
    # The top is what is being left, not what is being returned to.
    stack = GoalStack([explicit(SHADER_GOAL, "unity"), explicit(API_GOAL, "backend")])
    assert stack.resume().goal == SHADER_GOAL


def test_resume_takes_the_nearest_explicit_goal():
    stack = GoalStack([
        explicit("migrate the database", "backend"),
        explicit(SHADER_GOAL, "unity"),
        implicit("weather"),
    ])
    assert stack.resume().goal == SHADER_GOAL


def test_resume_skips_implicit_goals():
    stack = GoalStack([explicit(SHADER_GOAL, "unity"), implicit("weather"), implicit("travel")])
    assert stack.resume().goal == SHADER_GOAL


def test_resume_returns_none_with_no_explicit_goal_below():
    assert GoalStack([implicit("weather")]).resume() is None
    assert GoalStack([explicit(SHADER_GOAL, "unity")]).resume() is None
    assert GoalStack().resume() is None


def test_resume_does_not_mutate():
    state = explicit(SHADER_GOAL, "unity")
    stack = GoalStack([state, implicit("weather")])
    before = list(stack.stack)
    resumed = stack.resume()
    assert stack.stack == before
    assert resumed is state


def test_resumed_to_drops_everything_above():
    target = explicit(SHADER_GOAL, "unity")
    stack = GoalStack([target, implicit("weather"), implicit("travel")])
    assert stack.resumed_to(target).stack == [target]


def test_resumed_to_keeps_explicit_goals_below():
    lower = explicit("migrate the database", "backend")
    target = explicit(SHADER_GOAL, "unity")
    stack = GoalStack([lower, target, implicit("weather")])
    assert stack.resumed_to(target).stack == [lower, target]


def test_resumed_to_is_a_no_op_for_an_unknown_state():
    stack = GoalStack([explicit(SHADER_GOAL, "unity")])
    assert stack.resumed_to(explicit("something else", "unity")).stack == stack.stack


# ======================================================
# 3. Basic resumption through the tracker
# ======================================================
@pytest.mark.parametrize("tail", [
    "anyway",
    "anyway, why is it still failing",
    "back to that",
    "as I was saying",
    "returning to that",
])
def test_a_topicless_resumption_returns_to_the_stated_goal(tail):
    assert goal_after(tail).goal == SHADER_GOAL


def test_the_resumed_goal_keeps_its_own_topic():
    assert goal_after("anyway").topic == "unity"


def test_the_resumed_goal_is_marked_as_resumed():
    assert goal_after("anyway").continuity_source == SOURCE_RESUMPTION


def test_the_resumed_goal_is_still_explicit():
    assert goal_after("anyway").explicit


def test_the_detour_goal_is_dropped_from_the_stack():
    stack = track_goals(detoured("anyway"))
    assert stack.goals == [SHADER_GOAL]


def test_the_resumed_goal_is_refreshed_not_duplicated():
    stack = track_goals(detoured("anyway"))
    assert len(stack) == 1


# ======================================================
# 4. Messages that name a topic
# ======================================================
def test_a_resumption_marker_with_a_topic_is_a_topic_switch():
    # "anyway, what's the forecast" is changing the subject, not returning
    # to one. The marker is present; the topic is what decides.
    resumed = goal_after("anyway, what is the forecast tomorrow")
    assert resumed.goal != SHADER_GOAL
    assert resumed.topic == "weather"


def test_a_resumption_marker_naming_another_topic_does_not_resume():
    history = [
        user("Help me fix the shader error"),
        user("what is the forecast tomorrow"),
        user("anyway, the routing provider fell back"),
    ]
    assert track_goals(history).current.topic == "backend"


def test_naming_the_suspended_goals_own_topic_resumes_it():
    # The gate was widened after this case was pinned the other way: a
    # message naming the topic stated work was left on is a return to that
    # work, not a fresh subject. See test_phase7_resumption_topic_match.py
    # for the full behaviour.
    history = [
        user("Help me fix the shader error"),
        user("what is the forecast tomorrow"),
        user("anyway, the prefab is broken too"),
    ]
    current = track_goals(history).current
    assert current.goal == SHADER_GOAL
    assert current.topic == "unity"
    assert current.continuity_source == SOURCE_RESUMPTION


# ======================================================
# 5. Multiple implicit goals
# ======================================================
def test_several_implicit_detours_are_all_left_behind():
    history = [
        user("Help me fix the shader error"),
        user("what is the forecast tomorrow"),
        user("book a flight to Oslo"),
        user("anyway"),
    ]
    stack = track_goals(history)
    assert stack.current.goal == SHADER_GOAL
    assert stack.goals == [SHADER_GOAL]


def test_the_correct_explicit_goal_is_resumed_from_several():
    history = [
        user("Help me migrate the database"),
        user("Help me fix the shader error"),
        user("what is the forecast tomorrow"),
        user("anyway"),
    ]
    stack = track_goals(history)
    assert stack.current.goal == SHADER_GOAL
    # The older explicit goal is still suspended, not discarded.
    assert "migrate the database" in stack.goals


# ======================================================
# 6. No explicit goal to return to
# ======================================================
def test_a_resumption_marker_with_no_explicit_goal_is_a_no_op():
    history = [user("my prefab keeps breaking"), user("what is the forecast"), user("anyway")]
    before = track_goals(history[:-1])
    after = track_goals(history)
    assert after.current.goal == before.current.goal


def test_a_resumption_marker_on_an_empty_history_creates_nothing():
    assert len(track_goals([user("anyway")])) == 0


def test_a_resumption_marker_with_only_one_goal_leaves_it_alone():
    history = [user("Help me fix the shader error"), user("anyway")]
    stack = track_goals(history)
    assert stack.current.goal == SHADER_GOAL
    assert len(stack) == 1


# ======================================================
# 7. Interaction with continuity markers
# ======================================================
def test_continue_wins_over_resumption():
    resumed = goal_after("anyway, continue")
    assert resumed.goal != SHADER_GOAL
    assert resumed.continuity_source == "continue"


def test_fix_this_wins_over_resumption():
    resumed = goal_after("anyway, fix this")
    assert resumed.goal != SHADER_GOAL
    assert resumed.continuity_source == "fix this"


@pytest.mark.parametrize("marker", sorted(IMMEDIATE_MARKERS))
def test_every_immediate_marker_wins_over_resumption(marker):
    resumed = goal_after(f"anyway, {marker}")
    assert resumed.continuity_source != SOURCE_RESUMPTION


def test_an_anaphoric_question_does_not_block_resumption():
    # "why is it still failing" says the user is not starting something new
    # and nothing about which work they mean, so "anyway" answers that.
    assert not points_at_immediate_context(detect_continuity("why is it still failing"))
    assert goal_after("anyway, why is it still failing").goal == SHADER_GOAL


def test_a_continuity_marker_without_a_resumption_marker_is_unchanged():
    # The pre-existing behaviour: no "anyway", no reaching backwards.
    assert goal_after("why is it still failing").goal != SHADER_GOAL


# ======================================================
# 8. Interaction with explicit new goals
# ======================================================
def test_an_explicit_goal_in_the_same_turn_wins():
    resumed = goal_after("anyway, help me build the api")
    assert resumed.goal == API_GOAL
    assert resumed.continuity_source == SOURCE_EXPLICIT


def test_a_resumption_marker_is_stripped_from_a_stated_goal():
    from backend.context.goal_classifier import classify_goal

    assert classify_goal("anyway, help me build the api") == API_GOAL
    assert classify_goal("anyway, fix the shader error") == SHADER_GOAL


def test_an_explicit_goal_after_a_resumption_marker_pushes():
    stack = track_goals(detoured("anyway, help me build the api"))
    assert API_GOAL in stack.goals
    assert SHADER_GOAL in stack.goals


def test_a_bare_marker_is_still_not_a_goal():
    from backend.context.goal_classifier import classify_goal

    assert classify_goal("anyway") is None
    assert classify_goal("as I was saying") is None


# ======================================================
# 9. Assistant messages
# ======================================================
def test_an_assistant_resumption_marker_changes_nothing():
    history = detoured("rain tomorrow")[:-1] + [assistant("anyway, back to the shader")]
    assert track_goals(history).current.goal != SHADER_GOAL


# ======================================================
# 10. Immutability and determinism
# ======================================================
def test_the_input_stack_is_not_mutated():
    stack = track_goals(detoured("what is the forecast"))
    snapshot = list(stack.stack)
    update_goal_stack(stack, user("anyway"), "general")
    assert stack.stack == snapshot


def test_the_resumed_state_is_a_copy():
    stack = GoalStack([explicit(SHADER_GOAL, "unity"), implicit("weather")])
    original = stack.stack[0]
    updated = update_goal_stack(stack, user("anyway"), "general", at=99.0)
    assert updated.current.last_updated == 99.0
    assert original.last_updated == 0.0


def test_the_messages_are_not_mutated():
    history = [dict(message) for message in detoured("anyway")]
    before = [dict(message) for message in history]
    track_goals(history)
    assert history == before


def test_resumption_is_deterministic():
    assert all(goal_after("anyway").goal == SHADER_GOAL for _ in range(5))


def test_the_same_history_gives_the_same_stack():
    assert track_goals(detoured("anyway")) == track_goals(detoured("anyway"))


# ======================================================
# 11. Routing follows the resumed goal
# ======================================================
def test_retrieval_routes_by_the_resumed_goal(db):
    # "prefab" only expands toward "gameobject" in the Unity domain, so this
    # note earns keyword credit only if the resumed goal put the search back
    # on Unity rather than leaving it on the weather detour.
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    items = {i["id"]: i for i in aria_memory.search_hybrid(
        "prefab", min_score=0.0, conversation=detoured("anyway, why is it still failing")
    )}
    assert items[note_id]["keyword_score"] > 0.0


def test_retrieval_stays_on_the_detour_without_a_resumption_marker(db):
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    items = {i["id"]: i for i in aria_memory.search_hybrid(
        "prefab", min_score=0.0, conversation=detoured("why is it still failing")
    )}
    assert items.get(note_id, {}).get("keyword_score", 0.0) == 0.0


def test_routing_is_deterministic_under_resumption(db):
    semantic.index_note(notes_store.save_note("the gameobject was a reusable asset"))
    runs = [
        [i["keyword_score"] for i in aria_memory.search_hybrid(
            "prefab", min_score=0.0, conversation=detoured("anyway")
        )]
        for _ in range(3)
    ]
    assert all(run == runs[0] for run in runs)


# ======================================================
# 12. Nothing else moved
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
    track_goals(detoured("anyway"))
    after = aria_memory.search_hybrid("release pipeline", min_score=0.0)
    assert [i["keyword_score"] for i in before] == [i["keyword_score"] for i in after]
    assert [i["semantic_score"] for i in before] == [i["semantic_score"] for i in after]


def test_a_history_with_no_resumption_marker_is_untouched():
    # The regression guard: everything Phase 7.2 did before must still hold
    # for any conversation that never says "anyway".
    history = [user("Help me fix the shader error"), user("why is it still failing")]
    assert current_goal_of(history).goal == SHADER_GOAL
    assert current_goal_of(history).continuity_source == "why is it still failing"
