"""ARIA Lite - driving the Unity command-line tool.

Finds the `unity` CLI, asks it what commands it has, and runs them,
streaming the output back as it arrives.

WHY NOT unity_cli.py, WHICH THE TASK NAMED
------------------------------------------
backend/unity/unity_cli.py already exists and is something else: it is
ARIA's OWN command handler, the thing behind `aria unity list` and
`aria unity select`, and it calls input() to prompt a person at a
terminal. Overwriting it would have deleted a working handler to make
room for an unrelated module with a similar name. The task allowed "or
in a clearly separated Unity section", so this is that section, under a
name that says which of the two it is.

For the record, this repo now has several Unity modules and they are
genuinely different things:
  backend/core/unity_ops.py   - drives the Editor in batchmode through
                                ARIAEditorBridge.cs
  backend/unity/unity_ops.py  - project scanning and file creation
  backend/unity/unity_cli.py  - ARIA's own `aria unity ...` subcommands
  this module                 - drives an external `unity` executable
Nothing here duplicates those; where a path is needed it is asked for
rather than re-derived.

WHAT THE CLI ACTUALLY IS
------------------------
Verified by running it: "CLI for Unity" 1.0.0-beta.5, at
%LOCALAPPDATA%/Unity/bin/unity.exe. Everything below was checked
against `--help` and against live output, replacing an earlier set of
guesses that were wrong in three ways worth recording:

  --project does not exist. It is --project-path on the commands that
  take a flag, and a positional on `test` and `build`. Every command
  ARIA sent would have been rejected. The project is now passed as
  UNITY_PROJECT_PATH, which the CLI documents and which works for all
  of them.

  --mode belongs to `test` alone, not to every command.

  `list` returns {"data": {"tools": [...]}} -- 142 Editor tools from
  the Pipeline package, each run as `unity cmd <name>`, NOT as a
  top-level subcommand. The parser had never seen that shape and would
  have reported the CLI as saying nothing.

There is also a pager. Left on, a long listing would hold the pipe open
until the timeout killed it, so UNITY_NO_PAGER and UNITY_NON_INTERACTIVE
are set for every call: a prompt nobody can answer is a hang.

SAFETY
------
shell=False, always, and the argv is a list. The program run is only
ever the configured unity_cli_path -- never anything taken from a
command template, which supplies subcommands and flags and cannot
change which binary starts. Arguments are passed as separate argv
entries, so no quoting or metacharacter handling exists to get wrong.

Nothing here is registered as a model tool. These run when a person
presses Run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "COMMAND_PREFIX",
    "FIELD_MODE",
    "FIELD_PATH",
    "FIELD_PROJECT",
    "MODES",
    "PLUGIN_ID",
    "UnityCliUnavailable",
    "build_invocation",
    "cli_path",
    "cli_version",
    "command_record",
    "discover_commands",
    "list_commands",
    "run_command",
    "test_cli",
]


class UnityCliUnavailable(RuntimeError):
    """The Unity CLI is not configured, or is not where it was said to be."""


# The plugin this belongs to, and the fields it stores. Named here so
# the page, the validator and this module cannot disagree about what a
# setting is called.
PLUGIN_ID = "unity_cli"
FIELD_PATH = "unity_cli_path"
FIELD_PROJECT = "unity_cli_project"
FIELD_MODE = "unity_cli_mode"

# What a command entry is called in the registry. plugins.json holds
# both plugins and commands -- one registry, as the task requires -- and
# this prefix plus the "type" field is what tells them apart.
COMMAND_PREFIX = "unity_cmd_"
COMMAND_TYPE = "unity_cli_command"

MODES = ("EditMode", "PlayMode", "BuildMode")

# What the CLI is called. Windows first, because .cmd shims are how
# most Node- and dotnet-installed CLIs land there and PATH lookup finds
# the extensionless name last.
CLI_EXECUTABLES = ("unity.cmd", "unity.exe", "unity")

ENV_CLI_PATH = "ARIA_UNITY_CLI_PATH"
ENV_TIMEOUT = "ARIA_UNITY_CLI_TIMEOUT"

# Verified: `unity list --json` works, --json being a global shorthand
# for --format json. Still overridable, because a beta CLI may move.
ENV_LIST_ARGS = "ARIA_UNITY_CLI_LIST_ARGS"
LIST_ARGS = ("list", "--json")

# Tried in order, and the first that answers wins. The task named both
# and I cannot check which this CLI has, so it asks rather than assumes.
VERSION_PROBES = (("--version",), ("env",))

DEFAULT_TIMEOUT_SECONDS = 120

# Commands that spawn a whole Unity Editor and wait for it.
#
# 120 seconds is right for `env` or `list`, which answer in one, and
# badly wrong for a build: ARIA would kill Unity mid-compile and report
# a timeout, having destroyed the thing it was waiting for. A build of
# an empty project takes about a minute; a real one takes many.
#
# Half an hour is not a prediction, it is a ceiling -- long enough that
# reaching it means something is genuinely stuck rather than merely
# slow.
LONG_RUNNING_SECONDS = 1800
BATCHMODE_COMMANDS = frozenset({"build", "test", "run"})

# A version-looking token anywhere in the output of --version.
#
# The hyphen is in the class because this CLI reports "1.0.0-beta.5",
# and without it the card read "v1.0.0" -- a real version, for a
# different build than the one installed. A prerelease suffix is part
# of the version, not decoration after it.
_VERSION = re.compile(r"(\d+(?:\.\d+)+[A-Za-z0-9.\-]*[A-Za-z0-9])")


# ======================================================
# Finding the CLI
# ======================================================

def _plugin_setting(field: str) -> str:
    """One of this plugin's saved settings, or "".

    Never raises and never imports at module scope: plugin_settings
    imports discovery, discovery is imported by the registry, and a
    cycle here would be paid for at startup by everything.
    """
    try:
        from backend.plugins import plugin_settings

        return plugin_settings.configured_path(PLUGIN_ID, field)
    except Exception:  # pragma: no cover - a setting is not worth a crash
        logger.debug("could not read %s.%s", PLUGIN_ID, field, exc_info=True)
        return ""


def _hub_candidates() -> List[Path]:
    """Unity Hub ships tools beside the editors it installs."""
    from backend.plugins import plugin_discovery

    found: List[Path] = []
    for root in plugin_discovery.program_roots():
        for pattern in ("Unity/Hub/Editor/*/Editor/Data/Tools",
                        "Unity/Hub/*",
                        "Unity/Hub"):
            try:
                matches = sorted(root.glob(pattern))
            except OSError:
                continue
            for directory in matches:
                for name in CLI_EXECUTABLES:
                    candidate = directory / name
                    try:
                        if candidate.is_file():
                            found.append(candidate)
                    except OSError:
                        continue
    return found


def cli_path() -> Path:
    """Where the Unity CLI is, or a sentence saying why it is not.

    In order: the environment, then this plugin's saved setting, then
    PATH, then Unity Hub. Same order and same reasoning as unity_ops
    uses for the Editor -- an explicit setting beats a lucky guess, and
    an environment variable beats both so a shell can override a
    machine for one run.
    """
    configured = str(os.environ.get(ENV_CLI_PATH) or "").strip()
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return candidate
        raise UnityCliUnavailable(
            f"{ENV_CLI_PATH} points at {configured}, which is not a file.")

    saved = _plugin_setting(FIELD_PATH)
    if saved:
        candidate = Path(saved)
        if candidate.is_file():
            return candidate
        raise UnityCliUnavailable(
            f"The Unity CLI path is set to {saved}, but nothing is there.")

    for name in CLI_EXECUTABLES:
        found = shutil.which(name)
        if found:
            return Path(found)

    for candidate in _hub_candidates():
        return candidate

    raise UnityCliUnavailable(
        "No Unity CLI was found. Set its path on the Unity CLI plugin page.")


def _timeout(command: str = "") -> int:
    """How long a command may run before it is given up on.

    Per command, because they are not alike: `env` answers instantly
    and `build` starts an Editor. An explicit ARIA_UNITY_CLI_TIMEOUT
    still wins over both -- somebody who sets it means it.
    """
    raw = os.environ.get(ENV_TIMEOUT)
    try:
        value = int(str(raw))
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass

    if str(command or "").strip() in BATCHMODE_COMMANDS:
        return LONG_RUNNING_SECONDS
    return DEFAULT_TIMEOUT_SECONDS


def _list_args() -> Sequence[str]:
    raw = str(os.environ.get(ENV_LIST_ARGS) or "").strip()
    return tuple(raw.split()) if raw else LIST_ARGS


# ======================================================
# Running it
# ======================================================

def _extract_json(text: str):
    """The JSON in this output, or None.

    A CLI that prints a banner before its JSON is common enough to be
    worth handling: this takes the whole thing if it parses, and
    otherwise the first balanced object or array in it. Anything else
    is not JSON and says so by returning None rather than by raising --
    plain text output is a normal answer, not a fault.
    """
    body = (text or "").strip()
    if not body:
        return None

    try:
        return json.loads(body)
    except ValueError:
        pass

    for opener, closer in (("{", "}"), ("[", "]")):
        start = body.find(opener)
        end = body.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(body[start:end + 1])
            except ValueError:
                continue
    return None


def editor_has_project_open(project: str) -> bool:
    """Whether a Unity Editor currently holds this project.

    Unity refuses to open a project in batchmode while an Editor has
    it, and the refusal is silent: the log stops after "Successfully
    changed project path" and the process exits 1 with no message. A
    build that fails in one second for no stated reason is the worst
    kind of failure to hand somebody, so this asks first.

    The test is whether Temp/UnityLockfile can be opened for writing,
    not whether it exists. It survives a crash, so its presence alone
    would refuse builds on a project nothing has open -- and a guard
    that blocks the working case is worse than no guard.
    """
    if not project:
        return False

    lockfile = Path(project) / "Temp" / "UnityLockfile"
    try:
        if not lockfile.is_file():
            return False
        with open(lockfile, "a"):
            return False        # opened it, so nothing is holding it
    except PermissionError:
        return True
    except OSError:
        return False


def _kill_tree(process: "subprocess.Popen") -> None:
    """End the command and everything it started.

    process.kill() ends only the process ARIA started. That is not
    enough here: a .cmd shim is a cmd.exe that spawns the real tool,
    and Unity spawns compilers and importers of its own. Those children
    inherit the stdout pipe, so killing the parent alone leaves the
    pipe open and the read loop blocked -- the timeout fires, the
    result is correct, and the call still does not return until the
    child finishes on its own. Measured: a one-second timeout took
    twenty-nine seconds to come back.

    So the whole tree goes. taskkill /T on Windows, the process group
    on POSIX, and process.kill() as the fallback if either fails --
    ending one process is better than ending none.
    """
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True, timeout=10, check=False)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        return
    except Exception:
        logger.debug("could not end the Unity CLI process tree", exc_info=True)

    try:
        process.kill()
    except Exception:  # pragma: no cover - it may have just exited
        logger.debug("could not kill the Unity CLI", exc_info=True)


def _invoke(arguments: Sequence[str], *,
            on_output: Optional[Callable[[str, str], None]] = None,
            timeout: Optional[int] = None,
            cwd: Optional[str] = None) -> dict:
    """Run the CLI once and report what happened.

    Streams stdout line by line through on_output as it arrives, so a
    build that takes four minutes shows something for the first three.
    stderr is read after the process ends rather than concurrently:
    interleaving two pipes correctly needs threads, and a wrong
    interleaving is worse than a clearly separated one.

    Never raises for anything the CLI does. A crash, a timeout and a
    non-zero exit are all answers, and each says which it was.
    """
    try:
        executable = cli_path()
    except UnityCliUnavailable as error:
        return {"success": False, "error": str(error), "code": None,
                "output": "", "json": None}

    argv = [str(executable), *[str(part) for part in arguments]]
    leading = str(arguments[0]) if arguments else ""
    limit = timeout if timeout is not None else _timeout(leading)

    # A batchmode command against a project the Editor has open cannot
    # work, and saying so beats spending a second finding out and then
    # reporting forty lines of licensing handshake as the explanation.
    project_setting = _plugin_setting(FIELD_PROJECT)
    if leading in BATCHMODE_COMMANDS and editor_has_project_open(project_setting):
        return {
            # ran=False, because nothing did. "Ran X and it failed" for
            # a command that never started is the same small untruth as
            # reporting a project created that was not.
            "success": False, "ran": False, "code": None,
            "output": "", "json": None,
            "error": (f"Unity has this project open, and `{leading}` starts a "
                      f"second Unity that cannot share it. Close the Editor "
                      f"and run this again -- or use `unity cmd {leading}`, "
                      f"which runs inside the Editor already open."),
        }

    # The CLI reads all of these from the environment, which is better
    # than flags here: it applies to every subcommand without ARIA
    # having to know where each one accepts them.
    #
    # NO_PAGER is not a preference. A pager on a long listing waits for
    # a keypress that will never come, and the only thing that ends the
    # call is the timeout killing it.
    environment = dict(os.environ)
    environment.update({
        "UNITY_NON_INTERACTIVE": "1",
        "UNITY_NO_PAGER": "1",
        "UNITY_NO_BANNER": "1",
    })
    project = _plugin_setting(FIELD_PROJECT)
    if project:
        environment["UNITY_PROJECT_PATH"] = project

    logger.info("unity cli: %s", " ".join(argv))

    lines: List[str] = []
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd or None,
            env=environment,
            # shell=False is the default and is load-bearing: argv is a
            # list, so nothing in an argument can start a second command.
            shell=False,
            # Its own process group on POSIX, so _kill_tree can end the
            # command and its children together. Windows gets the same
            # effect from taskkill /T and needs no flag here.
            **({"start_new_session": True} if os.name != "nt" else {}),
        )
    except FileNotFoundError:
        return {"success": False, "code": None, "output": "", "json": None,
                "error": f"{executable} could not be run."}
    except OSError as error:
        return {"success": False, "code": None, "output": "", "json": None,
                "error": f"Could not start {executable}: {error}"}

    # THE DEADLINE IS A TIMER, NOT A wait(timeout=)
    #
    # Reading stdout line by line blocks until the pipe closes, so a
    # wait(timeout=...) after the read loop is only reached once the
    # process has already finished -- which is to say never, for the
    # hung build the timeout exists for. An earlier draft did exactly
    # that and a test that expected a one-second timeout sat for thirty.
    #
    # Killing on a timer ends the process, which closes the pipe, which
    # ends the read. It works whether the command is producing output
    # or silently stuck, and those are both things a Unity build does.
    expired = threading.Event()

    def give_up() -> None:
        expired.set()
        _kill_tree(process)

    deadline = threading.Timer(limit, give_up)
    deadline.daemon = True
    deadline.start()

    errors = ""
    try:
        if process.stdout is not None:
            for line in process.stdout:
                line = line.rstrip("\n")
                lines.append(line)
                if on_output is not None:
                    try:
                        on_output("stdout", line)
                    except Exception:  # pragma: no cover - a viewer is not the job
                        logger.debug("an output listener raised", exc_info=True)

        if process.stderr is not None:
            errors = process.stderr.read() or ""

        process.wait()
    finally:
        deadline.cancel()
        for pipe in (process.stdout, process.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:  # pragma: no cover
                pass

    if expired.is_set():
        return {"success": False, "code": None, "json": None,
                # What it managed to print before it was stopped is kept:
                # it is the only evidence of where it got stuck.
                "output": "\n".join(lines),
                "error": f"The command did not finish within {limit} seconds."}

    for line in (errors or "").splitlines():
        lines.append(line)
        if on_output is not None:
            try:
                on_output("stderr", line)
            except Exception:  # pragma: no cover
                logger.debug("an output listener raised", exc_info=True)

    output = "\n".join(lines)
    code = process.returncode
    parsed = _extract_json(output)

    if code != 0:
        # The output is kept, not thrown away. A failing build's reason
        # is in the output, and an error message that replaced it with
        # "exit code 1" would be the least useful thing ARIA could say.
        return {"success": False, "code": code, "output": output,
                "json": parsed,
                "error": (errors.strip().splitlines() or [f"Exited with code {code}."])[0]}

    return {"success": True, "code": code, "output": output,
            "json": parsed, "error": None}


# ======================================================
# Asking it what it can do
# ======================================================

def _probe() -> dict:
    """Ask the CLI who it is, once.

    Tries each probe in turn because the task named two and neither is
    verified: a CLI that rejects --version but answers `env` still gets
    read. Returns which probe answered and what version it gave, so a
    caller that wants both does not have to run the program twice --
    an earlier draft did exactly that and pressing Test CLI started
    three processes.
    """
    for probe in VERSION_PROBES:
        result = _invoke(probe, timeout=20)
        if not result["success"]:
            continue

        version = "0.0.0"
        found = _VERSION.search(result["output"] or "")
        if found:
            version = found.group(1)
        else:
            data = result.get("json")
            if isinstance(data, dict):
                for key in ("version", "cliVersion", "unityVersion"):
                    if data.get(key):
                        version = str(data[key])
                        break

        return {"answered": True, "probe": " ".join(probe), "version": version}

    return {"answered": False, "probe": "", "version": "0.0.0"}


def cli_version() -> str:
    """The CLI's version, or "0.0.0" when it will not say.

    A CLI that answers neither probe is still usable; it just has no
    version to show, and 0.0.0 says that honestly.
    """
    return _probe()["version"]


def _named_commands(data) -> List[dict]:
    """Pull command descriptions out of whatever `list --json` returned.

    The shape is not verified, so several plausible ones are accepted:
    a bare list, {"commands": [...]}, and a grouping such as
    {"builtin": [...], "pipeline": [...], "custom": [...]} -- which is
    what the task's three categories would most naturally look like.
    Entries may be strings or objects.

    Anything unrecognisable yields nothing rather than a guess.
    """
    found: List[dict] = []

    def take(entry, group: str) -> None:
        if isinstance(entry, str):
            name = entry.strip()
            if name:
                found.append({"name": name, "label": "", "group": group})
            return
        if not isinstance(entry, dict):
            return

        name = str(entry.get("name") or entry.get("command")
                   or entry.get("id") or "").strip()
        if not name:
            return
        label = str(entry.get("label") or entry.get("description")
                    or entry.get("summary") or "").strip()
        found.append({"name": name, "label": label,
                      "group": str(entry.get("group") or group)})

    if isinstance(data, list):
        for entry in data:
            take(entry, "builtin")
        return found

    if not isinstance(data, dict):
        return found

    # The real shape, confirmed against the CLI: a success envelope with
    # the tools under data.tools, each carrying name, description and
    # group. 142 of them on a connected Editor.
    payload = data.get("data")
    if isinstance(payload, dict) and isinstance(payload.get("tools"), list):
        for entry in payload["tools"]:
            take(entry, str(entry.get("group") or "built-in")
                 if isinstance(entry, dict) else "built-in")

    if isinstance(data.get("tools"), list):
        for entry in data["tools"]:
            take(entry, "built-in")

    if isinstance(data.get("commands"), list):
        for entry in data["commands"]:
            take(entry, "builtin")

    for group in ("builtin", "built_in", "pipeline", "custom", "registered"):
        entries = data.get(group)
        if isinstance(entries, list):
            for entry in entries:
                take(entry, group)

    return found


def list_commands() -> dict:
    """Ask the CLI what commands it has.

    Returns the raw result alongside the parsed commands, so a caller
    that got nothing can show the user what the CLI actually printed
    instead of an empty list and no explanation.
    """
    # No --project here either. The CLI rejects it outright -- measured:
    # "error: unknown option '--project'" -- and the project reaches
    # every command through UNITY_PROJECT_PATH, which _invoke sets.
    result = _invoke(list(_list_args()), timeout=60)
    if not result["success"]:
        return {"success": False, "commands": [], "error": result["error"],
                "output": result["output"]}

    commands = _named_commands(result.get("json"))
    if not commands:
        return {"success": False, "commands": [], "output": result["output"],
                "error": ("The CLI answered, but nothing in its output looked "
                          "like a list of commands.")}

    return {"success": True, "commands": commands, "output": result["output"],
            "error": None}


def command_record(command: dict) -> dict:
    """One discovered command, in the shape the registry stores.

    The template is the subcommand and nothing else. The program, the
    project and the mode are added at run time from settings, so a
    template can never decide which binary starts.

    The id is the name, so two commands reported under the same name --
    a builtin `build` and a pipeline `build`, say -- collapse into one
    entry rather than appearing twice. Whether the real CLI can do that
    is unknown; if it turns out it can, the fix is to include the group
    in the id, and the merge already refuses to overwrite the first.
    """
    name = str(command.get("name") or "").strip()
    label = str(command.get("label") or "").strip()
    group = str(command.get("group") or "builtin").strip()

    # `unity list` reports EDITOR tools, which are run as
    # `unity cmd <name>` -- not as top-level subcommands. Storing the
    # bare name as the template would have produced `unity create_scene`,
    # which is not a command the CLI has.
    return {
        "id": f"{COMMAND_PREFIX}{name}",
        "name": name,
        "type": COMMAND_TYPE,
        "plugin": PLUGIN_ID,
        "label": label or f"unity cmd {name}",
        "command": f"cmd {name}",
        "args": [],
        "group": group,
        "discovered": True,
        "enabled": False,
    }


def discover_commands() -> dict:
    """Every command the CLI reports, ready to merge into the registry."""
    listing = list_commands()
    records = [command_record(command) for command in listing["commands"]]

    logger.info("unity cli reported %d command(s)", len(records))
    return {"success": listing["success"], "found": records,
            "error": listing.get("error"), "output": listing.get("output", "")}


# ======================================================
# Running one
# ======================================================

def split_arguments(text: str) -> List[str]:
    """Split a typed command line into arguments.

    NOT shlex. shlex.split(posix=True) treats a backslash as an escape,
    so "D:\\Users\\William" comes back as DUsersWilliam -- and every path
    on this machine is a Windows path. posix=False keeps the quotes in
    the tokens instead of removing them.

    So: split on whitespace, respect double quotes, and never touch a
    backslash. That is the whole grammar a command line needs here, and
    it is the one that does not corrupt paths.
    """
    tokens: List[str] = []
    current: List[str] = []
    quoted = False

    for character in str(text or ""):
        if character == '"':
            quoted = not quoted
            continue
        if character.isspace() and not quoted:
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(character)

    if current:
        tokens.append("".join(current))
    return tokens


# Words that mean the line is a sentence about Unity rather than a
# command to Unity. "unity is a game engine" must never be executed.
_PROSE_AFTER_UNITY = frozenset({
    "is", "was", "are", "were", "be", "been", "can", "could", "does", "do",
    "did", "has", "have", "had", "will", "would", "should", "might", "must",
    "and", "or", "but", "the", "a", "an", "in", "on", "at", "for", "with",
    "to", "from", "about", "as", "by", "of", "that", "this", "it", "its",
    "seems", "looks", "says", "means", "gives", "needs", "uses", "supports",
    "requires", "keeps", "works", "runs", "lets", "makes", "projects",
})

# How many words a command line may be when it carries no flags. Real
# ones are short -- "unity env", "unity pipeline list" -- and the cap is
# what stops a sentence like "unity projects are stored in a folder"
# being read as the command `projects`.
_MAX_BARE_TOKENS = 3


def parse_invocation(text: str) -> Optional[dict]:
    """Read a typed line as a Unity CLI invocation, or decide it is not one.

    Deliberately narrow, because a false positive turns a sentence into
    a command.

    WHY THIS IS NOT "IS IT A REGISTERED COMMAND"
    The first version asked the registry, and only treated a bare
    `unity <word>` as a command when the CLI had already reported that
    word. That was wrong in the way that matters: `unity env` and
    `unity pipeline list` are exactly what a person types FIRST, before
    anything has been registered, and both fell through to the language
    model -- which duly routed them to phi-3-mini and answered with
    prose and a web search. A command that only counts once you have
    already discovered it cannot be the one that discovers things.

    So the test is now about the shape of the line, not about what
    happens to be in the registry:

      * anything with a flag is a command line, whatever its length
      * otherwise at most three words, no question mark, and the word
        after `unity` must not be one that starts a sentence

    Returns {"command": ..., "args": [...]} or None.
    """
    tokens = split_arguments(text)
    if len(tokens) < 2 or tokens[0].strip().lower() != "unity":
        return None

    command = tokens[1].strip()

    # A bare flag is still an invocation -- `unity --version` is a thing
    # a person types and expects to run.
    if command.startswith("-"):
        if not re.fullmatch(r"--?[A-Za-z][\w.-]*", command):
            return None
        return {"command": command, "args": tokens[2:]}

    if not re.fullmatch(r"[A-Za-z][\w.-]*", command):
        return None

    rest = tokens[2:]
    if any(part.startswith("-") for part in rest):
        return {"command": command, "args": rest}

    if command.lower() in _PROSE_AFTER_UNITY:
        return None
    if "?" in text or len(tokens) > _MAX_BARE_TOKENS:
        return None

    return {"command": command, "args": rest}


def build_invocation(record: dict, args: Optional[Sequence[str]] = None) -> List[str]:
    """The arguments for one command, in order.

    Settings first, then the caller's, so a user argument overrides a
    default rather than being silently overridden by it -- most CLIs
    take the last occurrence of a repeated flag.

    Only ever arguments. The executable is added by _invoke from the
    configured path, which is what stops a template naming a program.
    """
    template = str(record.get("command") or record.get("name") or "").strip()
    arguments: List[str] = template.split() if template else []

    # The project is NOT passed as a flag. --project does not exist;
    # --project-path exists on some commands and `test`/`build` take a
    # positional instead. UNITY_PROJECT_PATH covers all of them and is
    # set in _invoke's environment.
    #
    # --mode belongs to `test`. Appending it to everything, which an
    # earlier version did, would have made every other command fail on
    # an unknown option.
    if arguments and arguments[0] == "test":
        mode = _plugin_setting(FIELD_MODE)
        if mode in MODES:
            arguments += ["--mode", mode]

    for stored in (record.get("args") or []):
        text = str(stored).strip()
        if text:
            arguments.append(text)

    for extra in (args or []):
        text = str(extra).strip()
        if text:
            arguments.append(text)

    return arguments


def run_invocation(command: str, args: Optional[Sequence[str]] = None,
                   *, on_output: Optional[Callable[[str, str], None]] = None) -> dict:
    """Run a subcommand the user typed, without consulting the registry.

    WHY THIS IS NOT GATED THE WAY run_command IS
    run_command refuses anything not registered and enabled, and that is
    right for a tile and for a model: neither of those is the user, and
    "the CLI mentioned it" is not the same as "you asked for it".

    A line the user typed is different. They typed it. Requiring it to
    have been discovered first is friction with no safety behind it --
    they could open a terminal and run the same thing -- and it made
    `unity env`, the command you would type FIRST, impossible.

    The consent here is the plugin being enabled, which is a deliberate
    act on its own page. Beyond that this runs what was asked for and
    reports what happened.
    """
    from backend.plugins import plugin_settings

    plugin = plugin_settings.load_plugins().get(PLUGIN_ID) or {}
    if not plugin or plugin.get("dismissed", False):
        return {"success": False, "output": "", "json": None,
                "error": "The Unity CLI plugin is not installed."}
    if not plugin.get("enabled", False):
        return {"success": False, "output": "", "json": None,
                "error": "The Unity CLI plugin is switched off."}

    arguments = build_invocation({"command": command, "args": []}, args)
    project = _plugin_setting(FIELD_PROJECT)

    result = _invoke(arguments, on_output=on_output,
                     cwd=project if project and Path(project).is_dir() else None)

    return {
        "success": result["success"],
        "output": result["output"],
        "json": result["json"],
        "error": result["error"],
        "code": result.get("code"),
        # Carried through, not rebuilt away: a refusal that arrives
        # here as an ordinary failure gets reported as "Ran ... and it
        # failed", which is the one thing it must not say.
        "ran": result.get("ran", True),
        "invocation": arguments,
    }


def run_command(command_id: str, args: Optional[Sequence[str]] = None,
                *, on_output: Optional[Callable[[str, str], None]] = None) -> dict:
    """Run one registered command and report the result.

    Refuses a command that is not registered, and one that is
    registered but switched off. Discovery lists what a CLI could do;
    being listed is not permission to run it.
    """
    from backend.plugins import plugin_settings

    record = plugin_settings.load_plugins().get(str(command_id or ""))
    if record is None or record.get("type") != COMMAND_TYPE:
        return {"success": False, "output": "", "json": None,
                "error": f"{command_id!r} is not a Unity CLI command."}
    if record.get("dismissed", False):
        return {"success": False, "output": "", "json": None,
                "error": f"{command_id!r} was removed."}
    if not record.get("enabled", False):
        return {"success": False, "output": "", "json": None,
                "error": (f"{record.get('name') or command_id} is not enabled. "
                          f"Enable it before running it.")}

    arguments = build_invocation(record, args)
    project = _plugin_setting(FIELD_PROJECT)

    result = _invoke(arguments, on_output=on_output,
                     cwd=project if project and Path(project).is_dir() else None)

    return {
        "success": result["success"],
        "output": result["output"],
        "json": result["json"],
        "error": result["error"],
        "code": result.get("code"),
        # Carried through, not rebuilt away: a refusal that arrives
        # here as an ordinary failure gets reported as "Ran ... and it
        # failed", which is the one thing it must not say.
        "ran": result.get("ran", True),
        "invocation": arguments,
    }


# How many lines of a command's output belong in a chat message.
#
# A Unity build prints six hundred: licensing handshakes, package
# registrations, domain reload profiling, shader imports. One of them
# says why the build failed, and it is near the end. Pasting all of it
# into the conversation buries the answer, and it also goes into the
# history the next turn reads.
#
# The terminal view keeps two thousand. This is the chat, which is a
# different thing with a different job.
MAX_REPLY_LINES = 40


def _readable_output(output: str) -> str:
    """The part of a command's output worth putting in a message.

    Keeps the END, because that is where a failure says what happened
    -- Unity's build log opens with licensing and package resolution
    and closes with the exception. Says plainly how much was left out,
    so a truncated log never reads as a whole one.
    """
    lines = (output or "").strip().splitlines()
    if len(lines) <= MAX_REPLY_LINES:
        return "\n".join(lines)

    hidden = len(lines) - MAX_REPLY_LINES
    kept = lines[-MAX_REPLY_LINES:]
    return (f"[{hidden} earlier line(s) not shown -- the end is where the "
            f"reason usually is]\n" + "\n".join(kept))


def answer_invocation(invocation: dict) -> dict:
    """Do what a typed Unity CLI line asked for, and report what happened.

    Returns {"ran": bool, "text": str} -- text being what to say to the
    user. Every branch that did not run something says so in its first
    sentence, because the failure this exists to prevent is a report of
    work that never happened.

    WHY THIS IS NOT A MODEL'S JOB
    A 12B asked to "run" a command it has no way to run does not say it
    cannot. It writes the sentence that usually follows such a request:
    "Unity project created at D:\\...\\ARIA_TestProject". Confident,
    well-formed, and false -- and the user then goes looking for a
    folder that was never made. The same reasoning already keeps
    workspace queries away from the model; a command invocation has an
    authority behind it too, and this is it.
    """
    from backend.plugins import plugin_settings

    command = str(invocation.get("command") or "")
    args = list(invocation.get("args") or [])
    spoken = " ".join(["unity", command, *args])

    plugin = plugin_settings.load_plugins().get(PLUGIN_ID)
    if plugin is None or plugin.get("dismissed", False):
        return {"ran": False, "text": (
            f"I did not run `{spoken}`, and nothing was created.\n\n"
            "The Unity CLI plugin is not installed. Open Plugins and "
            "press Rescan for Plugins to look for it.")}

    if not plugin.get("enabled", False):
        return {"ran": False, "text": (
            f"I did not run `{spoken}`, and nothing was created.\n\n"
            "The Unity CLI plugin is installed but switched off. Enable "
            "it on its page under Plugins first.")}

    # No registry check. See run_invocation: the user typed this, and
    # `unity env` -- the first thing anybody types -- is not registered
    # until something has already run `unity list`.
    outcome = run_invocation(command, args)

    if outcome["success"]:
        body = _readable_output(outcome["output"])
        return {"ran": True, "text": (
            f"Ran `{spoken}`.\n\n" + (body if body else "It finished and printed nothing."))}

    reason = outcome.get("error") or "it failed"
    body = _readable_output(outcome["output"])

    # Refused before starting, rather than started and failed. The
    # difference matters to whoever reads it: one is something to fix
    # about the command, the other about the machine -- and "Ran X and
    # it failed" for a command that never started is the same small
    # untruth as reporting a project created that was not.
    if outcome.get("ran") is False:
        return {"ran": False, "text": f"I did not run `{spoken}`.\n\n{reason}"}

    return {"ran": True, "text": (
        f"Ran `{spoken}` and it failed: {reason}\n\n"
        + (body if body else "It printed nothing."))}


# ======================================================
# What a model may ask for
# ======================================================
#
# A FIXED LIST, NOT A COMMAND FIELD
# The obvious design is one tool taking a command string. It is also the
# wrong one: it hands a 12B a way to run any subcommand of a program,
# and the difference between `unity test` and something destructive
# becomes a matter of what the model typed.
#
# So each tool is one operation with a fixed subcommand. The model
# chooses WHICH tool, never what the command line says, and a tool that
# is not in this table cannot be called at all. Adding one is an entry
# here, deliberately.
#
# The subcommands come from the task's own list. They are UNVERIFIED
# against a real Unity CLI -- see the module docstring -- so a wrong one
# fails loudly with the CLI's own message rather than silently doing
# something else.
UNITY_TOOLS = (
    {
        "name": "unity_env",
        "command": "env",
        "description": "Report the Unity CLI's environment: versions, paths "
                       "and the project it is pointed at.",
        "parameters": {},
        "flags": (),
    },
    {
        "name": "unity_pipeline_list",
        # `unity pipeline list` after all. An earlier version mapped this
        # to `unity list` because `pipeline --help` prints no Commands
        # section -- so I concluded it had no subcommands. It does; the
        # help simply does not list them, and the CLI's own error text
        # recommends "unity pipeline list" by name. Running it settled
        # it, which reading the help had not.
        #
        # The two are different questions. `pipeline list` reports the
        # Editor instances and whether each has Pipeline running;
        # `list` reports the tools inside one of them. This tool is the
        # first, because that is what its name says.
        "command": "pipeline",
        "description": "List the Unity Editor instances and whether each "
                       "has the Pipeline server running.",
        "parameters": {},
        "flags": ("list",),
    },
    {
        "name": "unity_list_tools",
        "command": "list",
        "description": "List the tools the connected Unity Editor has "
                       "registered through the Pipeline package.",
        "parameters": {},
        "flags": ("--json",),
    },
    {
        "name": "unity_cmd_create_scene",
        "command": "cmd",
        "description": "Create a new scene in the configured Unity project.",
        "parameters": {
            "name": {"type": "string", "required": True,
                     "description": "Name for the new scene, e.g. TestScene"},
        },
        "flags": ("create_scene",),
    },
    {
        "name": "unity_test",
        "command": "test",
        "description": "Run the Unity project's tests.",
        "parameters": {
            "mode": {"type": "string", "required": False,
                     "description": "EditMode or PlayMode. Omit for the default."},
        },
        "flags": (),
    },
    {
        "name": "unity_build",
        "command": "build",
        "description": "Build the Unity project.",
        "parameters": {
            "target": {"type": "string", "required": False,
                       "description": "Build target, e.g. Win64"},
            "output": {"type": "string", "required": False,
                       "description": "Output directory, e.g. Build/"},
        },
        "flags": (),
    },
)


def run_tool(tool_name: str, arguments: Optional[dict] = None) -> dict:
    """Run one of the tools above on behalf of a model.

    Two things make this safe to expose. The subcommand is fixed by the
    tool, so a model cannot compose a command line; and each named
    parameter becomes one `--flag value` pair, so a value cannot smuggle
    in a second argument -- it is a single argv entry either way.
    """
    spec = next((tool for tool in UNITY_TOOLS if tool["name"] == tool_name), None)
    if spec is None:
        return {"success": False, "output": "", "json": None,
                "error": f"{tool_name!r} is not a Unity CLI tool."}

    supplied = dict(arguments or {})
    args: List[str] = list(spec["flags"])

    for field_name, rules in spec["parameters"].items():
        value = supplied.get(field_name)
        if value is None or str(value).strip() == "":
            if rules.get("required"):
                return {"success": False, "output": "", "json": None,
                        "error": f"{tool_name} needs {field_name}."}
            continue
        args += [f"--{field_name}", str(value)]

    return run_invocation(spec["command"], args)


def test_cli() -> dict:
    """Whether the Unity CLI is there and answers.

    Reports which probe worked, because "unity --version answered" and
    "unity env answered" are different facts about an unverified tool
    and the difference is worth seeing once.
    """
    try:
        executable = cli_path()
    except UnityCliUnavailable as error:
        return {"ok": False, "message": str(error)}

    answer = _probe()
    if not answer["answered"]:
        return {"ok": False,
                "message": (f"Found {executable}, but it answered neither "
                            f"`--version` nor `env`. It may not be the Unity CLI.")}

    version = answer["version"]
    return {"ok": True,
            "version": version,
            "message": (f"{executable.name} answered `{answer['probe']}`"
                        + (f" with version {version}." if version != "0.0.0" else "."))}
