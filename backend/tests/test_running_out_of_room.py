# backend/tests/test_running_out_of_room.py
#
# "create for me a player_inventory.cs file with a complete standard
# inventory system"
#
# nemo-12b generated for 64 seconds and stopped mid-string. The turn had
# 2048 tokens; the file needed more. An unterminated JSON string parses
# to nothing, so nothing was staged, nothing was created, and the reply
# was the raw action block followed by "I described that but did not
# produce a usable action, so nothing was staged."
#
# Three separate faults, and the model was not one of them:
#
#   the budget -- 2048 tokens for a turn whose whole purpose is writing
#   a file, on a model that holds 16384;
#
#   the display -- the failure path was the one route to the screen with
#   no action-block stripper on it, so the user was shown the machine
#   syntax that action_render exists to hide;
#
#   the words -- "did not produce a usable action" describes a model
#   that wandered off. This one did exactly what it was asked and was
#   cut off, which is a different failure with an obvious next step.

from __future__ import annotations

import pytest

from backend.core.action_plan import parse_actions, truncated_action
from backend.core.action_render import render_actions_for_reading
from backend.core.turn_orchestrator import _room_to_finish_the_file

QUOTE = '"'

BLOCK_HEAD = (
    "To create a player inventory system in C#, I'll propose an initial file "
    "structure for you. Please confirm if this meets your requirements.\n\n"
    "```json\n"
    '{"tool": "edit_file", "path": "player_inventory.cs", "content": "\n'
    "using System;\n"
    "\n"
    "public class PlayerInventory\n"
    "{\n"
    "    private Dictionary<string, int> _items;\n"
)

CUT_OFF = BLOCK_HEAD + "    public void AddItem(str"
COMPLETE = BLOCK_HEAD + "}\n" + QUOTE + "}\n```\n"


# --- the budget -------------------------------------------------------

def test_a_file_writing_turn_gets_more_room_than_a_chat_turn():
    """8192, raised from 4096 when the brief began asking for complete
    systems. The two have to move together: a brief demanding
    validation, persistence, hooks, documentation and tests, paid for
    out of a budget sized for a sketch, produces a file that stops in
    the middle."""
    assert _room_to_finish_the_file("tools", "nemo-12b-q5", 3000, 2048) == 8192
    assert _room_to_finish_the_file("chat", "nemo-12b-q5", 3000, 2048) == 2048


def test_the_budget_never_exceeds_what_the_model_can_hold():
    """mistral-7b holds 4096. Asking for more is an error at load time,
    not ambition."""
    budget = _room_to_finish_the_file("tools", "mistral-7b-q4km", 3000, 2048)

    assert 2048 <= budget < 4096


def test_the_budget_is_a_floor_and_never_lowers_a_request():
    # A caller that asked for more keeps it, even on a small window.
    assert _room_to_finish_the_file("tools", "mistral-7b-q4km", 12000, 2048) == 2048
    assert _room_to_finish_the_file("tools", "nemo-12b-q5", 3000, 8192) == 8192


def test_an_unknown_model_is_not_punished_for_being_unknown():
    """No window information is not evidence the window is small."""
    assert _room_to_finish_the_file("tools", "no-such-model", 3000, 2048) == 8192


# --- noticing it happened ---------------------------------------------

def test_an_unterminated_action_block_parses_to_nothing(cut_off=CUT_OFF):
    """The symptom, stated plainly, so the rest of this file has a subject."""
    assert parse_actions(cut_off) == []


def test_a_cut_off_block_is_recognised_as_cut_off():
    unfinished = truncated_action(CUT_OFF)

    assert unfinished["tool"] == "edit_file"
    assert unfinished["path"] == "player_inventory.cs"
    assert unfinished["lines"] > 0
    assert "class PlayerInventory" in unfinished["content"]


def test_a_complete_block_is_not_reported_as_truncated():
    assert parse_actions(COMPLETE), "this fixture is supposed to parse"
    assert truncated_action(COMPLETE) is None


def test_ordinary_prose_is_not_reported_as_truncated():
    assert truncated_action("Here is how an inventory system usually works.") is None
    assert truncated_action("") is None


# --- what the user reads ----------------------------------------------

def test_the_reply_says_it_ran_out_of_room():
    shown = render_actions_for_reading(CUT_OFF, expected_action=True)

    assert "ran out of room" in shown
    assert "`player_inventory.cs`" in shown
    assert "did not produce a usable action" not in shown


