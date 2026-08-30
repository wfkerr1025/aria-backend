"""ARIA Lite - showing a proposed action to a person instead of to a parser.

A fenced json block is how the model tells the machine what it wants. It
is not how ARIA should tell the user. Asked to create a file, mistral-7b
answered with the block and nothing else, so the entire reply on screen
was:

    ```json
    {"tool": "create_file", "path": "hello_world.py"}
    ```

Everything behind that worked -- it parsed, staged, and waited for
consent. The user was shown machine syntax and no sentence.

The brief asks the model to write the explanation itself, and it
sometimes does. Relying on that is the same mistake as relying on it to
spell the tool name correctly: prompt wording is a weak lever on a 7B,
and this is exactly solvable in code. So the block is replaced with a
description built from the parsed action, which cannot disagree with
what will actually happen -- it is made from the same invocation the
executor received.

WHAT IS REPLACED, AND WHAT IS NOT
---------------------------------
Only the action blocks. Prose the model wrote around them is kept, so an
answer that already explains itself keeps its own words and gains the
confirmation line. A ```python block stays exactly as written -- it is
the code being discussed, not scaffolding.

THE RAW ANSWER STILL DRIVES THE ACTIONS
---------------------------------------
This produces DISPLAY text. The transport parses actions from the raw
answer, before this runs. If the two were the same string, stripping the
block for the reader would take the action away from the executor -- the
proposal would vanish and nothing would be staged.
"""

from __future__ import annotations

import re

from logger import get_logger

logger = get_logger(__name__)

__all__ = ["render_actions_for_reading"]

# The same fence parse_actions matches, so this can only remove blocks
# that were actually read as actions.
_JSON_FENCE = re.compile(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", re.DOTALL)

_HOW_TO_CONFIRM = 'Say "yes, do it" and I will stage it. Nothing changes until you commit.'


def _describe(invocation) -> str:
    args = invocation.args or {}
    path = args.get("path", "")

    if invocation.tool_name == "edit_file":
        content = str(args.get("content") or "")
        if not content.strip():
            # Named, not hidden. A file proposed without contents is
            # real, committable and useless, and the only moment the user
            # can cheaply say "no, put the script in it" is before they
            # agree to it.
            return f"create `{path}` - **empty**, no contents were provided"
        lines = len(content.splitlines())
        return f"write `{path}` ({lines} line{'' if lines == 1 else 's'})"

    if invocation.tool_name == "delete_file":
        return f"delete `{path}`"
    if invocation.tool_name == "create_folder":
        return f"create the folder `{path}`"
    if invocation.tool_name == "move_file":
        return f"move `{path}` to `{args.get('dest', '')}`"
    if invocation.tool_name == "rename_file":
        return f"rename `{path}` to `{args.get('new_name', '')}`"
    if invocation.tool_name == "copy_file":
        return f"copy `{path}` to `{args.get('dest', '')}`"
    if invocation.tool_name == "run_tests":
        scope = args.get("scope")
        return f"run the tests ({scope})" if scope else "run the tests"

    return f"{invocation.tool_name} `{path}`"


def render_actions_for_reading(answer_text: str, staged: bool = False) -> str:
    """The answer as a person should read it, with the blocks described.

    Returns the text unchanged when there are no actions in it, which is
    almost every turn.
    """
    text = str(answer_text or "")

    try:
        from backend.core.action_plan import parse_actions

        invocations = parse_actions(text)
        if not invocations:
            return text

        from backend.core.action_plan import strip_action_json

        # Fenced blocks, then the ones written bare. The model emitted an
        # action with no fence at all in a live session, and stripping
        # only fences left the raw JSON on screen beside a sentence
        # describing it.
        without_blocks = strip_action_json(_JSON_FENCE.sub("", text)).strip()

        described = [f"- {_describe(invocation)}" for invocation in invocations]
        heading = "Staged:" if staged else "Here is what I would do:"

        parts = [without_blocks] if without_blocks else []
        parts.append(heading)
        parts.append("\n".join(described))
        if not staged:
            parts.append(_HOW_TO_CONFIRM)

        return "\n\n".join(parts)
    except Exception:  # pragma: no cover - display must not fail a turn
        logger.exception("could not render actions for reading; showing the raw answer")
        return text
