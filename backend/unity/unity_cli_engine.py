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

WHAT IS VERIFIED HERE AND WHAT IS NOT
-------------------------------------
Not verified: the Unity CLI's actual surface. There is no Unity on the
machine this was written on -- the discovery scan checked and found
none -- so `unity list --json`, `unity --version` and `unity env` are
the task's description of the tool, not something confirmed against
it. Every subcommand and flag is therefore a named constant, and the
important ones are environment-overridable, so correcting them is a
setting rather than a patch.

Verified: everything on this side of the process boundary. The argv
construction, the timeout, the exit-code handling, the streaming and
the JSON parsing are all exercised by tests against a real subprocess
running a real script -- a fake `unity` written by the test, so what is
tested is this module rather than Unity's.

The JSON parser deliberately accepts several plausible shapes for
`list --json` rather than one guessed shape. When the real format is
known, the others cost nothing and the right one already works.

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

# The subcommands. UNVERIFIED -- see the module docstring. Overridable
# because a wrong guess here should cost a setting, not a release.
ENV_LIST_ARGS = "ARIA_UNITY_CLI_LIST_ARGS"
LIST_ARGS = ("list", "--json")

# Tried in order, and the first that answers wins. The task named both
# and I cannot check which this CLI has, so it asks rather than assumes.
VERSION_PROBES = (("--version",), ("env",))

DEFAULT_TIMEOUT_SECONDS = 120

# A version-looking token anywhere in the output of --version.
_VERSION = re.compile(r"(\d+(?:\.\d+)+[A-Za-z0-9.]*)")


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


def _timeout() -> int:
    """How long a command may run before it is given up on."""
    raw = os.environ.get(ENV_TIMEOUT)
    try:
        value = int(str(raw))
        return value if value > 0 else DEFAULT_TIMEOUT_SECONDS
    except (TypeError, ValueError):
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
    limit = timeout if timeout is not None else _timeout()

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
    project = _plugin_setting(FIELD_PROJECT)
    arguments = list(_list_args())
    if project:
        arguments += ["--project", project]

    result = _invoke(arguments, timeout=60)
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

    return {
        "id": f"{COMMAND_PREFIX}{name}",
        "name": name,
        "type": COMMAND_TYPE,
        "plugin": PLUGIN_ID,
        "label": label or f"unity {name}",
        "command": name,
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


def parse_invocation(text: str) -> Optional[dict]:
    """Read a typed line as a Unity CLI invocation, or decide it is not one.

    Deliberately narrow, because a false positive turns a sentence into
    a command. "unity is a game engine" begins with the same word as
    "unity build --target Android" and must not be treated the same way,
    so a line counts only when the word after `unity` looks like a
    subcommand AND either a flag follows or the subcommand is one the
    CLI actually reported.

    Returns {"command": ..., "args": [...]} or None.
    """
    tokens = split_arguments(text)
    if len(tokens) < 2 or tokens[0].strip().lower() != "unity":
        return None

    command = tokens[1].strip()

    # A bare flag is still an invocation -- `unity --version` is a thing
    # a person types and expects to run. It is included so that it
    # reaches an answer with an authority behind it rather than the
    # model, which would simply make one up.
    if command.startswith("-"):
        if not re.fullmatch(r"--?[A-Za-z][\w.-]*", command):
            return None
        return {"command": command, "args": tokens[2:]}

    if not re.fullmatch(r"[A-Za-z][\w.-]*", command):
        return None

    rest = tokens[2:]
    looks_like_a_command_line = any(part.startswith("-") for part in rest)

    if not looks_like_a_command_line:
        try:
            from backend.plugins import plugin_settings

            # Matched on the NAME, which is what a person types and
            # what the tile shows. The template is what gets run, and
            # Edit can change it -- a command renamed to `boom` behind
            # the scenes is still typed as `build`.
            known = {str(record.get("name") or record.get("command") or "")
                     for record in plugin_settings.list_commands(PLUGIN_ID)}
        except Exception:  # pragma: no cover - a lookup is not a parse
            known = set()
        if command not in known:
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

    project = _plugin_setting(FIELD_PROJECT)
    if project:
        arguments += ["--project", project]

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
        "invocation": arguments,
    }


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

    registered = plugin_settings.list_commands(PLUGIN_ID)
    # By name, for the same reason parse_invocation matches by name:
    # the name is the command's identity, the template is its behaviour,
    # and editing the second must not change what the first is called.
    match = next((record for record in registered
                  if str(record.get("name") or record.get("command")) == command), None)

    if match is None:
        known = ", ".join(sorted(str(r.get("name") or "") for r in registered))
        detail = (f"The commands I know about are: {known}."
                  if known else
                  "I have not asked the Unity CLI what commands it has yet -- "
                  "press Refresh Commands on the Plugins page.")
        return {"ran": False, "text": (
            f"I did not run `{spoken}`, and nothing was created.\n\n"
            f"`{command}` is not a Unity CLI command I have registered. {detail}")}

    if not match.get("enabled", False):
        return {"ran": False, "text": (
            f"I did not run `{spoken}`, and nothing was created.\n\n"
            f"`{command}` is registered but not enabled. Enable it on the "
            "Plugins page, then run it again.")}

    outcome = run_command(match["id"], args)

    if outcome["success"]:
        body = (outcome["output"] or "").strip()
        return {"ran": True, "text": (
            f"Ran `{spoken}`.\n\n" + (body if body else "It finished and printed nothing."))}

    reason = outcome.get("error") or "it failed"
    body = (outcome["output"] or "").strip()
    return {"ran": True, "text": (
        f"Ran `{spoken}` and it failed: {reason}\n\n"
        + (body if body else "It printed nothing."))}


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