def test_the_raw_block_is_never_shown_when_it_was_cut_off():
    """The one path to the screen that had no stripper on it."""
    shown = render_actions_for_reading(CUT_OFF, expected_action=True)

    assert '"tool"' not in shown
    assert "```json" not in shown
    assert "using System;" not in shown


def test_the_model_s_own_sentence_survives():
    shown = render_actions_for_reading(CUT_OFF, expected_action=True)

    assert "player inventory system in C#" in shown


def test_a_described_but_unproduced_action_also_hides_its_block():
    """The other failure on the same path: a proposal with no usable
    action still had its raw JSON printed under the note."""
    described = (
        "I propose to create the file for you.\n\n"
        "```json\n"
        '{"tool": "nonsense_tool", "path": "x.py"}\n'
        "```\n"
    )
    shown = render_actions_for_reading(described, expected_action=True)

    assert "nonsense_tool" not in shown
    assert "```json" not in shown


@pytest.mark.parametrize("text", [COMPLETE, "Just a sentence about inventories."])
def test_nothing_changes_for_answers_that_were_fine(text):
    shown = render_actions_for_reading(text, expected_action=True)

    assert "ran out of room" not in shown


def test_the_prompt_size_reads_both_message_shapes():
    """provider_messages holds objects here and dicts in some callers.

    Reading one and assuming the other broke 132 tests a moment after it
    was written, all with the same AttributeError.
    """
    from backend.core.local_inference_engine import InferenceMessage
    from backend.core.turn_orchestrator import _prompt_size

    objects = [InferenceMessage(role="user", content="hello")]
    dicts = [{"role": "user", "content": "hello"}]

    assert _prompt_size(objects) == 5
    assert _prompt_size(dicts) == 5
    assert _prompt_size([]) == 0
    assert _prompt_size(None) == 0
    # Neither shape, and it must not raise.
    assert _prompt_size([object()]) == 0


# ======================================================
# The fault this file first blamed on the budget
# ======================================================
#
# The answer that prompted all of the above was NOT truncated. It was
# 1622 characters, complete, and closed its fence. It failed to parse
# because the model wrote a C# dictionary initializer into a JSON string
# without escaping its quotes:
#
#     _items = new Dictionary<string, int>
#     {
#         {"wood", 10},
#
# It escaped the quotes in throw new Exception(\"...\") and left these
# alone, so the JSON string ended at {" and json.loads stopped on the w.
#
# Any file containing a string literal hits this, which is most files.

UNESCAPED = (
    "Here you go.\n\n```json\n"
    '{"tool": "edit_file", "path": "player_inventory.cs", "content": "\n'
    "public class PlayerInventory\n"
    "{\n"
    "    public void Seed()\n"
    "    {\n"
    "        _items = new Dictionary<string, int>\n"
    "        {\n"
    '            {"wood", 10},\n'
    '            {"stone", 10}\n'
    "        };\n"
    "    }\n"
    "}\n"
    '"}\n'
    "```\n"
)


def test_unescaped_quotes_in_the_content_still_produce_the_action():
    actions = parse_actions(UNESCAPED)

    assert len(actions) == 1
    assert actions[0].args["path"] == "player_inventory.cs"


def test_the_recovered_content_keeps_its_quotes_exactly():
    content = parse_actions(UNESCAPED)[0].args["content"]

    assert '{"wood", 10},' in content
    assert '{"stone", 10}' in content
    assert "public class PlayerInventory" in content
    # And nothing of the JSON wrapper leaked in.
    assert '"tool"' not in content
    assert not content.rstrip().endswith('"}')


def test_the_escapes_the_model_did_write_are_applied():
    """A salvage that left backslashes in would write them to the file."""
    block = (
        "```json\n"
        '{"tool": "edit_file", "path": "a.cs", "content": "'
        'say(\\"hi\\");\nnext();"}\n'
        "```\n"
    )
    content = parse_actions(block)[0].args["content"]

    assert content == 'say("hi");\nnext();'


def test_an_unknown_escape_survives_rather_than_failing_the_turn():
    """A Windows path in the content is far likelier than a real mistake.

    json.loads refuses an unknown escape outright. Here it is almost
    always a path or a regex the model wrote into the file, so it passes
    through as written rather than costing the user the turn.
    """
    BS = chr(92)
    body = 'p = ' + BS + '"C:' + BS + 'Users' + BS + 'wfk10' + BS + '"'
    block = ("```json\n"
             '{"tool": "edit_file", "path": "a.py", "content": "' + body + '"}\n'
             "```\n")

    actions = parse_actions(block)

    assert len(actions) == 1
    content = actions[0].args["content"]
    assert "Users" in content
    # The escaped quotes became quotes; the path separators survived.
    assert content.startswith('p = "C:')


