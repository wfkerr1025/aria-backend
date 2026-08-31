"""ARIA Lite - telling the model that its tools exist.

The missing half. Everything downstream of a model writing an action was
built first: parse_actions reads the block, tool_orchestrator executes
it, fs_plan stages it, two consents gate it, the Control Center renders
it. None of that ever ran, because nothing told the model there was a
block to write.

Asked to create a file, ARIA answered "I'm unable to directly create
files on your system, but you can do it yourself with the following
command" -- which was accurate. As far as the model could tell, it had
no tools. The capability was real and unreachable, and a capability the
model does not know about is indistinguishable from one that does not
exist.

GENERATED FROM THE REGISTRY, NOT WRITTEN OUT
--------------------------------------------
The brief is built from ToolSchema objects, so the argument names in it
are the argument names the executor validates against. A hand-written
list would be correct on the day it was written and would then drift:
the model would emit `filename` where the tool wants `path`, the
invocation would be discarded as malformed, and the symptom would be
this exact one again -- an action that silently does not happen.

WHAT IT DOES NOT SAY
--------------------
It does not tell the model it can apply anything. The block is a
PROPOSAL: staged on the user's word, applied on their second. A brief
that said "you can edit files" would produce a model announcing changes
it has not made, which is the failure the staging system exists to
prevent, reintroduced through its own instructions.
"""

from __future__ import annotations

from logger import get_logger

logger = get_logger(__name__)

__all__ = ["action_tool_brief", "craft_rules"]

# Kept short deliberately. It is prepended to every tool-bearing turn, so
# every line costs context on a 4k-window model -- and a long brief is
# also a long thing for a 7B to half-follow.
#
# The example's path and language are load-bearing, which is not
# obvious and was measured twice. With the example written as
# hello_world.py, a user asking for hello_world.py got an action with no
# content at all -- the model appeared to avoid reproducing the example
# it had just been shown. Changing the path fixed that and the model
# then wrote a MARKDOWN-shaped comment into a .py, because the example
# had become a .md. It is now a small Python file with real code in it,
# under a name nobody asks for.
#
# It also has to say that the block is REQUIRED, not decorative.
# Measured on nemo-12b: asked to delete a file it answered "I propose to
# delete the file notes.md. Please confirm this action." -- doing the
# "explain it and ask" half of these rules and skipping the block
# entirely. The reply reads exactly like a working one and does nothing.
#
# Prescriptive about the SHAPE because the first version was not, and
# mistral-7b invented its own: given one example it replied with
# {"create_folder": ".", "create_file": {...}} -- no "tool" key, and a
# tool that does not exist. It had understood that it could act and not
# how to say so, which parses to zero actions and looks exactly like the
# refusal this brief replaced.
_PREAMBLE = """You can act on this project, not just describe it.

To propose an action, write a sentence explaining it, then a fenced json
block. Every block MUST have a "tool" key naming one of the actions
below, with that action's arguments beside it:

```json
{"tool": "edit_file", "path": "src/greet.py", "content": "def greet(name):\n    print(f\\"Hi {name}\\")\n"}
```

There is no create_file. A file is created by writing it with edit_file:
if the path does not exist, edit_file makes it.

Available actions:
"""

_RULES = """
Rules:
- The "tool" value must be one of the names above, exactly.
- Paths are relative to the project root. Never an absolute path.
- edit_file ALWAYS needs content. Write the file's actual text there.
  If the user asked for a hello world script, a README or anything else
  whose contents are obvious, WRITE THOSE CONTENTS. Never propose an
  empty file unless the user asked for an empty file.
- One action per block. Several blocks in one answer is fine.
- ALWAYS write the block. A sentence describing an action does nothing
  at all; only the block reaches the tools. "I propose to delete
  notes.md, please confirm" with no block is an answer that looks like
  it worked and did not.
- Nothing happens until the user agrees. The block is a proposal:
  say what it will do and ask them to confirm. Do not say you have
  created, deleted or moved anything -- you have proposed it.
- Never tell the user to run terminal commands to do something you can
  propose here.
"""


