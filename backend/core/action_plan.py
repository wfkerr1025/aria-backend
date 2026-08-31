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
    "truncated_action",
    "ACTION_TOOLS",
    "BARE_AFFIRMATIVES",
    "LIVE_EXECUTION_PHRASES",
    "NEGATION_VETO",
    "parse_actions",
    "loads_lenient",
    "strip_action_json",
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
# Every fenced block in the answer, with its language tag, matched IN
# ORDER so that opening and closing fences pair.
#
# The first version matched ```(?!json) directly and found blocks that
# were not there: the CLOSING fence of one json block, the prose after
# it, and the OPENING fence of the next all satisfied the pattern. The
# prose between two action blocks was then read as a code block and
# used as a file's contents -- which is to say a model's commentary
# would have been written to disk. A test caught it.
#
# Consuming through the closing fence is what makes that impossible.
_ANY_FENCE = re.compile(r"```([a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)
BACKSLASH = chr(92)

# Raw control characters are illegal inside a JSON string. Each has
# exactly one escape it can have meant.
_CONTROL_ESCAPES = {
    "\n": BACKSLASH + "n",
    "\r": BACKSLASH + "r",
    "\t": BACKSLASH + "t",
}

_FENCED = re.compile(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", re.DOTALL)

_MAX_ACTIONS = 20


def _normalize(text) -> str:
    # Apostrophes are removed, not replaced with a space. Substituting
    # every non-alphanumeric turned "don't" into "don t", so the negation
    # veto never matched it and "don't apply the changes yet" read as
    # consent -- the one false positive in this module that costs a file.
    lowered = str(text or "").lower().replace("'", "").replace("’", "")
    return f" {' '.join(re.sub(r'[^a-z0-9]+', ' ', lowered).split())} "


def requests_live_execution(user_text: str, history=()) -> bool:
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

    if normalized.strip() in BARE_AFFIRMATIVES:
        return True

    return _is_an_imperative_file_request(user_text, history)


def _is_an_imperative_file_request(user_text: str, history=()) -> bool:
    """The user told ARIA to do the thing, in their own words.

    "Create the actual file" is a confirmation by any reading, and it
    matched nothing: the user asked for a file, ARIA proposed one, the
    user said create it, and nothing was staged. Then ARIA explained how
    to make files with a file manager. That loop is the complaint.

    What this grants is STAGING -- a copy in the ghost workspace and a
    diff to look at. The project is untouched, and commit is still a
    separate, explicit act. So the two consents are intact; what changed
    is that the first one no longer has to be phrased in ARIA's
    vocabulary rather than the user's.

    Narrow on two counts. detect_intent must read the message as a task
    request rather than a question -- "what files are in src/" is a tool
    turn and is not an instruction -- and the routing classifier must
    read it as file work. Both already exist and are already tested; this
    asks them, rather than adding a third opinion about what a sentence
    means.
    """
    try:
        from backend.chat.model_router import TURN_HEAVY, TURN_TOOLS, classify_text

        if classify_text(user_text, history) not in (TURN_TOOLS, TURN_HEAVY):
            return False

        return not _is_a_question(user_text)
    except Exception:  # pragma: no cover - consent must fail closed
        logger.exception("could not classify the request; not granting execution")
        return False


# Openers that make a sentence a question rather than an instruction.
# "What files are in src/" is file work and is not a request to change
# anything.
_QUESTION_WORDS = (
    "what", "which", "how", "why", "where", "who", "when",
    "is", "are", "was", "were", "does", "do", "did", "should",
)

# ...except when they are politeness wrapped around an instruction.
# "Can you create hello_world.py?" is a request by any reading, and a
# question mark is not a refusal.
_POLITE_REQUEST = (
    "can you", "could you", "would you", "will you", "can u",
    "please", "i need you to", "i want you to", "i'd like you to",
)


def _is_a_question(user_text: str) -> bool:
    """Whether this asks about something rather than asking for it.

    Decided here rather than by detect_intent's label, because that
    taxonomy has three separate names for what is plainly an
    instruction -- "edit existing.py and replace its contents" comes
    back as `request`, "rewrite existing.py" as `clarification_needed`,
    and only "update existing.py" as `task_request`. Keying consent on
    one of those meant two of three plain instructions granted nothing.
    """
    text = str(user_text or "").strip().lower()
    if not text:
        return True

    if any(marker in text for marker in _POLITE_REQUEST):
        return False

    first = text.split()[0].strip("',.!?") if text.split() else ""
    return text.endswith("?") or first in _QUESTION_WORDS


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


def _invocation(payload, index: int, only_code_block: str | None = None) -> ToolInvocation | None:
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
        # The model wrote the script and then proposed the file without
        # it. Measured, twice, on mistral-7b: "create hello_world.py"
        # came back as {"tool": "create_file", "path": "hello_world.py"}
        # in the same answer as a ```python block containing exactly the
        # script the user asked for.
        #
        # Taking that block is the reading a person would give it. Only
        # when there is EXACTLY ONE, because two blocks make it a guess,
        # and a guess about file contents is written to disk. With none
        # or several, the file is empty -- recoverable, visible in the
        # staged diff, and not a fabrication.
        args["content"] = only_code_block or ""

    return ToolInvocation(tool_name=name, args=args, step_id=f"action-{index}")


def _fenced_payload_blocks(source: str) -> list:
    """Every fenced block that looks like an action payload, in order.

    Both the well-formed ones and the ones the model never closed.
    _FENCED requires the payload to end with a brace, so a block whose
    JSON stopped mid-string was not merely unparseable -- it was never
    extracted, and neither the salvage nor the unsupported-tool report
    ever saw it. Measured on nemo-12b: three blocks in one answer, the
    middle one a complete C# class whose JSON wrapper was two characters
    short, discarded without a word.

    Document order, so a later write still wins over an earlier one
    exactly as before.
    """
    blocks = []
    for language, body in _ANY_FENCE.findall(str(source or "")):
        if language.lower() not in ("", "json"):
            continue
        payload = body.strip()
        if payload.startswith(("{", "[")):
            blocks.append(payload)
    return blocks


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

    # The same blocks parse_actions reads, including the ones the model
    # never closed. Measured: an answer inventing
    # "create_serializable_class" wrote it into a block whose JSON
    # stopped mid-string, _FENCED did not extract it, and the invented
    # tool went unreported -- silence, which is the worst answer here.
    for block in _fenced_payload_blocks(str(text or "")):
        try:
            payload = loads_lenient(block)
            if payload is None:
                raise ValueError("not json")
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


def _escape_control_characters(candidate: str) -> str:
    """Escape raw newlines and tabs that sit INSIDE a JSON string.

    The single most common way a model breaks JSON, and the one that
    matters most here, because the value it breaks is a file's contents.
    Measured live, twice:

        {"tool": "edit_file", "path": "open_world.py",
         "content": "def open_world:
            print(\"Opening the world...\")"}

    That is a real newline inside a string, which json.loads refuses with
    "Invalid control character". The action was correct, complete, and
    unparseable -- so nothing was staged and ARIA went on to explain how
    to create files by hand.

    Escaping is exact rather than a guess: a control character inside a
    JSON string is invalid, so there is only one thing it can have meant.
    Characters outside strings are untouched, which is what keeps the
    document's own formatting intact.
    """
    out = []
    in_string = False
    escaped = False

    for char in candidate:
        if in_string:
            if escaped:
                escaped = False
                out.append(char)
                continue
            if char == BACKSLASH:
                escaped = True
                out.append(char)
                continue
            if char == '"':
                in_string = False
                out.append(char)
                continue
            replacement = _CONTROL_ESCAPES.get(char)
            out.append(replacement if replacement else char)
            continue

        if char == '"':
            in_string = True
        out.append(char)

    return "".join(out)


# The one action, rebuilt by hand when JSON cannot read it.
#
# A model writing a file into a JSON string has to escape every quote in
# that file, and it does not. Measured live, asked for a complete
# inventory system, nemo-12b produced a well-formed block whose content
# contained a C# dictionary initializer:
#
#     _items = new Dictionary<string, int>
#     {
#         {"wood", 10},
#
# It escaped the quotes in `throw new Exception(\"...\")` and left these
# alone. So the JSON string ended at `{"`, the next character was `w`,
# and json.loads stopped there. parse_actions returned nothing, nothing
# was staged, and the user was told no usable action had been produced --
# for an answer that was complete and correct in every way except its
# punctuation.
#
# This is not a rare shape. Any file with a string literal in it hits it,
# which is most files, and no amount of prompt wording fixes a model's
# escaping reliably.
#
# WHY THIS IS SAFE TO DO STRUCTURALLY
# -----------------------------------
# The shape is fixed and small: one object, a tool name, a path, and a
# content blob that runs to the end. tool and path are short identifiers
# that models do not mangle. Content is whatever lies between the opening
# quote and the LAST quote before the closing brace -- which is exactly
# the file, however many quotes are inside it.
#
# Only attempted when json has already failed, and only for a payload
# holding exactly one action. Two actions have two places the content
# could end, and guessing between them is how a salvage becomes a
# corruption.
_ONE_TOOL = re.compile(r'"tool"\s*:\s*"([a-z_]+)"', re.IGNORECASE)
_ONE_PATH = re.compile(r'"path"\s*:\s*"([^"\n]{1,200})"')
_CONTENT_OPENS = re.compile(r'"content"\s*:\s*"')


def _salvage_one_action(payload: str):
    """A dict for a single-action payload whose content is badly escaped.

    Returns None whenever it is not certain -- more than one action, no
    recognisable tool, or no content to rescue.
    """
    text = str(payload or "")

    tools = _ONE_TOOL.findall(text)
    if len(tools) != 1:
        return None

    path = _ONE_PATH.search(text)
    if not path:
        return None

    opens = _CONTENT_OPENS.search(text)
    if not opens:
        # No content at all is a perfectly good action -- delete, create
        # a folder -- and json can read those, so it did not need us.
        return None

    # Everything to the last quote before the object closes. rstrip
    # first, because the payload may carry trailing whitespace from the
    # fence.
    tail = text.rstrip()

    if tail.endswith("}"):
        # Everything to the last quote before the object closes.
        closing = tail.rfind('"', 0, len(tail) - 1)
        if closing <= opens.end():
            return None
        content = tail[opens.end():closing]
    else:
        # The object was never closed -- no final quote, no final brace.
        #
        # Measured on nemo-12b, asked for a Unity inventory: it wrote a
        # complete C# class into "content" and then stopped, still inside
        # the string, while inside a properly closed ```json fence. json
        # could not read it, this salvage refused it for not ending in a
        # brace, and a finished file was thrown away over two missing
        # characters.
        #
        # The FENCE is what makes this safe. It is a real boundary the
        # model wrote deliberately, so the content ends there rather than
        # wherever a guess would put it. A file genuinely cut off
        # mid-line still fails content_check and stages with the reason
        # attached, which is the existing net and the right one.
        content = tail[opens.end():]
        # A half-written escape at the very end would otherwise leave a
        # stray backslash in the file.
        if content.endswith(BACKSLASH):
            content = content[:-1]
        logger.info("salvaging an action whose json was never closed")

    # The escapes the model DID write are still escapes. Undoing them is
    # what turns \" into " and \n into a newline, and leaving them would
    # write the backslashes into the file.
    content = _unescape_json_body(content)

    logger.info("salvaged a single %s action for %s that json could not read",
                tools[0], path.group(1))
    return {"tool": tools[0], "path": path.group(1), "content": content}


_JSON_ESCAPES = {
    '"': '"', "\\": "\\", "/": "/",
    "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
}


def _unescape_json_body(text: str) -> str:
    """Apply JSON's own escapes, and pass anything else through as written.

    json.loads would refuse an unknown escape. Here an unknown one is far
    more likely to be a Windows path or a regex the model wrote into the
    file than a mistake worth failing over, so it survives verbatim.
    """
    out = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != "\\" or index + 1 >= length:
            out.append(char)
            index += 1
            continue

        following = text[index + 1]
        if following in _JSON_ESCAPES:
            out.append(_JSON_ESCAPES[following])
            index += 2
            continue
        if following == "u" and index + 5 < length:
            try:
                out.append(chr(int(text[index + 2:index + 6], 16)))
                index += 6
                continue
            except ValueError:
                pass
        out.append(char)
        index += 1
    return "".join(out)


def loads_lenient(candidate: str):
    """json.loads, with the one repair a model reliably needs.

    Returns None when the text is not JSON at all. Never raises.
    """
    text = str(candidate or "")
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass

    try:
        return json.loads(_escape_control_characters(text))
    except (ValueError, TypeError):
        pass

    # Last: rebuild it by hand. A model writing a file into a JSON string
    # has to escape every quote in that file, and it does not.
    return _salvage_one_action(text)


def _unfenced_payloads(text: str) -> list:
    """Action objects the model wrote without a code fence, with spans.

    Measured: asked to create a file, mistral-7b answered with the whole
    action and no fence at all --

        {"tool": "edit_file", "path": "hello_world.py", "content": "..."}

    -- so parse_actions found nothing, no action ran, and the next three
    turns had the model claiming it had created the file. The proposal
    was right there and the format was one pair of backticks away.

    Requiring a "tool" key is what keeps this narrow. Prose containing a
    stray brace does not match; an answer whose JSON has no tool in it
    does not match. Scanned with brace counting rather than a regex
    because content is a JSON string that can hold braces of its own.
    """
    payloads = []
    source = str(text or "")
    index = 0

    while True:
        start = source.find("{", index)
        if start == -1:
            return payloads

        depth, in_string, escaped, end = 0, False, False, -1
        for position in range(start, len(source)):
            char = source[position]
            if in_string:
                if escaped:
                    escaped = False
                elif char == chr(92):
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = position + 1
                    break

        if end == -1:
            return payloads

        candidate = source[start:end]
        parsed = loads_lenient(candidate)

        if isinstance(parsed, dict) and isinstance(parsed.get("tool") or parsed.get("name"), str):
            # The span comes back too, so the display layer can remove
            # exactly what was read as an action rather than running a
            # second scanner that might disagree with this one.
            payloads.append((parsed, start, end))

        index = end


def strip_action_json(text: str) -> str:
    """The answer with unfenced action objects removed.

    Fenced blocks are the display layer's own business; this covers the
    ones written bare, using the SAME scan that read them as actions so
    the two cannot disagree about what is an action and what is prose.
    """
    source = str(text or "")
    spans = [(start, end) for _payload, start, end in _unfenced_payloads(source)]

    for start, end in reversed(spans):
        source = source[:start] + source[end:]
    return source


# An action block the model never finished.
#
# Measured live. Asked for "a complete standard inventory system",
# nemo-12b generated for 64 seconds and stopped mid-string, because the
# turn had 2048 tokens and the file needed more. An unterminated JSON
# string parses to nothing, so parse_actions returned nothing, and the
# reply was the raw block followed by "I described that but did not
# produce a usable action".
#
# Two things were wrong with that, beyond the budget:
#
#   the user was shown machine syntax, which is the exact thing
#   action_render exists to prevent, and
#
#   "did not produce a usable action" describes a model that wandered
#   off. This model did exactly what it was asked and was cut off. Those
#   are different failures and the second one has an obvious next step.
#
# The fence may be missing too: a stream that stops mid-block never
# closes it, so this deliberately does not require one.
_UNFINISHED_TOOL = re.compile(
    r'\{\s*"tool"\s*:\s*"(?P<tool>[a-z_]+)"', re.IGNORECASE)
_UNFINISHED_PATH = re.compile(r'"path"\s*:\s*"(?P<path>[^"\n]{1,200})"')
_UNFINISHED_CONTENT = re.compile(r'"content"\s*:\s*"')


def truncated_action(text: str) -> dict | None:
    """What the model was part-way through, when nothing parsed.

    Returns None whenever an action DID parse -- a complete action is not
    a truncated one, and a turn that produced both should be described by
    the one that worked.
    """
    source = str(text or "")
    if not source.strip() or parse_actions(source):
        return None

    # It has to be genuinely unfinished. This claimed a COMPLETE block
    # was truncated once already, and told the user "I ran out of room
    # part-way through writing player_inventory.cs" about an answer that
    # was whole -- the real fault was unescaped quotes in the content,
    # and the confident wrong explanation was worse than the vague right
    # one it replaced. A block that closed its string and its brace did
    # not run out of room, whatever else is wrong with it.
    closed = source.rstrip().rstrip("`").rstrip()
    if closed.endswith('"}') or closed.endswith("}"):
        return None

    match = _UNFINISHED_TOOL.search(source)
    if not match:
        return None

    tail = source[match.start():]
    path = _UNFINISHED_PATH.search(tail)
    content_at = _UNFINISHED_CONTENT.search(tail)

    partial = ""
    if content_at:
        partial = tail[content_at.end():]
        # Whatever closing the model did manage, removed -- it is
        # punctuation from the block, not part of the file.
        partial = partial.rstrip().rstrip("`").rstrip()
        if partial.endswith('"}'):
            partial = partial[:-2]

    return {
        "tool": match.group("tool"),
        "path": path.group("path") if path else "",
        "content": partial,
        "lines": len(partial.splitlines()),
    }


def _drop_repeated_actions(invocations: list) -> list:
    """The same action, asked for twice, is one action.

    Not the same as _collapse_duplicate_writes, which picks a winner
    between two DIFFERENT writes to one path. This removes exact
    repeats -- identical tool, identical arguments -- which a model
    produces when it loops.

    Measured live: nemo-12b emitted create_folder src eleven times and
    run_tests three times in one answer. Every copy failed the same way,
    so the reply was twenty identical error lines with the real content
    somewhere in the middle. Running an operation eleven times is not
    eleven times as correct, and reporting it eleven times is not
    eleven times as clear.
    """
    seen = set()
    kept = []
    for invocation in invocations:
        try:
            fingerprint = (invocation.tool_name,
                           tuple(sorted((str(k), str(v))
                                        for k, v in (invocation.args or {}).items())))
        except Exception:  # pragma: no cover - an unhashable arg is not a repeat
            kept.append(invocation)
            continue
        if fingerprint in seen:
            logger.info("dropping a repeated %s action", invocation.tool_name)
            continue
        seen.add(fingerprint)
        kept.append(invocation)
    return kept


def _collapse_duplicate_writes(invocations: list) -> list:
    """One path, one write. The one that has contents wins.

    Measured on mistral-7b: asked for hello_world.py it proposed the file
    TWICE in one answer -- an empty create, then a write with the script
    in it. Two writes to one path in a single turn cannot both be meant,
    and keeping both makes the outcome depend on ordering while showing
    the user a list with the same file in it twice.

    Prefers content over emptiness rather than simply taking the last:
    the intent that carries a file's text is unambiguously the real one,
    whichever order the model happened to write them in.
    """
    kept: list = []
    by_path: dict = {}

    for invocation in invocations:
        if invocation.tool_name != "edit_file":
            kept.append(invocation)
            continue

        path = str(invocation.args.get("path") or "")
        previous = by_path.get(path)

        if previous is None:
            by_path[path] = invocation
            kept.append(invocation)
            continue

        has_content = bool(str(invocation.args.get("content") or "").strip())
        had_content = bool(str(previous.args.get("content") or "").strip())

        if has_content and not had_content:
            kept[kept.index(previous)] = invocation
            by_path[path] = invocation
            logger.info("collapsed a duplicate write to %s onto the one with contents", path)
        else:
            logger.info("dropped a duplicate write to %s", path)

    return kept


def parse_actions(text: str) -> list[ToolInvocation]:
    """The actions a model's answer asked for, in the order it wrote them.

    Never raises and never executes. An answer with no actions in it
    returns [], which is the common case and not a failure.
    """
    invocations: list[ToolInvocation] = []

    # The code the model wrote in this same answer, when there is exactly
    # one block of it. Used only to fill a create whose content the model
    # left out -- see _invocation.
    source = str(text or "")
    code_blocks = [
        body for language, body in _ANY_FENCE.findall(source)
        if language.lower() != "json" and body.strip()
        # A block with no language tag holding JSON is an action block
        # the model did not label. It is not the file's contents.
        and not body.strip().startswith(("{", "["))
    ]
    only_code = code_blocks[0] if len(code_blocks) == 1 else None

    # Both the well-formed blocks and the ones the model never closed.
    #
    # _FENCED requires the payload to end with a brace, so a block whose
    # JSON stopped mid-string was not merely unparseable -- it was never
    # extracted at all, and loads_lenient's salvage never saw it.
    # Measured on nemo-12b: three fenced blocks in one answer, the middle
    # one a complete C# class whose JSON wrapper was two characters
    # short, and the file was discarded without a word.
    #
    # Read in document order, so a later write still wins over an
    # earlier one exactly as before.
    blocks = _fenced_payload_blocks(source)

    # A model that wrote the action without a fence still wrote the
    # action. Only consulted when there is no fenced one, so a properly
    # fenced answer is read exactly as before and a fenced block is never
    # counted twice.
    if not blocks:
        for payload, _start, _end in _unfenced_payloads(source):
            invocation = _invocation(payload, len(invocations) + 1, only_code)
            if invocation is not None:
                invocations.append(invocation)

    for block in blocks:
        if len(invocations) >= _MAX_ACTIONS:
            logger.warning("more than %d actions in one answer; ignoring the rest",
                           _MAX_ACTIONS)
            break
        payload = loads_lenient(block)
        if payload is None:
            # A block that is not JSON is prose that happened to be
            # fenced. Skipped rather than guessed at: inventing what a
            # malformed action meant is how a wrong edit gets written.
            logger.info("action ignored: a fenced block was not valid JSON")
            continue

        # A model may write one action or a list of them in one block.
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            invocation = _invocation(candidate, len(invocations) + 1, only_code)
            if invocation is not None:
                invocations.append(invocation)

    invocations = _drop_repeated_actions(invocations)
    invocations = _collapse_duplicate_writes(invocations)

    if invocations:
        logger.info("parsed %d action(s): %s",
                    len(invocations), [i.label for i in invocations])
    return invocations
