"""ARIA Lite - turning a model's answer into actions it may take.

Phase 7 plans are reasoning plans: what to read, what to compare, what
kind of answer to give. Their edit step targets `conversation` and
carries no path and no content, because it is an instruction to the model
about what to write, not an action on a file -- which is exactly what
tool_router says: "their edit step stays with the model, which is correct
while nothing is wired up to edit anything."

So an action cannot come from the planner. It has to come from the model,
because only the model can produce the one thing an edit needs and a plan
never had: the new contents of the file.

The format is a fenced JSON block naming a registered tool:

    ```json
    {"tool": "edit_file", "args": {"path": "notes.txt", "content": "..."}}
    ```

Chosen over prose parsing and over diffs for one reason each. Prose makes
the answer text load-bearing, so a formatting slip becomes a wrong edit
and the parser becomes a security boundary. Diffs are smaller and more
reviewable, but local models emit malformed hunks often enough that the
common failure would be a patch that will not apply. JSON is the format
these models are most reliable at, and it maps onto the arguments
tool_registry already validates -- so a well-formed action needs no
translation and a malformed one is rejected before anything runs.

Two boundaries this module exists to keep:

    Parsing is not executing. This returns ToolInvocations, which are
    records of an intention. tool_orchestrator decides whether anything
    happens, and defaults to a dry run.

    The model does not grant itself permission. requests_live_execution
    reads the USER's words. An action arrives in model output; the
    licence to apply it does not, and cannot, whatever the model writes.

Only tools on ACTION_TOOLS may be named. The registry holds tools a turn
uses for its own reasoning -- web_search, weather -- and a model asking
to "act" by searching is not an action the user needs to approve. The
allowlist is narrow on purpose and is meant to be extended a tool at a
time, deliberately.
"""

from __future__ import annotations

import json
import re

from backend.tools.tool_registry import ToolInvocation

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "ACTION_TOOLS",
    "BARE_AFFIRMATIVES",
    "LIVE_EXECUTION_PHRASES",
    "NEGATION_VETO",
    "parse_actions",
    "unsupported_actions",
    "requests_live_execution",
]

# The tools a model may ask to run as an action, as opposed to the ones a
# turn uses to answer a question. Narrow on purpose.
# The tools a model may ask for in an answer.
#
# The five shape-changing operations are here because they are safe to
# be here: their handlers stage and cannot touch the project (see
# backend/core/tool_registry.register_fs_operation_tools). Adding them to
# this set widens what ARIA can PROPOSE, not what it can do without
# being asked twice.
ACTION_TOOLS = frozenset({
    "edit_file", "run_tests",
    "delete_file", "create_folder", "move_file", "rename_file", "copy_file",
})

# What a user says to mean "do it for real". Matched whole-word, and read
# from the user's message only.
LIVE_EXECUTION_PHRASES: tuple[str, ...] = (
    "execute this", "execute the plan", "execute it",
    "apply the changes", "apply the change", "apply it",
    "run the plan", "make the edits", "make the edit",
    "perform the actions", "perform the action",
    "go ahead and do it", "do it for real",

    # Answering the question ARIA just asked.
    #
    # The first consent is a reply to "I will delete X and move Y.
    # Proceed?", and the natural reply to that is "yes, do it" -- which
    # matched nothing. The specification for this feature names that
    # exact phrase, and a consent step that does not recognise its own
    # standard answer is a consent step that quietly never fires.
    #
    # Safe to widen because of what this consent BUYS: staging, and only
    # staging. Nothing here reaches the project. The plan then has to be
    # read and committed -- a second, separate act -- so the worst case
    # for an over-eager match is an operation the user sees listed and
    # discards.
    "yes do it", "yes please do it", "do it", "go ahead",
    "please do", "please proceed", "proceed", "confirmed",
)

# Affirmatives that count only as a WHOLE message.
#
# "yes" is the most natural answer to "Proceed?" and the most dangerous
# substring in the language -- "yes, but not the delete" contains it, and
# so does "yesterday". Matched only when the entire message is one of
# these, which is exactly the shape of a reply to a yes/no question.
BARE_AFFIRMATIVES: frozenset[str] = frozenset({
    "yes", "yep", "yeah", "y", "ok", "okay", "sure", "confirm", "approved",
    "yes please", "do it", "go", "go ahead", "affirmative",
})

# What turns a directive into its opposite. Checked first, and
# deliberately blunt: "I don't want to wait, apply the changes" is
# refused too. Refusing a live run costs the user one more message;
# granting one they did not ask for costs them their files.
NEGATION_VETO: tuple[str, ...] = (
    "dont", "do not", "never", "without", "instead of",
    "not yet", "hold off", "wait",
)