# What "write the file" means.
#
# Asked for "a player_inventory.cs file with a full inventory script",
# nemo-12b produced nine lines: a class holding a dictionary with Gold,
# Wood and Iron in it. Everything worked -- routed, parsed, checked,
# created, tested -- and the file was a sketch. Asked for "a complete
# standard inventory system" it wrote add and remove and stopped.
#
# Nothing downstream can fix that. A stub parses, balances its braces,
# and passes every check ARIA has, because it is well-formed; it is just
# not what was asked for. The only place this can be addressed is in
# what the model is told to produce.
#
# UNIVERSAL ON PURPOSE
# --------------------
# Not one word here is about inventories, or C#, or games. It says
# COMPLETE, VALIDATED, PERSISTABLE, OBSERVABLE, DOCUMENTED, TESTED --
# properties any working system in any language has -- and leaves every
# mechanism to backend/core/brief_plugins.py. The moment this file names
# JsonUtility it is wrong in Django, and the moment it names UnityEvent
# it is wrong everywhere but one engine.
#
# So a plugin says how a property is satisfied here; this says the
# property is required at all. With no plugins loaded a model still gets
# all eight rules and satisfies them in the plain idiom of whatever
# language it is writing, which is the correct default.
#
# WHAT IT COSTS, AND WHY THERE ARE TWO OF THEM
# --------------------------------------------
# The original brief's own comment: "Kept short deliberately. It is
# prepended to every tool-bearing turn, so every line costs context on a
# 4k-window model." That is still true and it is now in tension with
# being asked for more rules.
#
# Measured: mistral-7b and phi-3-mini hold 4096 tokens; nemo-12b holds
# 16384. The full rules are about 300 tokens. On the 12B that is two
# percent of the window and worth every token. On a 4k model it competes
# with the file being written, and a model that runs out of room
# mid-file produces nothing at all -- which this project has already
# watched happen.
#
# So there are two, and the window chooses. The short one keeps the
# rules that change the OUTPUT most and drops the ones a small model was
# never going to follow anyway.
_CRAFT = """
What to write when you write a file:
- Write the whole thing. A file you propose must work as it stands: no
  stubs, no placeholder bodies, no "TODO", no toy example standing in
  for the real one.
- Work out what the request implies and include all of it. If the user
  names a system, a working version of that system has a set of
  operations they did not list; write those too, not just the two they
  happened to mention.
- Validate what comes in. Guard clauses, explicit errors on bad input,
  safe defaults, and a log line where the language and project have one.
- Let its state be saved and loaded back, in whatever format is normal
  for this language and project.
- Expose hooks so other code can respond to it -- events, callbacks,
  signals, observers, whichever this language uses.
- Document it: a short summary on each public item, a clear separation
  between public API and private helpers, and names that say what they
  are.
- For anything beyond a trivial script, write its tests as well, in this
  project's existing style.
- Follow the conventions of the language and framework you are writing
  in, rather than translating another language's habits into it.
"""

# The same instruction, for a model whose window cannot afford the rest.
# Completeness first, because it is the one that changes the output most,
# and validation second, because a small model omits it by default.
_CRAFT_SHORT = """
What to write when you write a file:
- Write the whole thing: no stubs, no placeholders, no TODO. Include
  every operation the request implies, not only the ones named.
- Validate inputs, handle errors, and document each public item.
- Follow the conventions of the language you are writing in.
"""

# Below this many tokens of context, the short form is used. A 4096
# window has to hold the brief, the conversation and the file being
# written; above 8k there is room to spend on being precise.
_SMALL_WINDOW_TOKENS = 8192