def test_two_actions_are_never_salvaged_by_guessing():
    """Two actions have two places the content could end, and guessing
    between them is how a salvage becomes a corruption."""
    from backend.core.action_plan import _salvage_one_action

    payload = ('[{"tool": "edit_file", "path": "a.py", "content": "x"},'
               ' {"tool": "edit_file", "path": "b.py", "content": "y"}]')

    assert _salvage_one_action(payload) is None


def test_a_complete_block_is_never_reported_as_running_out_of_room():
    """The wrong diagnosis this file was written around.

    A confident wrong explanation is worse than the vague right one it
    replaced.
    """
    assert truncated_action(UNESCAPED) is None

    shown = render_actions_for_reading(UNESCAPED, expected_action=True)
    assert "ran out of room" not in shown


# ======================================================
# A fence the model opened and never closed
# ======================================================
#
# From the same session. The payload was stripped, the ```json marker was
# not, and the UI rendered everything after it as a code block -- so a
# correct reply appeared inside a fence:
#
#     To create a basic player inventory system, I propose the
#     following content for player_inventory.cs:
#     ```json
#
#     Done:
#     - created `player_inventory.cs` (44 lines)
#     I ran 8 related suites (21s) and they pass.
#
# _JSON_FENCE needs both markers to match and strip_action_json removes
# the object rather than the fence around it, so an unclosed fence fell
# between the two.

PROSE = "To create a basic player inventory system, I propose the following content:"
PAYLOAD = '{"tool": "edit_file", "path": "player_inventory.cs", "content": "class X {}"}'


def test_an_unclosed_action_fence_is_not_left_on_screen():
    raw = PROSE + "\n\n```json\n" + PAYLOAD + "\n"

    shown = render_actions_for_reading(raw, expected_action=True,
                                       created=["player_inventory.cs"])

    assert "```json" not in shown
    assert "Done:" in shown
    assert PROSE in shown


def test_a_closed_action_fence_is_still_removed():
    raw = PROSE + "\n\n```json\n" + PAYLOAD + "\n```\n"

    shown = render_actions_for_reading(raw, expected_action=True,
                                       created=["player_inventory.cs"])

    assert "```" not in shown


def test_the_users_own_code_block_is_never_touched():
    """The json tag is what makes the removal safe. A ```csharp block is
    the code being discussed, however the model punctuated it."""
    raw = (PROSE + "\n\n```json\n" + PAYLOAD + "\n```\n\n"
           "For example:\n```csharp\nvar x = 1;\n")

    shown = render_actions_for_reading(raw, expected_action=True,
                                       created=["player_inventory.cs"])

    assert "```csharp" in shown
    assert "var x = 1;" in shown
    assert "```json" not in shown


# ======================================================
# An answer that hands the work back
# ======================================================

MANUAL_INSTRUCTIONS = (
    'First, let\'s create a new Unity project. Go to the Unity Editor and '
    'click on "Create". You can do this by right-clicking on the project '
    'folder. Then in the new file, add the following code:\n\n'
    '```json\n{ "PlayerInventory": { "Items": [{"Name": "Item1"}] } }'
)


def test_an_answer_that_tells_the_user_to_do_it_says_nothing_was_staged():
    """It reads like help and it did nothing. Measured on nemo-12b."""
    shown = render_actions_for_reading(MANUAL_INSTRUCTIONS, expected_action=True)

    assert "did not produce a usable action" in shown


def test_a_dangling_fence_is_stripped_even_from_an_answer_with_no_action():
    """This used to be inside the proposal check, so an answer that
    produced no action AND did not sound like a proposal kept its raw
    block."""
    shown = render_actions_for_reading(MANUAL_INSTRUCTIONS, expected_action=True)

    assert "```json" not in shown
    # And the model's own words survive.
    assert "Unity Editor" in shown


def test_an_ordinary_answer_with_no_action_is_still_left_alone():
    plain = "The inventory keeps items in a dictionary keyed by name."
    assert render_actions_for_reading(plain, expected_action=True) == plain
