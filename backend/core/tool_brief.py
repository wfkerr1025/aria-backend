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

__all__ = ["action_tool_brief"]

# Kept short deliberately. It is prepended to every tool-bearing turn, so
# every line costs context on a 4k-window model -- and a long brief is
# also a long thing for a 7B to half-follow.
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
{"tool": "edit_file", "path": "hello_world.py", "content": "print(\\"Hello World\\")\n"}
```

There is no create_file. A file is created by writing it with edit_file:
if the path does not exist, edit_file makes it.

Available actions:
"""

_RULES = """
Rules:
- The "tool" value must be one of the names above, exactly.
- Paths are relative to the project root. Never an absolute path.
- content is the COMPLETE new text of the file, not a fragment.
- One action per block. Several blocks in one answer is fine.
- Nothing happens until the user agrees. The block is a proposal:
  say what it will do and ask them to confirm. Do not say you have
  created, deleted or moved anything -- you have proposed it.
- Never tell the user to run terminal commands to do something you can
  propose here.
"""


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


def action_tool_brief() -> str:
    """The system message for a turn that may propose an action.

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

        return _PREAMBLE + "\n".join(lines) + "\n" + _RULES
    except Exception:  # pragma: no cover - a brief is not worth a turn
        logger.exception("could not build the action tool brief")
        return ""