# Fenced blocks, json-tagged or not. The tag is what the prompt asks for;
# accepting an untagged block too costs nothing and models drop the tag.
_FENCED = re.compile(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", re.DOTALL)

_MAX_ACTIONS = 20


def _normalize(text) -> str:
    # Apostrophes are removed, not replaced with a space. Substituting
    # every non-alphanumeric turned "don't" into "don t", so the negation
    # veto never matched it and "don't apply the changes yet" read as
    # consent -- the one false positive in this module that costs a file.
    lowered = str(text or "").lower().replace("'", "").replace("’", "")
    return f" {' '.join(re.sub(r'[^a-z0-9]+', ' ', lowered).split())} "


def requests_live_execution(user_text: str) -> bool:
    """Whether the USER asked for the actions to be applied for real.

    Deliberately takes the user's message and nothing else. A model that
    wrote "apply the changes" in its own answer has described an
    intention, not been granted one, and reading its output here would
    let it authorise itself.
    """
    normalized = _normalize(user_text)

    if any(f" {marker} " in normalized for marker in NEGATION_VETO):
        logger.info("live execution not granted: the request was negated")
        return False

    if any(f" {phrase} " in normalized for phrase in LIVE_EXECUTION_PHRASES):
        return True

    # A bare "yes". Whole message only -- see BARE_AFFIRMATIVES.
    return normalized.strip() in BARE_AFFIRMATIVES


# What a model calls a tool when it has not read the list carefully.
#
# Told in the brief that there is no create_file and that edit_file makes
# a missing path, mistral-7b proposed create_file twice. The intent was
# never in doubt -- "create hello_world.py" with a path argument -- and
# the block parsed to nothing, which looks to the user exactly like the
# refusal the brief was written to fix.
#
# Prompt wording is a weak lever on a 7B. This is the same principle the
# supervisor uses on malformed JSON: what is exactly solvable in code
# gets solved in code, and the model is left to do the part only it can.
# Nothing here widens what can happen -- every alias lands on a tool that
# stages and needs both consents.
_TOOL_ALIASES = {
    "create_file": "edit_file",
    "write_file": "edit_file",
    "new_file": "edit_file",
    "save_file": "edit_file",
    "update_file": "edit_file",
    "make_folder": "create_folder",
    "make_directory": "create_folder",
    "create_directory": "create_folder",
    "mkdir": "create_folder",
    "remove_file": "delete_file",
    # NOT "rm", and not "delete". An existing test lists rm alongside
    # "shell" and "run_shell" as names that must never resolve to an
    # action, and it is right to: this codebase forbids shell execution,
    # and a shell verb quietly becoming a file tool is how that boundary
    # stops being legible. Aliases here are tool-name variants a model
    # reaches for, not command names.
    "move": "move_file",
    "rename": "rename_file",
    "copy": "copy_file",
    "run_test": "run_tests",
    "test": "run_tests",
}


def _invocation(payload, index: int) -> ToolInvocation | None:
    if not isinstance(payload, dict):
        return None

    name = payload.get("tool") or payload.get("name")
    if not isinstance(name, str):
        return None
    name = name.strip()

    name = _TOOL_ALIASES.get(name.lower(), name)

    if name not in ACTION_TOOLS:
        # Includes tools that exist but are not actions, and tools that do
        # not exist at all. Both are refusals rather than errors: a model
        # naming something outside the list has not produced an action.
        logger.info("action ignored: %r is not an action tool", name)
        return None

    args = payload.get("args")
    if args is None:
        # Tolerated: a model that put the arguments at the top level
        # rather than under "args" has still said what it wants, and the
        # registry validates the result either way.
        args = {k: v for k, v in payload.items() if k not in ("tool", "name")}
    if not isinstance(args, dict):
        logger.info("action ignored: %r args were %s, not an object",
                    name, type(args).__name__)
        return None

    if name == "edit_file" and "content" not in args:
        # "Create hello_world.py" with no content is a request for an
        # empty file, not a malformed action. Refusing it would be
        # technically right and would leave the user with nothing.
        args["content"] = ""

    return ToolInvocation(tool_name=name, args=args, step_id=f"action-{index}")


def unsupported_actions(text: str) -> list[str]:
    """Tools an answer asked for that ARIA does not have.

    Separate from parse_actions because the two answer different
    questions, and the second one was going unasked. parse_actions
    returns the actions it CAN run; a block naming delete_file -- which
    is not implemented -- simply is not among them, and the turn then
    reported nothing at all. The model said it would delete the file, the
    file was not deleted, and nobody was told.

    Silence is the worst available answer there. A user who reads "I'll
    remove notes.md" and sees no error has every reason to believe it
    happened. This makes the refusal sayable.

    Never raises, never executes. Blocks that are not JSON are prose that
    happened to be fenced and are not counted.
    """
    names: list[str] = []

    for block in _FENCED.findall(str(text or "")):
        try:
            payload = json.loads(block)
        except (ValueError, TypeError):
            continue

        for candidate in (payload if isinstance(payload, list) else [payload]):
            if not isinstance(candidate, dict):
                continue
            name = candidate.get("tool") or candidate.get("name")
            # Shaped like an action -- a "tool" key with a string in it --
            # but naming something that does not exist.
            if not isinstance(name, str) or not name.strip():
                continue
            # The same aliases parse_actions applies. Without this a
            # create_file block ran correctly as edit_file AND was
            # reported as a tool ARIA does not have -- one action, two
            # contradictory messages.
            resolved = _TOOL_ALIASES.get(name.strip().lower(), name.strip())
            if resolved not in ACTION_TOOLS and resolved not in names:
                names.append(resolved)

    return names


def parse_actions(text: str) -> list[ToolInvocation]:
    """The actions a model's answer asked for, in the order it wrote them.

    Never raises and never executes. An answer with no actions in it
    returns [], which is the common case and not a failure.
    """
    invocations: list[ToolInvocation] = []

    for block in _FENCED.findall(str(text or "")):
        if len(invocations) >= _MAX_ACTIONS:
            logger.warning("more than %d actions in one answer; ignoring the rest",
                           _MAX_ACTIONS)
            break
        try:
            payload = json.loads(block)
        except (ValueError, TypeError):
            # A block that is not JSON is prose that happened to be
            # fenced. Skipped rather than repaired: guessing at what a
            # malformed action meant is how a wrong edit gets written.
            logger.info("action ignored: a fenced block was not valid JSON")
            continue

        # A model may write one action or a list of them in one block.
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            invocation = _invocation(candidate, len(invocations) + 1)
            if invocation is not None:
                invocations.append(invocation)

    if invocations:
        logger.info("parsed %d action(s): %s",
                    len(invocations), [i.label for i in invocations])
    return invocations
