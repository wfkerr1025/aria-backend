# backend/tests/test_supervisor_layer.py
#
# Checking a heavier model's answer before it is acted on.
#
# The interesting half of this file is not "does it clean the mess" --
# that is string handling and it either works or it does not. It is the
# half about what the supervisor is NOT allowed to do.
#
# A 3.8B asked to tidy a 12B's answer will sometimes summarise it
# instead, drop a code block, or quietly answer the question itself. Any
# of those is a worse outcome than the untidiness being fixed, and none
# of them looks like a failure from the outside -- the reply is fluent
# and shorter, which reads as improvement. So the rewrite is accepted
# only if it survives a set of guards, and those guards get as many
# tests as the cleaning does.
#
# The other reason the deterministic stage exists at all: leakage,
# duplication and malformed JSON are exactly-solvable in code. Handing
# them to a model would introduce the exact failure class the layer is
# supposed to remove, while looking like more care was taken.

from __future__ import annotations

import json

import pytest

from backend.chat import supervisor_layer as sup
from backend.chat.supervisor_layer import repair_deterministically, supervise_chat_output


# ======================================================
# Leaked scaffolding
# ======================================================
@pytest.mark.parametrize("marker", [
    "<|im_start|>", "<|im_end|>", "<|system|>", "<|assistant|>",
    "[INST]", "[/INST]", "<<SYS>>", "### System:",
])
def test_chat_template_markers_are_removed(marker):
    result = repair_deterministically(f"{marker}\nThe answer is 42.")

    assert marker not in result.text
    assert "The answer is 42." in result.text


def test_an_echoed_system_turn_is_removed():
    raw = "System: You are ARIA, a helpful assistant.\nThe build runs in two stages."

    result = repair_deterministically(raw)

    assert "You are ARIA" not in result.text
    assert "The build runs in two stages." in result.text
    assert "removed system prompt leakage" in result.repairs


def test_the_residue_a_marker_leaves_behind_goes_too():
    # "<|im_start|>system" loses its marker and leaves the bare word on a
    # line of its own, which reads as a stray heading. The leak is gone
    # and its shadow is still there.
    result = repair_deterministically("<|im_start|>system\nHere is the answer.")

    assert result.text.strip() == "Here is the answer."


def test_a_paragraph_that_merely_mentions_the_word_survives():
    # "system:" as a label in prose is not leakage. Deleting a line
    # because it contains a word is how a cleaner starts eating answers.
    raw = "The build system: a short description follows.\nIt compiles shaders."

    result = repair_deterministically(raw)

    assert "The build system: a short description follows." in result.text


def test_leakage_inside_a_code_block_is_left_alone():
    # A code block legitimately contains things that look like leakage --
    # `[INST]` in a string, `system:` in a YAML sample. Editing inside
    # one corrupts the answer in order to tidy it.
    raw = "Here is the config:\n\n```yaml\nsystem:\n  name: aria\n```\n\nThat is all."

    result = repair_deterministically(raw)

    assert "system:\n  name: aria" in result.text


# ======================================================
# Repetition
# ======================================================
def test_a_repeated_paragraph_is_dropped_once():
    paragraph = "The pipeline compiles shaders first, then links them into a bundle."
    raw = f"{paragraph}\n\n{paragraph}\n\nThen it runs the tests."

    result = repair_deterministically(raw)

    assert result.text.count(paragraph) == 1
    assert "Then it runs the tests." in result.text
    assert any("repeated paragraph" in repair for repair in result.repairs)


def test_repetition_is_matched_past_whitespace_differences():
    a = "The pipeline compiles shaders first, then links them into a bundle."
    b = "The  pipeline compiles shaders first,  then links them into a bundle."

    result = repair_deterministically(f"{a}\n\n{b}")

    assert result.text.count("compiles shaders") == 1


def test_a_short_repeated_line_is_kept():
    # A heading, a label, a closing line. Removing these damages the
    # answer to fix nothing.
    raw = "Done.\n\nFirst step here.\n\nDone."

    result = repair_deterministically(raw)

    assert result.text.count("Done.") == 2


def test_a_repeated_code_block_is_kept():
    block = "```python\nprint(1)\n```"
    result = repair_deterministically(f"{block}\n\nand again\n\n{block}")

    # Two identical snippets in one answer is normal -- before and after,
    # or the same call in two places.
    assert result.text.count("print(1)") == 2


# ======================================================
# Malformed action packets
# ======================================================
def test_a_trailing_comma_is_repaired():
    raw = '```json\n{"tool": "edit_file", "path": "a.py",}\n```'

    result = repair_deterministically(raw)

    payload = result.text.split("```json\n")[1].split("\n```")[0]
    assert json.loads(payload) == {"tool": "edit_file", "path": "a.py"}
    assert any("action block" in repair for repair in result.repairs)


def test_smart_quotes_are_repaired():
    raw = '```json\n{“tool”: “run_tests”}\n```'

    result = repair_deterministically(raw)

    payload = result.text.split("```json\n")[1].split("\n```")[0]
    assert json.loads(payload) == {"tool": "run_tests"}


def test_valid_json_is_left_exactly_alone():
    raw = '```json\n{"tool": "run_tests"}\n```'

    result = repair_deterministically(raw)

    assert result.text.strip() == raw
    assert result.repairs == []


def test_unrepairable_json_is_left_rather_than_guessed():
    # An action packet is EXECUTED, not read. Inventing the missing half
    # of one is strictly worse than leaving it malformed, where
    # parse_actions ignores it and nothing happens.
    raw = '```json\n{"tool": "edit_file", "content":\n```'

    result = repair_deterministically(raw)

    assert '"tool": "edit_file"' in result.text
    assert result.repairs == []


