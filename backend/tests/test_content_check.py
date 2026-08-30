# backend/tests/test_content_check.py
#
# Does what the model wrote parse as what its extension says it is?
#
# Nothing asked that before. Measured on this machine, nemo-12b proposed
#
#     def open_world:
#         print("Opening the world...")
#
# which is not Python -- the parentheses are missing -- and it would have
# gone into the project as a file that cannot be imported. "ARIA created
# the file" and "ARIA created a working file" were the same claim, and
# only the first one was true.
#
# What this cannot do is as important as what it can. It does not know
# whether the code is correct, whether it does what was asked, or whether
# it breaks something else. Those need the test suite, which takes ninety
# seconds on this project and is not something to spend on every turn
# without being asked.

from __future__ import annotations

import pytest

from backend.core.content_check import check_content, describe_problem


# ======================================================
# The failure that actually happened
# ======================================================
def test_the_python_a_model_really_wrote():
    result = check_content("open_world.py", 'def open_world:\n    print("x")\n')

    assert result.failed is True
    assert result.line == 1
    assert "does not parse" in describe_problem("open_world.py", result)


@pytest.mark.parametrize("content", [
    'print("Hello, World!")\n',
    "def greet(name):\n    return f'hi {name}'\n",
    "x = 1\n\n\nclass A:\n    pass\n",
    'x = "a { brace inside a string"\n',
])
def test_valid_python_passes(content):
    result = check_content("a.py", content)

    assert result.ok is True
    assert result.checked is True


def test_json_is_checked():
    assert check_content("a.json", '{"a": 1}').ok is True
    assert check_content("a.json", '{"a": 1,}').failed is True


# ======================================================
# Languages with no parser here
# ======================================================
def test_balanced_braces_pass():
    content = ("public class PlayerInventory\n{\n"
               "    public int gold { get; set; }\n}\n")

    assert check_content("player_inventory.cs", content).ok is True


def test_a_truncated_file_is_caught():
    # The failure a model produces is a cut-off file, and a cut-off file
    # has unclosed braces.
    result = check_content("a.cs", "public class A\n{\n    void B()\n    {\n")

    assert result.failed is True
    assert "truncated" in result.problem


@pytest.mark.parametrize("content", [
    'string s = "a { brace in a string";\n',
    "// a comment with { in it\nint x = 1;\n",
    "/* block comment } */\nint y = 2;\n",
])
def test_braces_in_strings_and_comments_do_not_count(content):
    # A checker that cries wolf gets switched off, and then it checks
    # nothing.
    assert check_content("a.cs", content).ok is True


# ======================================================
# What is deliberately not checked
# ======================================================
@pytest.mark.parametrize("path", ["notes.md", "README", "data.csv", "a.txt"])
def test_unknown_types_are_not_guessed_at(path):
    result = check_content(path, "# anything { goes here")

    # Reported as unchecked rather than passed, because those are
    # different claims and the caller may want to say which it has.
    assert result.ok is True
    assert result.checked is False


def test_an_empty_file_is_well_formed():
    result = check_content("a.py", "")

    assert result.ok is True
    assert result.checked is False


def test_a_checker_fault_is_not_a_verdict(monkeypatch):
    import backend.core.content_check as module

    monkeypatch.setattr(module, "_check_python",
                        lambda content: (_ for _ in ()).throw(RuntimeError("boom")))

    # Refusing a turn over a fault in the checker would be worse than the
    # thing it checks for.
    assert check_content("a.py", "print(1)").failed is False


# ======================================================
# What the turn does with a failure
# ======================================================
def test_broken_content_loses_the_shortcut_but_not_the_work(tmp_path, monkeypatch):
    import json

    from backend.core import file_tools, ghost_workspace
    from backend.core.tool_orchestrator import run_answer_actions

    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))

    answer = ("```json\n" + json.dumps({
        "tool": "edit_file", "path": "open_world.py",
        "content": 'def open_world:\n    print("x")\n'}) + "\n```")

    report = run_answer_actions(answer, "create open_world.py for me")

    # Not written...
    assert report["created"] == []
    assert not (tmp_path / "open_world.py").exists()
    # ...but not thrown away either. A model that writes broken code has
    # still done most of the work, and the checker can be wrong about a
    # dialect.
    assert ghost_workspace.staged_files() == ["open_world.py"]
    assert "open_world.py" in report["problems"]
    assert any("does not parse" in note for note in report["notes"])


def test_valid_content_still_takes_the_shortcut(tmp_path, monkeypatch):
    import json

    from backend.core import file_tools
    from backend.core.tool_orchestrator import run_answer_actions

    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))

    answer = ("```json\n" + json.dumps({
        "tool": "edit_file", "path": "fine.py", "content": 'print("ok")\n'}) + "\n```")

    report = run_answer_actions(answer, "create fine.py for me")

    assert report["created"] == ["fine.py"]
    assert report["problems"] == {}
    assert (tmp_path / "fine.py").exists()


def test_the_problem_is_shown_in_the_reply():
    from backend.core.action_render import render_actions_for_reading

    out = render_actions_for_reading(
        '```json\n{"tool": "edit_file", "path": "a.py", "content": "def f:"}\n```',
        staged=True, problems={"a.py": "`a.py` does not parse at line 1: expected '('"})

    # Named in the answer, not left in a log. It is the difference
    # between "ARIA created the file" and "ARIA created a working file",
    # and the user is the one who has to know which happened.
    assert "does not parse" in out
