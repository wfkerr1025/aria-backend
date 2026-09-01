"""ARIA Lite - plugins that are command-line programs you type at.

Some integrations are a program: you type `blender --version` or
`unity env` and expect it to run. Typed into chat, those used to reach
a language model -- which cannot run anything, and so writes what such
a command usually produces.

Measured, twice, on this machine:

    unity env
      -> routed to phi-3-mini, answered with prose and a web search

    blender --background --python <script>
      -> routed to phi-3-mini, which produced `def main:` (not valid
         Python), invented bpy.ops.preferences.addonSettings (not a
         real operator), and then repeated one line eleven times until
         the stream was cut

Neither ran anything. The second is the more instructive failure: the
model did not decline, it produced something that looks like an answer
and is not one.

So a typed command line is answered by the program, or not at all.
This is the same rule the orchestrator already applies to workspace
questions -- "a fact with an authority behind it should never be
routed through a model" -- and a program is its own authority.

WHAT COUNTS AS ONE
------------------
A plugin appears here when it IS a program rather than merely having
one. Unity CLI and Blender qualify. Ludo.ai does not: it is an HTTP
API, and "ludo something" is a sentence, not a command.

Unity keeps its own module because it has a great deal more to say --
subcommand discovery, batchmode project locks, per-command timeouts.
This handles the plainer case, where a program takes flags and runs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from backend.core import cli_runner
from logger import get_logger

logger = get_logger(__name__)

__all__ = ["PROGRAMS", "answer_invocation", "parse_invocation", "program_names"]


# The programs a typed line may start with, and where their executable
# is configured. One entry per plugin that is a program.
#
# Adding a fifth integration that happens to be a CLI is one line here
# and nothing else -- which is the point of it being a table.
PROGRAMS = {
    "blender": {
        "plugin": "blender",
        "field": "blender_path",
        "label": "Blender",
        # Blender opens a window unless told otherwise, and a GUI that
        # nobody can see is a process that never exits. ARIA is not the
        # place to launch an interactive Blender, so a typed line that
        # does not already say --background gets it added.
        "force": ("--background",),
        "timeout": 900,
    },
}

# Long enough for a render, short enough that a stuck process is not
# forever. Blender's own default here is 15 minutes; see PROGRAMS.
DEFAULT_TIMEOUT_SECONDS = 300

# Words that mean the line is a sentence about the program rather than
# a command to it. "blender is a modelling tool" must never run.
_PROSE_AFTER_PROGRAM = frozenset({
    "is", "was", "are", "were", "be", "been", "can", "could", "does", "do",
    "did", "has", "have", "had", "will", "would", "should", "might", "must",
    "and", "or", "but", "the", "a", "an", "in", "on", "at", "for", "with",
    "to", "from", "about", "as", "by", "of", "that", "this", "it", "its",
    "seems", "looks", "says", "means", "gives", "needs", "uses", "supports",
    "requires", "keeps", "works", "runs", "lets", "makes", "files", "models",
})

# A command line with no flags is short. The cap is what stops
# "blender models are stored as .blend files" being read as a command.
_MAX_BARE_TOKENS = 3


def program_names() -> List[str]:
    """Every program a typed line may begin with."""
    return sorted(PROGRAMS)


def parse_invocation(text: str) -> Optional[dict]:
    """Read a typed line as a command for one of these programs.

    Deliberately narrow, because a false positive turns a sentence into
    an execution. A line counts when it starts with a known program and
    then either carries a flag -- unambiguous -- or is at most three
    words with no question mark and no sentence-starting word after the
    program name.

    Returns {"program": ..., "args": [...]} or None.
    """
    tokens = cli_runner.split_arguments(text)
    if len(tokens) < 2:
        return None

    program = tokens[0].strip().lower()
    if program not in PROGRAMS:
        return None

    rest = tokens[1:]
    if any(part.startswith("-") for part in rest):
        return {"program": program, "args": rest}

    if rest[0].strip().lower() in _PROSE_AFTER_PROGRAM:
        return None
    if "?" in text or len(tokens) > _MAX_BARE_TOKENS:
        return None
    if not re.fullmatch(r"[A-Za-z][\w.-]*", rest[0].strip()):
        return None

    return {"program": program, "args": rest}


def _executable(spec: dict) -> tuple:
    """(path, problem) for a program -- exactly one of them is set."""
    from backend.plugins import plugin_settings

    plugin = plugin_settings.load_plugins().get(spec["plugin"])
    if plugin is None or plugin.get("dismissed", False):
        return None, f"The {spec['label']} plugin is not installed."
    if not plugin.get("enabled", False):
        return None, (f"The {spec['label']} plugin is installed but switched "
                      f"off. Enable it on its page under Plugins first.")

    configured = str(plugin.get(spec["field"]) or "").strip()
    if not configured:
        return None, (f"No {spec['label']} path is set. Add one on its page "
                      f"under Plugins.")

    path = Path(configured)
    if not path.is_file():
        return None, f"Nothing runnable at {configured}."

    return path, None


def answer_invocation(invocation: dict, *, on_output=None) -> dict:
    """Run a typed command line and report what happened.

    Returns {"ran": bool, "text": str}. Every branch that did not run
    something says so in its first sentence, because the failure this
    exists to prevent is a report of work that never happened.
    """
    program = str(invocation.get("program") or "")
    spec = PROGRAMS.get(program)
    args = [str(a) for a in (invocation.get("args") or [])]
    spoken = " ".join([program, *args])

    if spec is None:  # pragma: no cover - parse_invocation gates this
        return {"ran": False, "text": f"I do not know a program called {program}."}

    executable, problem = _executable(spec)
    if problem:
        return {"ran": False,
                "text": f"I did not run `{spoken}`, and nothing happened.\n\n{problem}"}

    # Flags the program needs to be usable without a person at it.
    forced = [flag for flag in spec.get("force", ()) if flag not in args]
    arguments = forced + args

    result = cli_runner.run(executable, arguments, on_output=on_output,
                            timeout=int(spec.get("timeout") or DEFAULT_TIMEOUT_SECONDS),
                            label=program)

    body = _readable(result.get("output"))
    if result["success"]:
        return {"ran": True, "text": (
            f"Ran `{spoken}`.\n\n" + (body if body else "It finished and printed nothing."))}

    reason = result.get("error") or "it failed"
    return {"ran": True, "text": (
        f"Ran `{spoken}` and it failed: {reason}\n\n"
        + (body if body else "It printed nothing."))}


# How many lines of output belong in a chat message. Blender prints a
# great deal; the end is where a failure explains itself.
MAX_REPLY_LINES = 40


def _readable(output) -> str:
    lines = (output or "").strip().splitlines()
    if len(lines) <= MAX_REPLY_LINES:
        return "\n".join(lines)

    hidden = len(lines) - MAX_REPLY_LINES
    return (f"[{hidden} earlier line(s) not shown -- the end is where the "
            f"reason usually is]\n" + "\n".join(lines[-MAX_REPLY_LINES:]))
