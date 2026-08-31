"""ARIA Lite - which model takes this turn, decided before anything runs.

Four models are installed and they are good at different things. Until
now the choice between them was made by prompt length (complexity_router)
or not made at all (the session default, which is the 12B). Neither asks
the question that actually matters: what is this turn going to DO.

    classification  -> qwen2.5-0.5b   nobody reads its output but the code
    tools           -> mistral-7b     the default when a tool will run
    heavy reasoning -> nemo-12b       long context, deep analysis, code
    ordinary chat   -> phi-3-mini     the default voice

The 0.5B is the reason this exists. It is genuinely useful and genuinely
unable to hold a conversation, and the only way to have both is to know,
before generating, whether anyone is going to read the output.

WHAT THIS MODULE MUST NOT DO
----------------------------
Two invariants, both older than this file and both easy to break here:

    Absolute mode separation. In Cloud or Automatic mode the answer is
    None -- "ProviderRouter chooses" -- and never a local model id.
    model_violates_mode_separation() treats any concrete local id in
    Cloud Mode as a violation, so a router that helpfully named one
    would break the boundary while looking like an optimisation.

    The safety gate. This returns an id; it does not load one. The
    orchestrator passes what comes back through chat_capability_gate and
    then through _evaluate_safety, in that order, exactly as before.

A PIN IS RESPECTED. FULL STOP
-----------------------------
An explicitly chosen model wins here, and this module never overrides
one. That is not deference for its own sake -- it is the only way two
layers that both know something about capability can coexist.

The first version DID override a pin, for a model whose role says it
cannot chat. It was wrong, and the tests said so immediately: the 0.5B
was being moved aside HERE, so chat_capability_gate never fired, its
notice was never emitted, and its telemetry event vanished. A gate that
is never reached is not a gate. Worse, the two layers disagreed about
their own subject -- a role table and a parameter floor, both answering
"may this model chat", which is precisely how one authority becomes two
and the two drift.

So the division is clean:

    this module   decides which model gets an UNPINNED turn, from what
                  the turn is going to do;
    the gate      decides whether the model in hand may chat at all, by
                  the parameter floor, and redirects if not.

A pinned model that cannot chat is therefore still redirected -- by the
gate, with the gate's notice, exactly as it was before this file
existed. Nothing here is around it; this sits above it.

The tool prediction is still conservative, for the same reason it was
when it could override: it decides which model an unpinned turn lands
on, and a turn misread as tool-bearing lands on the tool model for no
reason. The signals are explicit ones -- naming a lookup, asking for a
file to change -- taken from the tables the planner itself uses, so a
turn this routes as tool-bearing is a turn the planner also treats that
way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.config import model_roles
from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "HEAVY_CONTEXT_CHARS",
    "ModelChoice",
    "TURN_CHAT",
    "TURN_CLASSIFICATION",
    "TURN_HEAVY",
    "TURN_TOOLS",
    "classify_turn",
    "select_model_for_turn",
]

TURN_CLASSIFICATION = "classification"
TURN_TOOLS = "tools"
TURN_HEAVY = "heavy_reasoning"
TURN_CHAT = "chat"

# "the caller said nothing about a pin", which is not the same as
# "the caller says there is no pin" -- the second is an answer and
# must be honoured rather than re-derived from the request.
_UNSET = object()

# Above this much conversation, a turn is escalated to the 12B whatever
# else it looks like. Not a token count: this counts characters across
# the whole request, because the point is "there is a lot to hold in
# mind at once", and a rough measure of that beats a precise measure of
# something else. phi-3's window is 4k tokens, so a turn approaching it
# has to move regardless of how simple the question sounds.
HEAVY_CONTEXT_CHARS = 6000

# Asking for depth, in the user's own words. Kept short and specific --
# a long list of vaguely intellectual words would escalate every turn to
# the slowest model on the machine, which is the failure mode this whole
# file exists to avoid.
_HEAVY_PHRASES = (
    "think carefully", "think hard", "step by step", "step-by-step",
    "in depth", "in-depth", "thoroughly", "comprehensive",
    "architecture", "refactor", "trace through", "reason about",
    "why does", "root cause", "design a", "plan out",
)

# Workspace and file work. These are the turns where a malformed action
# packet DOES something rather than reads wrong, so they route to a model
# that can hold the shape.
#
# This was a list of fixed phrases and it was badly wrong. Probed against
# 27 ordinary ways of asking for a file operation it matched six: "create
# a file" routed correctly and "create a new file called notes.md" did
# not, because one extra word broke the substring. Twenty-one requests
# went to the chat model, whose role says can_tools False -- which is the
# "ARIA talks about the change instead of making it" failure that this
# whole layer exists to prevent, arriving through the layer meant to
# prevent it.
#
# So it is now three cheap signals instead of a phrase book:
#
#   a concrete path or filename   -- naming main.py is talking about this
#                                    project, whatever the sentence
#   a file verb near a file noun  -- "make a new folder", "update the config"
#   a workspace verb              -- commit, stage, discard, rollback
#
# Kept deliberately mechanical. A model could classify this better and
# would cost an inference on every turn to decide which model the turn
# gets, which is the wrong place to spend latency.
_WORKSPACE_PHRASES = (
    "in the workspace", "the workspace", "project root",
    "apply the change", "apply the patch", "make the change",
    "staged change", "working directory",
)

# A path is unambiguous: separators do not appear in conversation.
_PATH_TOKEN = re.compile(r"(?:\.{1,2}[/\\]|~[/\\])[\w.\-]|[\w.\-]+[/\\][\w.\-]+[/\\]")

# A bare directory: "what files are in src/". _PATH_TOKEN needs
# something AFTER the separator, so it misses a trailing one. The
# lookahead is what keeps "and/or" and "he/she" out of this.
_DIR_TOKEN = re.compile(r"\b[\w.\-]{2,}[/\\](?![\w.\-])")

# A filename is only unambiguous if the extension is one. Matching any
# word.word turns "3.13", "e.g." and "Node.js" into file operations, so
# the extension has to be a real one.
_CODE_EXTENSIONS = (
    "py|js|mjs|cjs|jsx|ts|tsx|json|md|txt|rst|yml|yaml|toml|ini|cfg|conf|"
    "html|htm|css|scss|less|sh|bash|bat|ps1|c|h|cpp|hpp|cc|cs|java|go|rs|"
    "rb|php|sql|xml|csv|tsv|log|lock|env|gitignore|gguf|ipynb|vue|svelte"
)
_FILENAME = re.compile(rf"\b[\w\-]+\.({_CODE_EXTENSIONS})\b", re.IGNORECASE)

# Library names that are shaped exactly like filenames. "I like Node.js"
# is conversation, and without this it routes as a file operation --
# harmless (mistral-7b answers it fine) but slower than it needs to be,
# and the kind of thing that makes routing look arbitrary.
_NOT_FILENAMES = frozenset({
    "node.js", "next.js", "nuxt.js", "vue.js", "three.js", "d3.js",
    "express.js", "react.js", "chart.js", "socket.io",
})


_FILE_VERBS = (
    "create", "make", "write", "add", "delete", "remove", "rename", "move",
    "copy", "edit", "update", "modify", "change", "fix", "patch", "refactor",
    "read", "open", "show", "list", "generate", "scaffold", "implement",
)
_FILE_NOUNS = (
    "file", "files", "folder", "folders", "directory", "directories",
    "script", "scripts", "module", "modules", "class", "classes",
    "function", "functions", "method", "methods", "handler", "handlers",
    "endpoint", "endpoints", "test", "tests", "config", "readme",
)

# Acting on staged work. These are verbs with no innocent reading in a
# project assistant -- nobody asks ARIA to "discard my changes" rhetorically.
_WORKSPACE_VERBS = ("commit", "stage", "unstage", "discard", "rollback", "revert")

# Said alongside "unity", these mean the CLI rather than the engine as a
# subject. "unity build" is a request; "unity is a game engine" is not,
# and neither is "how do I use Unity's animator" -- none of those words
# appear in it.
_UNITY_CLI_WORDS = (
    "env", "cli", "pipeline", "build", "test", "cmd", "scene",
    "run", "list", "project", "editmode", "playmode", "buildmode",
)

# "run the tests", "run pytest". The verb alone is far too common.
_RUN_VERBS = ("run", "execute", "launch")
_RUN_NOUNS = ("test", "tests", "pytest", "suite", "build", "lint", "typecheck", "script")

# The same veto the workspace-query intent uses, and for the same reason:
# "how do I create a file in python" asks for instructions, not for a file.
# It is skipped when a real path is named -- someone who says parser.py is
# talking about this project however they phrased the question.
_INSTRUCTIONAL = (
    "how do i", "how do you", "how can i", "how would i", "how to ",
    "what is a", "what is the difference", "explain what", "in python",
    "in javascript",
)


def _word_set(text: str) -> set:
    return set(re.findall(r"[a-z]+", text.lower()))


@dataclass(frozen=True)
class ModelChoice:
    """The model this turn should use, and why.

    A value, not an action. The orchestrator decides what to do with it,
    which is what keeps this testable without a registry or a socket.
    """

    model_id: str | None
    turn_kind: str
    reason: str
    # The user named this model and it was honoured.
    pinned: bool = False
    # Moved up to the heavy tier from the tool default.
    escalated: bool = False


def _text_of(turn_request) -> str:
    return str(getattr(turn_request, "latest_user_text", "") or "")


def _context_chars(turn_request) -> int:
    messages = getattr(turn_request, "messages", None) or []
    return sum(len(str(m.get("content", ""))) for m in messages if isinstance(m, dict))


def _mentions_tools(text: str) -> bool:
    lowered = text.lower()

    # A named path outranks everything, including the instructional veto.
    named_a_path = bool(_PATH_TOKEN.search(text) or _DIR_TOKEN.search(text)) or any(
        match.group(0).lower() not in _NOT_FILENAMES
        for match in _FILENAME.finditer(text)
    )
    if named_a_path:
        return True

    if any(phrase in lowered for phrase in _WORKSPACE_PHRASES):
        return True

    if any(veto in lowered for veto in _INSTRUCTIONAL):
        return False

    words = _word_set(lowered)

    # A turn about the Unity CLI is a tool turn, however short it is.
    #
    # This is here because of what short meant. complexity_router picks
    # its model from prompt LENGTH, and "unity env" is nine characters
    # -- so it went to phi-3-mini, which cannot call tools, and answered
    # with prose and a web search. The length of a request is not a
    # measure of what it asks for, and this is the case that shows it
    # most plainly: the shortest messages here are the ones that most
    # need a model able to act.
    if "unity" in words and (words & set(_UNITY_CLI_WORDS)):
        return True

    if words & set(_WORKSPACE_VERBS):
        return True

    if (words & set(_FILE_VERBS)) and (words & set(_FILE_NOUNS)):
        return True

    if (words & set(_RUN_VERBS)) and (words & set(_RUN_NOUNS)):
        return True

    # The planner's own tables, rather than a second copy. A turn this
    # routes as tool-bearing is then a turn the planner also treats that
    # way -- two vocabularies would eventually disagree, and the
    # disagreement would show up as a model chosen for work it never did.
    try:
        from backend.core import search_intent

        if search_intent.mentions_web_search(text):
            return True
    except Exception:  # pragma: no cover - a vocabulary fault is not fatal
        logger.exception("could not consult search_intent while routing")

    # action_plan.requests_live_execution used to be consulted here and
    # must not be: it now asks classify_turn whether the message is an
    # imperative file request, and classify_turn asks this function.
    # Each call re-entered the other and the turn never returned -- the
    # test suite went from 96 seconds to a hang.
    #
    # Nothing is lost. The phrases it recognised ("apply the changes",
    # "make the edits") are in _WORKSPACE_PHRASES above, matched here
    # directly, so the same sentences still route as tool work.
    return False


def _is_heavy(text: str, context_chars: int) -> bool:
    if context_chars >= HEAVY_CONTEXT_CHARS:
        return True

    lowered = text.lower()
    if any(phrase in lowered for phrase in _HEAVY_PHRASES):
        return True

    # The existing classifier gets the last word on difficulty rather
    # than a third heuristic being invented here.
    try:
        from backend.core.task_classifier import classify_task_complexity

        return classify_task_complexity(text) == "high"
    except Exception:  # pragma: no cover
        logger.exception("could not classify task complexity while routing")
        return False


# A follow-up that changes the file ARIA just wrote.
#
# Measured live, two turns apart:
#
#   "create a player_inventory.cs file..."          -> tools. Worked.
#   "ok, I need you to add some things to the
#    inventory. First the inventory needs to be
#    36 slots..."                                   -> chat. Did nothing.
#
# The second names no file, so nothing here saw file work: no tool floor
# fired, phi-3-mini answered, and it wrote a ```csharp block into the
# chat. The user had asked for their file to be changed and got a code
# listing to copy by hand -- the exact thing this system exists to stop.
#
# A person reading those two messages has no doubt what the second one
# means. The only reason ARIA did is that it looked at one message at a
# time.
#
# WHAT MAKES THIS SAFE TO INFER
# -----------------------------
# Both halves are required, and each is narrow.
#
# The turn has to READ like a change: an imperative or a stated need,
# not a question. "How does the inventory work" stays chat.
#
# And the conversation has to have a file in it already -- a path named
# in a recent message by either side. Talking about inventories does not
# make a turn file work; having just written player_inventory.cs does.
#
# Only recent messages count. A file mentioned once, twenty turns ago,
# in a conversation that has moved on, is not what "the inventory" means
# any more.
_RECENT_MESSAGES = 6

# Said as an instruction rather than asked as a question. Deliberately
# not a general imperative list: these are the verbs of modification,
# because the file already exists and the turn is about changing it.
_CHANGE_INTENT = re.compile(
    r"\b(add|adjust|change|expand|extend|fix|implement|include|"
    r"increase|make (?:it|them|that)|modify|move|put|refactor|remove|"
    r"rename|rewrite|set|support|update)\b",
    re.IGNORECASE,
)

# The other half of how people phrase a change: not an imperative verb
# but a stated requirement. "I need the inventory to be 36 slots."
_STATED_NEED = re.compile(
    r"\b(i (?:need|want|would like)|it (?:needs|should|has) to be|"
    r"needs to (?:be|have|support)|should (?:be|have|support))\b",
    re.IGNORECASE,
)


def _file_in_recent_history(turn_request) -> bool:
    """Whether this conversation is already working on a named file."""
    messages = getattr(turn_request, "messages", None) or []
    for message in list(messages)[-_RECENT_MESSAGES:]:
        content = (message.get("content") if isinstance(message, dict)
                   else getattr(message, "content", ""))
        text = str(content or "")
        if not text:
            continue
        if _PATH_TOKEN.search(text):
            return True
        if any(match.group(0).lower() not in _NOT_FILENAMES
               for match in _FILENAME.finditer(text)):
            return True
    return False


def _continues_file_work(turn_request, text: str) -> bool:
    """A change to the file this conversation is already about.

    Requires both halves: the turn has to read like a change, and there
    has to be a file to change.
    """
    # Imported here, not at module scope. action_plan imports this
    # module's classifier, and a top-level import back would close the
    # loop -- which it did once before, turning a 96-second suite into a
    # hang.
    try:
        from backend.core.action_plan import _is_a_question
    except Exception:  # pragma: no cover - a classifier must not break a turn
        return False

    if _is_a_question(text):
        return False
    if not (_CHANGE_INTENT.search(text) or _STATED_NEED.search(text)):
        return False
    return _file_in_recent_history(turn_request)


def classify_text(text: str, history=()) -> str:
    """Classify a bare string, with the conversation it came from.

    Three call sites built this request object themselves and every one
    of them passed no history: the consent check, the ladder's role
    floor, and the transport's "did this turn expect an action". So a
    follow-up naming no file -- "add some things to the inventory" --
    read as ordinary chat at all three, and a turn that was plainly
    about a file got a chat model, no tool floor, and no action.

    One function, one shape, one place to pass the history. A caller
    that genuinely has none passes none and gets exactly the old
    answer.
    """
    class _Request:
        latest_user_text = str(text or "")

    _Request.messages = list(history or ())
    return classify_turn(_Request())


def classify_turn(turn_request, workspace_state=None, *, intent=None,
                  classification_only=False) -> str:
    """What kind of work this turn is. Decided from the request alone."""
    if classification_only:
        return TURN_CLASSIFICATION

    text = _text_of(turn_request)
    context_chars = _context_chars(turn_request)

    try:
        from backend.core.conversation_manager import INTENT_SEARCH_QUERY

        tool_intent = intent == INTENT_SEARCH_QUERY
    except Exception:  # pragma: no cover
        tool_intent = False

    tools = (tool_intent or _mentions_tools(text)
             or _continues_file_work(turn_request, text))
    heavy = _is_heavy(text, context_chars)

    if tools:
        # A tool turn that is also deep work escalates rather than
        # choosing between the two: the tool plan is the harder half.
        return TURN_HEAVY if heavy else TURN_TOOLS
    return TURN_HEAVY if heavy else TURN_CHAT


def _family_for(turn_kind: str) -> str:
    # The table lives in model_roles, so the routing done here and the
    # floor complexity_router applies when this defers cannot disagree.
    return model_roles.family_for_turn(turn_kind)


def select_model_for_turn(turn_request, workspace_state=None, *, intent=None,
                          classification_only=False, pin=_UNSET) -> ModelChoice:
    """The model this turn should run on.

    Returns model_id None to mean "ProviderRouter chooses", which is what
    Cloud and Automatic mode always get and what a local install missing
    the ideal model falls back to. None is a real answer here, not a
    failure -- it is this codebase's way of deferring, and replacing it
    with a guess would put a name in front of the safety gate that the
    turn need not honour.

    `pin` is the caller's already-resolved explicit model. The
    orchestrator passes what _resolve_model_id produced -- which has had
    absolute mode separation applied to it -- so this cannot reinstate a
    pin that was just rejected. Omitted, the request's own field is read,
    which is what a direct caller wants.
    """
    session = getattr(turn_request, "session", None)
    mode = str(getattr(session, "mode", "local") or "local")

    # Absolute mode separation, first and without exception. Nothing
    # below this line may name a local model when the turn is not local.
    #
    # Automatic defers too, and that is deliberate rather than an
    # oversight -- but the comment that used to sit here was wrong about
    # WHY, in a way that hid a real bug for a whole session.
    #
    # It said None meant "a frontier model answers". In Cloud Mode that
    # is true. In Automatic mode it is not: None means a LOCAL model
    # chosen AFTER this function, by complexity_router, from prompt
    # length. And complexity_router rates "think carefully about the
    # architecture" as low complexity, so it chose the 0.5B -- for chat,
    # every turn, with this module, the capability gate and the role
    # table all standing by having each deferred to the next.
    #
    # The deferral itself is kept. Naming a model here would put every
    # Automatic turn in front of the safety gate, which was measured and
    # removed once before, and would take away AutoSelector's ability to
    # fall back to cloud when local is unsuitable.
    #
    # The fix is at the layer that actually decides: complexity_router
    # now floors its ladder at the same chat capability floor this gate
    # uses, and at the tool tier for a tool-bearing prompt. A floor works
    # wherever the decision is made; a gate only works where it is
    # placed.
    # Classified FIRST, then the model decided. Deferring on which model
    # runs a turn is not the same as having no opinion about what the
    # turn is, and collapsing the two hid the tool brief for a whole
    # session: the early return reported TURN_CHAT, the orchestrator
    # injects the brief only on TURN_TOOLS or TURN_HEAVY, so in Automatic
    # mode the model was never told its tools existed and went back to
    # answering "I'll create the file" without proposing anything.
    #
    # turn_kind is read by two other things besides the model choice --
    # the capability gate's replacement tier and the tool brief -- and
    # both are correct in every mode. Only model_id is mode-dependent.
    turn_kind = classify_turn(turn_request, workspace_state, intent=intent,
                              classification_only=classification_only)

    if mode != "local":
        return ModelChoice(None, turn_kind, f"{mode} mode routes through the provider")

    # The caller's already-resolved pin wins over the request field, and
    # that is a correctness rule rather than a convenience.
    #
    # _resolve_model_id drops a pin that violates absolute mode
    # separation before this is ever called. Reading requested_model_id
    # again here put the rejected model straight back -- a cloud id
    # reinstated onto a local turn, by the module whose own docstring
    # says it must never name one. An existing test caught it on the
    # first run. Mode separation is enforced in one place; this reads
    # the answer rather than forming a second opinion.
    if pin is not _UNSET:
        pinned = pin
    else:
        pinned = (getattr(turn_request, "requested_model_id", None)
                  or getattr(session, "explicit_model_override", None))

    if pinned:
        # Handed straight back, whatever its role says. If it cannot hold
        # a chat turn, chat_capability_gate redirects it a few lines
        # later in the orchestrator -- with its notice and its telemetry.
        # Doing it here instead means the gate never runs.
        return ModelChoice(pinned, turn_kind, "explicitly selected", pinned=True)

    chosen = model_roles.installed_model_for(_family_for(turn_kind))

    if chosen is None:
        # The ideal model for this turn is not installed. Deferring beats
        # naming a model that is not there: ProviderRouter and the
        # complexity ladder both still work, and they know what this
        # install actually has.
        logger.info("routing: no installed model for a %s turn; deferring to the router",
                    turn_kind)
        return ModelChoice(None, turn_kind, f"no installed model for {turn_kind}")

    return ModelChoice(
        chosen, turn_kind,
        {
            TURN_CLASSIFICATION: "classification only, nobody reads this",
            TURN_TOOLS: "this turn will use tools",
            TURN_HEAVY: "deep reasoning or long context",
            TURN_CHAT: "ordinary chat",
        }[turn_kind],
        escalated=turn_kind == TURN_HEAVY,
    )
