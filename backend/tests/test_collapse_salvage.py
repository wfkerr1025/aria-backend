# backend/tests/test_collapse_salvage.py
#
# When a model stops writing and starts looping.
#
# Measured live on nemo-12b, asked for a Unity inventory. It produced
# usable action blocks and then this, for the rest of the turn:
#
#     assistant.
#     user interface. user.assistant.
#     assistant.assistant.assistant.assistant.assistant.
#
# A collapse cannot be a terminator. AnswerStream's whole table is
# prefixes ending in a colon, and "assistant." is neither a prefix nor
# punctuated like one. It is a SHAPE -- a short unit repeated until the
# budget runs out -- so a shape is what this looks for.
#
# The risk runs the other way. Real code repeats short lines, and a rule
# that cut those would corrupt files to tidy up a symptom. Every test
# below that asserts "untouched" is guarding against that, and they
# outnumber the ones asserting it fires.

from __future__ import annotations

import pytest

from backend.core.action_plan import collapse_point, parse_actions, without_collapse
from backend.core.action_render import render_actions_for_reading


THE_COLLAPSE = ("assistant.\n\nuser interface. user.assistant.\n\n"
                "assistant.assistant.assistant.assistant.assistant.assistant.")


# --- it fires on a real collapse --------------------------------------

def test_the_reported_collapse_is_found():
    """The contiguous loop goes. The ragged edge before it does not.

    Degeneration on this model starts scattered -- "assistant." on its
    own line, then "user interface. user.assistant." -- and only later
    becomes a clean repeat. This cuts the clean repeat, which is the
    part that can be recognised without guessing.

    Widening it to catch the ragged part would mean deciding some prose
    is too repetitive to keep, and the cost of being wrong there is a
    sentence deleted from a user's answer. The loop is both the part
    worth removing and the part that can be removed safely.
    """
    answer = "Here is the inventory script.\n\n" + THE_COLLAPSE
    trimmed = without_collapse(answer)

    assert "assistant.assistant" not in trimmed
    assert trimmed.startswith("Here is the inventory script.")
    assert len(trimmed) < len(answer)


def test_the_loop_never_reaches_the_screen():
    answer = "Here is the inventory script.\n\n" + THE_COLLAPSE
    shown = render_actions_for_reading(answer, expected_action=False)

    assert "assistant.assistant" not in shown
    assert "Here is the inventory script." in shown


def test_a_loop_inside_a_files_contents_is_not_written_to_disk():
    """The salvage reads to the end of the fence. If the model looped
    inside the string, the loop is not part of the file."""
    block = ('```json\n'
             '{"tool": "edit_file", "path": "a.cs", "content": "class A { }\n'
             + "loop.loop.loop.loop.loop.loop.loop." + '\n```\n')
    actions = parse_actions(block)

    assert len(actions) == 1
    content = actions[0].args["content"]
    assert "class A" in content
    assert "loop.loop" not in content


# --- and not on anything else -----------------------------------------

@pytest.mark.parametrize("text", [
    "public class Player\n{\n    void A() { }\n    void B() { }\n}\n",
    "if (a)\n{\n    if (b)\n    {\n    }\n}\n}\n}\n}\n}\n",
    "x = 1;\nx = 1;\nx = 1;\nx = 1;\nx = 1;\nx = 1;\n",
    "The quick brown fox jumps over the lazy dog and keeps going.",
    "a\n\n\n\n\n\n\n\n",
    "----------------------------------------",
    "",
    "short",
])
def test_ordinary_text_is_left_exactly_alone(text):
    assert collapse_point(text) is None
    assert without_collapse(text) == text


def test_a_repeated_unit_without_a_letter_is_not_a_collapse():
    """Punctuation runs are ordinary: a rule of dashes, a row of dots."""
    assert collapse_point("=" * 60) is None
    assert collapse_point("......................................") is None


def test_it_takes_six_repeats_and_not_three():
    """Three of anything is a pattern a person might write."""
    assert collapse_point("done. done. done.") is None
