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

__all__ = ["render_actions_for_reading", "strip_scaffolding"]

# The same fence parse_actions matches, so this can only remove blocks
# that were actually read as actions.
_JSON_FENCE = re.compile(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", re.DOTALL)

_HOW_TO_CONFIRM = 'Say "yes, do it" and I will stage it. Nothing changes until you commit.'
_ALREADY_STAGED = ("Staged in the ghost workspace. Review the diff and commit it in "
                   "Settings -> Workspaces; your project is untouched until you do.")

# ARIA's own scaffolding, when it comes back in the history as a previous
# assistant turn. The model imitates whatever it sees itself having said,
# so a rendered proposal in the transcript produces nested copies of the
# rendering in the next answer -- observed, with three "Here is what I
# would do:" headings in one reply, one of them empty.
_SCAFFOLDING = (
    "Here is what I would do:",
    "Staged:",
    "Done:",
    "- run the tests",
    _HOW_TO_CONFIRM,
    _ALREADY_STAGED,
)

# The described-action bullets this module writes: "- write `a.py` (2
# lines)", "- delete `notes.md`". They are the other half of the
# scaffolding and the more damaging half.
#
# Measured on nemo-12b across two turns. With turn one's rendering in the
# transcript, turn two answered with a python block and the line
# "- write `open_world.py` (5 lines)" -- imitating ARIA's summary instead
# of emitting an action. Nothing was staged, and because turn one HAD
# staged something the reply looked right. A conversation that gets worse
# the longer it runs is exactly what was reported.
#
# The backtick is what keeps this off ordinary prose: every bullet this
# module writes names a path in backticks, and "- write the tests first"
# does not.
_DESCRIBED_ACTION = re.compile(
    r"^-\s+(write|created|create|delete|move|rename|copy)\b[^`]*`", re.IGNORECASE)


# A request for confirmation, when the thing is already done.
#
# The model writes "Please confirm if this meets your requirement"
# because the brief tells it to ask -- and for a new file ARIA has
# already created it by the time the reply is rendered. Leaving both in
# reads as a contradiction: an answer that says Done and then asks
# permission.
#
# Only the ASK is dropped. Everything else the model wrote stays: it
# explained what it was making, and that is worth reading.
_ASKS_TO_CONFIRM = re.compile(
    r"^\s*(please\s+)?(confirm|let me know|tell me)\b.*$|"
    r"^\s*(shall|should|would|do)\s+(i|you)\b.*\?\s*$|"
    r"^\s*is (this|that) (what|ok|okay|correct)\b.*$",
    re.IGNORECASE,
)


def _drop_confirmation_requests(text: str) -> str:
    """Remove "please confirm" once the work has already happened."""
    kept = [line for line in str(text or "").splitlines()
            if not _ASKS_TO_CONFIRM.match(line.strip())]
    return "\n".join(kept).strip()


def _describe(invocation, created=()) -> str:
    args = invocation.args or {}
    path = args.get("path", "")

    # Past tense for a file that now exists. A new file is committed as
    # soon as it is asked for -- it destroys nothing, so there is nothing
    # for a second confirmation to protect.
    if invocation.tool_name == "edit_file" and str(path) in set(created or ()):
        content = str(args.get("content") or "")
        lines = len(content.splitlines())
        return f"created `{path}` ({lines} line{'' if lines == 1 else 's'})"

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


def render_actions_for_reading(answer_text: str, staged: bool = False,
                               expected_action: bool = False,
                               created=()) -> str:
    """The answer as a person should read it, with the blocks described.

    Returns the text unchanged when there are no actions in it, which is
    almost every turn.
    """
    text = str(answer_text or "")

    try:
        from backend.core.action_plan import parse_actions

        invocations = parse_actions(text)
        if not invocations:
            return _note_a_missing_action(text, expected_action)

        from backend.core.action_plan import strip_action_json

        # Fenced blocks, then the ones written bare. The model emitted an
        # action with no fence at all in a live session, and stripping
        # only fences left the raw JSON on screen beside a sentence
        # describing it.
        without_blocks = strip_action_json(_JSON_FENCE.sub("", text)).strip()

        made = {str(name) for name in (created or ())}
        if made:
            # It is already done; asking permission now is noise at best
            # and a contradiction at worst.
            without_blocks = _drop_confirmation_requests(without_blocks)
        described = [f"- {_describe(invocation, made)}" for invocation in invocations]

        # Three states, and the difference matters to whoever is reading.
        # A file that now exists is reported in the past tense; one
        # waiting for a commit is not; one waiting for a yes is neither.
        if made:
            heading = "Done:"
        elif staged:
            heading = "Staged:"
        else:
            heading = "Here is what I would do:"

        parts = [without_blocks] if without_blocks else []
        parts.append(heading)
        parts.append("\n".join(described))

        # Only say how to commit when something is actually waiting.
        # Telling the user to go and commit a file that already exists is
        # the same lie as telling them to confirm work already staged.
        still_waiting = [
            invocation for invocation in invocations
            if str(invocation.args.get("path") or "") not in made
        ]
        if still_waiting:
            parts.append(_ALREADY_STAGED if staged else _HOW_TO_CONFIRM)

        return "\n\n".join(parts)
    except Exception:  # pragma: no cover - display must not fail a turn
        logger.exception("could not render actions for reading; showing the raw answer")
        return text


_NO_ACTION = ("_I described that but did not produce a usable action, so nothing "
              "was staged. Ask me again and I will try once more._")

# Prose that reads like a proposal without being one. Measured on
# nemo-12b: "delete notes.md" came back as "I propose to delete the file
# notes.md. Please confirm." -- the explain-and-ask half of the brief
# with the block left out. It looks exactly like a working answer.
_SOUNDS_LIKE_A_PROPOSAL = (
    "i propose", "i will create", "i will delete", "i will move",
    "i will rename", "i will write", "shall i", "please confirm",
    "would you like me to",
)


def _note_a_missing_action(text: str, expected_action: bool) -> str:
    """Say when a file turn produced words and no action.

    Silence here is the failure the user actually experiences: ARIA says
    "I propose to delete notes.md, please confirm", nothing is staged,
    and the reply is indistinguishable from one that worked. Naming it
    costs a line and removes the whole class of "she said she did it and
    did not".
    """
    if not expected_action:
        return text

    lowered = str(text or "").lower()
    if not any(marker in lowered for marker in _SOUNDS_LIKE_A_PROPOSAL):
        return text

    return f"{text}\n\n{_NO_ACTION}"


def strip_scaffolding(text: str) -> str:
    """ARIA's own rendering, removed from a previous assistant turn.

    The model imitates what it sees itself having said. With a rendered
    proposal in the history it produced a reply containing three nested
    "Here is what I would do:" headings, one of them empty, and a
    confirmation line for a proposal that no longer existed.

    This is UI text, not something the model wrote, and it does not
    belong in the transcript the model reads.
    """
    source = str(text or "")
    kept = [
        line for line in source.splitlines()
        if line.strip() not in _SCAFFOLDING
        and not _DESCRIBED_ACTION.match(line.strip())
    ]
    return "\n".join(kept).strip()