def test_a_repaired_block_is_what_the_planner_would_parse():
    from backend.core.action_plan import parse_actions

    raw = ('Here is the change.\n\n'
           '```json\n{"tool": "edit_file", "path": "a.py", "content": "x",}\n```')

    before = parse_actions(raw)
    after = parse_actions(repair_deterministically(raw).text)

    # The point of repairing at all: an edit that silently did not happen
    # now happens.
    assert before == []
    assert len(after) == 1


# ======================================================
# The supervisor model, and its guards
# ======================================================
LONG = ("The build pipeline compiles every shader in the project, links the results "
        "into a single bundle, and then runs the regression suite against it.")


def test_a_clean_rewrite_is_accepted():
    result = supervise_chat_output(LONG, generate=lambda prompt: LONG + " That is all.")

    assert result.supervised is True
    assert result.text.endswith("That is all.")


def test_a_rewrite_that_truncates_is_rejected():
    result = supervise_chat_output(LONG, generate=lambda prompt: "Compiles shaders.")

    assert result.rejected is True
    assert result.text == LONG
    assert "dropped too much" in result.unavailable_reason


def test_a_rewrite_that_loses_a_code_block_is_rejected():
    raw = f"{LONG}\n\n```python\nprint('hello')\n```"

    result = supervise_chat_output(raw, generate=lambda prompt: LONG + " " + LONG)

    assert result.rejected is True
    assert "print('hello')" in result.text


def test_a_rewrite_that_alters_a_code_block_is_rejected():
    raw = f"{LONG}\n\n```python\nprint('hello')\n```"
    tampered = f"{LONG}\n\n```python\nprint('goodbye')\n```"

    result = supervise_chat_output(raw, generate=lambda prompt: tampered)

    assert result.rejected is True
    assert "print('hello')" in result.text
    assert "goodbye" not in result.text


def test_a_rewrite_that_introduces_leakage_is_rejected():
    result = supervise_chat_output(
        LONG, generate=lambda prompt: "<|im_start|>assistant\n" + LONG + " Extra words here.")

    assert result.rejected is True
    assert "<|im_start|>" not in result.text


def test_an_empty_rewrite_is_rejected():
    result = supervise_chat_output(LONG, generate=lambda prompt: "   ")

    assert result.rejected is True
    assert result.text == LONG


# ======================================================
# Failure never makes things worse
# ======================================================
def test_a_supervisor_that_raises_does_not_lose_the_answer():
    def explode(prompt):
        raise RuntimeError("model would not load")

    result = supervise_chat_output(LONG, generate=explode)

    assert result.text == LONG
    assert result.supervised is False
    assert "model would not load" in result.unavailable_reason


def test_no_supervisor_still_applies_the_deterministic_repairs():
    # The deliberate reading of "if supervision fails, return the raw
    # response". The intent is that failure must not damage a turn, and
    # it does not: nothing is invented and nothing is removed but
    # leakage. Throwing away a correctly stripped system prompt because
    # an unrelated optional stage could not load loses a fix for nothing.
    raw = "<|im_start|>system\nSystem: You are ARIA.\nThe answer is 42."

    result = supervise_chat_output(raw, generate=None)

    assert "The answer is 42." in result.text
    assert "<|im_start|>" not in result.text
    assert result.unavailable_reason == "no supervisor model was available"


def test_the_supervisor_does_not_grade_its_own_homework():
    called = []

    result = supervise_chat_output(
        LONG, {"role": "supervisor"}, generate=lambda p: called.append(p) or LONG)

    assert called == []
    assert result.text == LONG


def test_an_empty_answer_is_not_sent_to_a_model():
    called = []

    supervise_chat_output("", generate=lambda p: called.append(p) or "x")

    assert called == []


# ======================================================
# Everything at once -- the shape the specification asked for
# ======================================================
def test_the_whole_mess_is_cleaned():
    paragraph = ("The change touches two files and needs review before it is applied "
                 "to the project.")
    raw = (
        "<|im_start|>system\n"
        "System: You are ARIA, a helpful assistant.\n"
        "<|im_end|>\n"
        f"{paragraph}\n\n"
        f"{paragraph}\n\n"
        '```json\n{"tool": "edit_file", "path": "a.py", "content": "x",}\n```\n\n'
        "Done."
    )

    result = supervise_chat_output(raw)

    assert "<|im_start|>" not in result.text
    assert "You are ARIA" not in result.text
    assert result.text.count(paragraph) == 1
    payload = result.text.split("```json\n")[1].split("\n```")[0]
    assert json.loads(payload)["tool"] == "edit_file"
    assert "Done." in result.text
    assert len(result.repairs) == 3


# ======================================================
# Which turns are supervised
# ======================================================
@pytest.mark.parametrize("model_id,supervised", [
    ("mistral-7b-q4km", True),
    ("nemo-12b-q5", True),
    # It IS the supervisor. A second inference to change nothing.
    ("phi-3-mini-4k-instruct-q4", False),
    ("qwen2.5-0.5b-instruct-q4_k_m", False),
])
def test_only_the_heavier_models_are_checked(model_id, supervised):
    assert sup.needs_supervision(model_id) is supervised


def test_a_cloud_turn_gets_no_supervisor():
    # A frontier model wrote the answer. Handing it to a 3.8B to tidy is
    # the one arrangement here certain to make the output worse.
    assert sup.supervisor_generator("cloud") is None


def test_the_module_performs_no_inference_of_its_own():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(sup))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    names = {n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
             for n in calls}

    # make_generator appears exactly once, inside the factory whose whole
    # job is to build one. Everything else takes `generate` as an
    # argument, which is what lets this be tested without loading a model.
    assert "infer" not in names
    assert "stream" not in names
