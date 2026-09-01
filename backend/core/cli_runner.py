"""ARIA Lite - running an external command-line program, once.

Extracted from unity_cli_engine, where all of it was written and none
of it was ever about Unity. Streaming, timeouts, killing a process
tree, reading JSON out of chatty output: a second integration wanting
the same behaviour should get the same code, not a copy of it.

That is not a hypothetical here. This repo already carries five
modules with "unity" in the name because the previous four were each
easier to copy than to share, and adding Blender by copying this file
would have made it six.

SAFETY
------
shell=False, always, and argv is a list. Nothing in an argument can
start a second command, so there is no quoting to get wrong. The
program run is whatever the caller resolved -- this module never picks
one, and never takes one from a string it was handed.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "extract_json",
    "kill_tree",
    "run",
    "split_arguments",
]

DEFAULT_TIMEOUT_SECONDS = 120


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


def extract_json(text: str):
    """The JSON in this output, or None.

    A program that prints a banner before its JSON is common enough to
    be worth handling: this takes the whole thing if it parses, and
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


def kill_tree(process: "subprocess.Popen") -> None:
    """End the command and everything it started.

    process.kill() ends only the process ARIA started. That is not
    enough: a .cmd shim is a cmd.exe that spawns the real tool, and a
    Unity build spawns compilers and importers of its own. Those
    children inherit the stdout pipe, so killing the parent alone
    leaves the pipe open and the read loop blocked -- the timeout
    fires, the result is correct, and the call still does not return
    until the child finishes on its own. Measured: a one-second
    timeout took twenty-nine seconds to come back.

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
        logger.debug("could not end the process tree", exc_info=True)

    try:
        process.kill()
    except Exception:  # pragma: no cover - it may have just exited
        logger.debug("could not kill the process", exc_info=True)


def run(executable, arguments: Sequence[str], *,
        on_output: Optional[Callable[[str, str], None]] = None,
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,
        env_extra: Optional[dict] = None,
        label: str = "cli") -> dict:
    """Run one program and report what happened.

    Streams stdout line by line through on_output as it arrives, so a
    build that takes four minutes shows something for the first three.
    stderr is read after the process ends rather than concurrently:
    interleaving two pipes correctly needs threads, and a wrong
    interleaving is worse than a clearly separated one.

    Never raises for anything the program does. A crash, a timeout and
    a non-zero exit are all answers, and each says which it was.
    """
    argv = [str(executable), *[str(part) for part in arguments]]
    limit = timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS

    environment = dict(os.environ)
    if env_extra:
        environment.update({str(k): str(v) for k, v in env_extra.items()})

    logger.info("%s: %s", label, " ".join(argv))

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
            # Its own process group on POSIX, so kill_tree can end the
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
    # or silently stuck, and those are both things a build does.
    expired = threading.Event()

    def give_up() -> None:
        expired.set()
        kill_tree(process)

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
    parsed = extract_json(output)

    if code != 0:
        # The output is kept, not thrown away. A failing build's reason
        # is in the output, and an error message that replaced it with
        # "exit code 1" would be the least useful thing ARIA could say.
        return {"success": False, "code": code, "output": output,
                "json": parsed,
                "error": (errors.strip().splitlines() or [f"Exited with code {code}."])[0]}

    return {"success": True, "code": code, "output": output,
            "json": parsed, "error": None}