def _describe(schema) -> str | None:
    """One line for one tool: its name, its arguments, what it is for."""
    if schema is None:
        return None

    required = [
        name for name, spec in (schema.parameters or {}).items()
        if isinstance(spec, dict) and spec.get("required")
        # `confirm` is set by the orchestrator from the user's own words,
        # never by the model. Listing it would invite a model to grant
        # itself the consent the whole design reads from the user.
        and name != "confirm"
    ]
    args = ", ".join(required)

    # The registry's description of edit_file ends "...writes nothing
    # unless confirm=true", which is true for a caller and must not reach
    # the model: a model that knows the flag exists can set it, and
    # `confirm` is the orchestrator's record of what the USER said.
    description = " ".join(
        sentence for sentence in str(schema.description or "").split(". ")
        if "confirm" not in sentence.lower()
    ).strip()

    return f"- {schema.name}({args}) - {description or schema.name}"


# The last thing the model reads, and only when a plugin has spoken.
#
# Measured, one run each, same prompt and same model. With the global
# rules alone: 63 lines, four methods, validated, typed exceptions. With
# a Unity plugin added: 29 lines, two methods, events and
# [Serializable] -- and the validation gone.
#
# The plugin overruled nothing. It competed for attention, which is what
# more instructions do to a 12B, and the rule furthest from the end
# lost. So the one that matters most is repeated where recency is on its
# side.
#
# Only when there are plugin sections to come after it. On a turn with
# no plugins the craft rules are already last, and saying it twice is
# noise.
#
# Stated as a mitigation, not a fix. Two runs are not a study, variance
# on this is wide, and no wording makes a 12B follow eight rules
# reliably. This is cheap and points the right way.
_CLOSING = """
Above all: write the complete system, not a sketch of one.
"""


def _context_window(model_id) -> int:
    """How much room this model has, or 0 when nothing says.

    Unknown means generous. A model whose window is not recorded is far
    likelier to be a large one nobody catalogued than a tiny one, and
    being wrong towards the full rules costs some context, while being
    wrong the other way costs the completeness they exist for.
    """
    if not model_id:
        return 0
    try:
        from backend.core.model_registry import get_model

        return int((get_model(model_id) or {}).get("maxContext") or 0)
    except Exception:  # pragma: no cover - a brief must not fail a turn
        logger.exception("could not read the context window for %s", model_id)
        return 0


def craft_rules(model_id=None) -> str:
    """The completeness rules, sized to what this model can hold."""
    window = _context_window(model_id)
    if 0 < window < _SMALL_WINDOW_TOKENS:
        logger.info("brief: %s holds %d tokens; using the short craft rules",
                    model_id, window)
        return _CRAFT_SHORT
    return _CRAFT


def action_tool_brief(model_id=None, hint: str = "") -> str:
    """The system message for a turn that may propose an action.

    Four parts, in this order: how to write a block, what tools exist,
    the rules for using them, and what a finished file looks like. The
    first three are mechanics -- get them wrong and nothing runs at all.
    The fourth is craft, and is the difference between a file that parses
    and the file the user asked for.

    Plugins come last, so a domain's conventions read as refinements of
    rules already stated rather than as the only thing the model heard.

    Empty string when the registry cannot be read, which makes the caller
    fall back to an ordinary chat turn rather than failing it.
    """
    try:
        from backend.core.action_plan import ACTION_TOOLS
        from backend.core.tool_registry import get_tool_schema

        lines = []
        for name in sorted(ACTION_TOOLS):
            described = _describe(get_tool_schema(name))
            if described:
                lines.append(described)

        if not lines:
            logger.warning("no action tools are registered; the brief would be empty")
            return ""

        brief = _PREAMBLE + "\n".join(lines) + "\n" + _RULES + craft_rules(model_id)

        # Last, and never instead. A plugin refines a rule that has
        # already been stated; it does not get to be the only thing the
        # model was told, and it cannot remove a rule it disagrees with.
        try:
            from backend.core import brief_plugins

            sections = brief_plugins.active_plugin_sections(hint)
        except Exception:  # pragma: no cover - plugins are not worth a turn
            logger.exception("could not read the brief plugins")
            sections = []

        if sections:
            brief = brief + "\n" + "\n\n".join(sections) + "\n" + _CLOSING

        return brief
    except Exception:  # pragma: no cover - a brief is not worth a turn
        logger.exception("could not build the action tool brief")
        return ""
