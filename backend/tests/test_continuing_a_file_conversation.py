# backend/tests/test_continuing_a_file_conversation.py
#
# Two turns, one minute apart, from a live session:
#
#   "create a player_inventory.cs file that has a complete standard
#    inventory system that I can use"
#       -> classified tools. nemo-12b. Created 44 lines. Tests ran.
#
#   "ok, I need you to add some things to the inventory. First the
#    inventory needs to be 36 slots (6x6) with the capability to expand
#    to 60 slots (6x10)... I also need the inventory to be stackable up
#    to 99 per slot. The Inventory should be drag n drop as well."
#       -> classified CHAT. phi-3-mini. Wrote a ```csharp listing into
#          the chat window. Changed nothing.
#
# The second message names no file, so nothing in the router saw file
# work: no tool floor fired, a model that cannot emit actions took the
# turn, and the user was handed code to copy by hand. That is the exact
# failure this whole subsystem exists to prevent, arriving through the
# one door nobody had shut.
#
# A person reading those two messages together has no doubt what the
# second one means. The only reason ARIA did is that it read one message
# at a time.

from __future__ import annotations

import pytest

from backend.chat.model_router import TURN_CHAT, TURN_TOOLS, classify_turn


def turn(text, messages=()):
    class _Request:
        latest_user_text = text

    _Request.messages = list(messages)
    return _Request()


AFTER_CREATING = [
    {"role": "user",
     "content": "create a player_inventory.cs file with an inventory system"},
    {"role": "assistant",
     "content": "Done:\n\n- created `player_inventory.cs` (44 lines)"},
]

THE_FOLLOW_UP = (
    "ok, I need you to add some things to the inventory.  First the inventory "
    "needs to be 36 slots (6x6) with the capability to expand to 60 slots "
    "(6x10) when the player gets upgrades.  I also need the inventory to be "
    "stackable up to 99 per slot.  The Inventory should be drag n drop as well."
)


def test_the_follow_up_that_did_nothing_is_now_file_work():
    assert classify_turn(turn(THE_FOLLOW_UP, AFTER_CREATING)) == TURN_TOOLS


def test_the_same_words_with_no_file_in_the_conversation_stay_chat():
    """Talking about inventories is not file work. Having just written
    player_inventory.cs is."""
    assert classify_turn(turn(THE_FOLLOW_UP, [])) == TURN_CHAT


@pytest.mark.parametrize("text", [
    "how does the inventory work?",
    "what do you think of that design",
    "why did you use a Dictionary there",
])
def test_a_question_about_the_file_is_still_a_question(text):
    """The turn has to READ like a change. Asking about the file is not
    asking for one."""
    assert classify_turn(turn(text, AFTER_CREATING)) == TURN_CHAT


@pytest.mark.parametrize("text", [
    "thanks, that looks good",
    "great, that is exactly what I wanted",
])
def test_ordinary_conversation_after_a_file_turn_is_not_file_work(text):
    assert classify_turn(turn(text, AFTER_CREATING)) == TURN_CHAT


@pytest.mark.parametrize("text", [
    "add a sort method to it",
    "I need it to support 60 slots",
    "make it stackable up to 99",
    "remove the placeholder comments",
    "it should have a save and load system",
])
def test_the_ordinary_ways_people_ask_for_a_change(text):
    assert classify_turn(turn(text, AFTER_CREATING)) == TURN_TOOLS


def test_a_file_mentioned_long_ago_does_not_make_everything_file_work():
    """A conversation that has moved on has moved on."""
    stale = AFTER_CREATING + [
        {"role": "user", "content": "anyway, what is the weather like"},
        {"role": "assistant", "content": "It is sunny."},
        {"role": "user", "content": "and tomorrow?"},
        {"role": "assistant", "content": "Rain in the afternoon."},
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": "Any time."},
    ]

    assert classify_turn(turn("I need it to be warmer", stale)) == TURN_CHAT


def test_history_may_be_objects_as_well_as_dicts():
    """Both shapes reach this layer; reading one and assuming the other
    is a mistake this codebase has already made once."""
    class Message:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    history = [Message("assistant", "Done:\n\n- created `player_inventory.cs`")]

    assert classify_turn(turn("add a sort method", history)) == TURN_TOOLS


def test_classification_does_not_recurse_into_the_action_planner():
    """action_plan imports this classifier. A top-level import back closed
    the loop once and turned a 96-second suite into a hang."""
    import backend.chat.model_router as router

    source = open(router.__file__, encoding="utf-8").read()
    top_level = [line for line in source.splitlines()
                 if line.startswith("from backend.core.action_plan")
                 or line.startswith("import backend.core.action_plan")]

    assert top_level == [], f"action_plan must be imported lazily, found: {top_level}"
