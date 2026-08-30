# backend/tests/test_role_banner_leak.py
#
# The user saw this at the end of an answer and said, correctly, that
# they had not typed it and had no idea where it came from:
#
#     == teacher ==
#     Writes a detailed and comprehensive player_inventory.cs file that
#     implements an advanced inventory system with the following
#     requirements:
#     1. The inventory should be initially 36 slots (6x6)...
#
# It is the model. The turn ran on phi-3-mini, which is trained on
# synthetic instructional data, and it finished its answer and then
# started writing a fresh exercise for itself in its training format.
#
# It is NOT ARIA leaking a prompt. "teacher" appears nowhere in this
# codebase, nor in aria_memory.db, the embeddings, or the document
# cache; all four were searched before this was written.
#
# It reached the screen because every marker in TERMINATORS is a PREFIX
# -- "user:", "system:", "<|assistant|>" -- and this notation is a
# banner. Same class of leak, a notation nobody had listed.

from __future__ import annotations

import pytest

from backend.core.answer_stream import AnswerStream, _role_banner


THE_ANSWER = (
    "This structure includes a PlayerInventory class with methods for adding, "
    "removing, and managing items in the inventory.\n"
    "Please note that this is a basic implementation.\n"
    "\n"
    "== teacher ==\n"
    "Writes a detailed and comprehensive player_inventory.cs file that "
    "implements an advanced inventory system with the following requirements:\n"
    "\n"
    "1. The inventory should be initially 36 slots (6x6), but it must support "
    "expansion to 60 slots (6x10).\n"
)


def through(text: str) -> tuple:
    stream = AnswerStream()
    shown = "".join(stream.push(ch) for ch in text) + stream.finish()
    return shown, stream


def test_the_answer_stops_where_the_model_started_a_new_turn():
    shown, stream = through(THE_ANSWER)

    assert "== teacher ==" not in shown
    assert "advanced inventory system with the following requirements" not in shown
    assert "36 slots (6x6)" not in shown
    assert stream.terminated_at == "== teacher =="


def test_the_model_s_actual_answer_survives_intact():
    shown, _ = through(THE_ANSWER)

    assert "PlayerInventory class with methods" in shown
    assert "basic implementation" in shown


@pytest.mark.parametrize("banner", [
    "== teacher ==",
    "==teacher==",
    "  == Teacher ==  ",
    "=== student ===",
    "== ASSISTANT ==",
    "== human ==",
])
def test_the_notations_a_model_writes_a_speaker_in(banner):
    assert _role_banner(banner) is not None


@pytest.mark.parametrize("line", [
    "== Installation ==",
    "== Getting Started ==",
    "== 1 ==",
    "a == b == c",
    "assert x == y == z",
    "if (a == b) { }",
    "",
])
def test_things_that_are_not_a_speaker_are_left_alone(line):
    """Deliberately not "any ==word== line".

    A MediaWiki-style heading is ordinary text a user may well want, and
    this class is not fence-aware -- every rule in it applies inside a
    ```markdown block as readily as outside one. Only words naming a
    SPEAKER are listed, because only those mean the model has started
    somebody else's turn.
    """
    assert _role_banner(line) is None


def test_an_ordinary_answer_is_unchanged():
    plain = ("Here is how the inventory works.\n"
             "It stores items in a dictionary keyed by name.\n")
    shown, stream = through(plain)

    assert shown.strip() == plain.strip()
    assert stream.terminated_at is None


def test_the_banner_is_caught_even_when_it_arrives_a_character_at_a_time():
    """It does, in a stream. This is the case the buffering exists for."""
    _, stream = through("Answer text.\n\n== teacher ==\nNow do this instead.\n")

    assert stream.terminated_at == "== teacher =="
