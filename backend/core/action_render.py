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


# The same ask, riding on the end of a sentence rather than owning a
# line. Measured on nemo-12b: "Here's a proposal for creating a player
# inventory in C#. Please confirm:" -- and directly under it, "Done:".
# Line matching never saw it, because the ask starts mid-line, so the
# reply asked permission for work it had just reported finishing.
#
# Only a TRAILING clause is cut, and only after a sentence break, so the
# sentence the model wrote about its own work survives intact.
_TRAILING_ASK = re.compile(
    r"(?:(?<=[.!?])|^)\s*"
    r"(?:please\s+)?"
    r"(?:confirm|let me know|tell me|review it)\b[^.!?]*[.!?:]?\s*$",
    re.IGNORECASE,
)


def _drop_confirmation_requests(text: str) -> str:
    """Remove "please confirm" once the work has already happened."""
    kept = []
    for line in str(text or "").splitlines():
        if _ASKS_TO_CONFIRM.match(line.strip()):
            continue
        trimmed = _TRAILING_ASK.sub("", line).rstrip()
        # A line that was ONLY the ask is dropped rather than left blank.
        kept.append(trimmed if trimmed.strip() else "")
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
                               created=(), problems=None, failed=None,
                               new_folders=(), relocated=None,
                               verification=None) -> str:
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
        refused = dict(failed or {})

        # The model proposed one path and the file went to another,
        # because the user named no folder and the model invented one.
        # The description follows the file: saying "created
        # src/inventory.cs" when it is sitting in the root sends the user
        # looking in a directory that does not exist.
        for invocation in invocations:
            moved_to = (relocated or {}).get(str(invocation.args.get("path") or ""))
            if moved_to:
                invocation.args["path"] = moved_to
        if made:
            # It is already done; asking permission now is noise at best
            # and a contradiction at worst.
            without_blocks = _drop_confirmation_requests(without_blocks)
        # An action that did not run is reported as not run. The render
        # used to describe the PROPOSAL and stop there: a staging failure
        # -- "'src' is not a directory in the project" -- still printed
        # "Staged: - write src/player_inventory.cs (17 lines)", which is
        # ARIA claiming work it had not done. The description is built
        # from the invocation AND its result, or it is not a description
        # of what happened.
        described = []
        for invocation in invocations:
            path = str(invocation.args.get("path") or "")
            if path in refused:
                described.append(f"- **could not** {_describe(invocation, ())} "
                                 f"- {refused[path]}")
            else:
                described.append(f"- {_describe(invocation, made)}")

        # Three states, and the difference matters to whoever is reading.
        # A file that now exists is reported in the past tense; one
        # waiting for a commit is not; one waiting for a yes is neither.
        # A change that broke the tests was written and then taken back
        # out. "Here is what I would do" is wrong -- she did it -- and so
        # is "Staged", which reads like an ordinary proposal waiting on a
        # yes. It is a proposal that has already been tried and failed,
        # and the verification line underneath says how.
        harmed = verification is not None and verification.harmed

        if harmed:
            heading = "I tried that and took it back out:"
        elif refused and not made:
            heading = "I could not do that:"
        elif made:
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
            and str(invocation.args.get("path") or "") not in refused
        ]
        # Not when it was reverted. Inviting a "yes, do it" for a change
        # that has just been measured breaking the suite is asking the
        # user to approve something ARIA already knows the answer to.
        if still_waiting and not harmed:
            parts.append(_ALREADY_STAGED if staged else _HOW_TO_CONFIRM)

        # A file that does not parse is named here, not left in a log. It
        # is the difference between "ARIA created the file" and "ARIA
        # created a working file", and the user is the one who has to
        # know which of those happened.
        # A new folder is a bigger change than a new file, so it is said
        # out loud rather than inferred from a path. stage_path lets a
        # create bring its parent; this is the half that makes it visible.
        folders = [str(name) for name in (new_folders or ())]
        if folders:
            named = ", ".join(f"`{name}/`" for name in folders)
            verb = "This added" if made else "This will add"
            parts.append(f"{verb} the folder{'' if len(folders) == 1 else 's'} {named}.")

        # What the tests said. It goes in whether they passed or failed:
        # "I ran them and they pass" is the sentence that makes "Done"
        # mean something, and silence on a green run would make the red
        # one look like a new kind of event rather than the same check
        # reporting a different answer.
        if verification is not None:
            said = verification.describe()
            if said:
                parts.append(("[!] " + said) if verification.harmed else said)

        for message in (problems or {}).values():
            parts.append(f"[!] {message}")

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
