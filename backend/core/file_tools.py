"""ARIA Lite Phase 9.2 - the handlers behind read_file, edit_file and run_tests.

The first tools in this codebase that touch the user's disk. Everything here
is written around one fact about where they run: tool_registry executes
handlers through run_in_sandbox, and that sandbox is a worker thread in this
same process. It enforces a wall-clock timeout and watches memory. It does
not isolate the filesystem, and an `open(path, "w")` inside it writes to the
real disk exactly as it would anywhere else.

So the containment is here, and it is the point of the module:

    Every path is resolved and checked against a workspace root before it
    is opened. Resolution happens first, so "../../.ssh/id_rsa" and a
    symlink pointing outside are both rejected by the same check rather
    than by a string test that either could defeat.

    edit_file previews by default. Writing is opt-in per call, so a
    mis-planned invocation costs a diff rather than a file. Rewriting a
    file with what it already contains is reported and skipped, which makes
    a repeated edit idempotent rather than merely harmless.

    run_tests never takes a command from its caller. The command is
    configuration; the argument only chooses which tests, and is rejected
    if it looks like anything but a path or a test expression. No shell is
    involved at any point.

None of these are theoretical. A read tool with no root check is an
arbitrary file disclosure primitive, a write tool with no preview turns one
bad plan step into data loss, and a test runner that accepts its own command
line is remote code execution wearing a different name.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "DEFAULT_TEST_COMMAND",
    "ENV_TEST_COMMAND",
    "ENV_WORKSPACE",
    "MAX_READ_BYTES",
    "MAX_WRITE_BYTES",
    "WorkspaceError",
    "edit_file",
    "read_file",
    "resolve_in_workspace",
    "run_tests",
    "workspace_root",
]

# Where the tools are allowed to operate. Defaults to the working directory,
# which is the project root when the backend is started normally.
ENV_WORKSPACE = "ARIA_TOOL_WORKSPACE"

# The test command. Configuration, never an argument -- see the module
# docstring. Split on whitespace, run without a shell.
ENV_TEST_COMMAND = "ARIA_TEST_COMMAND"
DEFAULT_TEST_COMMAND = (sys.executable, "-m", "pytest", "-q")

# A read returns at most this much text, and a write refuses beyond it.
# Bounds what one tool call can pull into a prompt or commit to disk.
MAX_READ_BYTES = 200_000
MAX_WRITE_BYTES = 1_000_000

# What a test scope may look like: a path, a node id, or a -k expression.
# Anything with a shell metacharacter in it is refused rather than escaped,
# because there is no shell to escape for and a scope that needs one is a
# scope that is trying to be a command.
_SCOPE_ALLOWED = re.compile(r"^[A-Za-z0-9_./:\- \[\]=]*$")

_TEST_TIMEOUT_SECONDS = 600


class WorkspaceError(ValueError):
    """A path that resolved outside the workspace, or could not be read."""


def workspace_root() -> Path:
    """The directory the file tools are confined to.

    Read on every call rather than cached at import: a test fixture that
    points the workspace at a tmp_path must take effect without reloading
    the module, and a cached root is a root that silently ignores it.
    """
    configured = os.environ.get(ENV_WORKSPACE)
    return Path(configured).resolve() if configured else Path.cwd().resolve()


def resolve_in_workspace(path: str, root: Path | None = None) -> Path:
    """Resolve `path` and confirm it is inside the workspace.

    strict=False so a file that does not exist yet still resolves -- writing
    a new file is legitimate -- while symlinks and "..' segments are
    flattened before the check, which is what makes the check meaningful.

    `root` names which workspace to check against, and defaults to the
    active one. It exists for multi-workspace operations, which have to
    confine a path to a project that is not the one the environment
    currently points at. Parameterised rather than reimplemented
    elsewhere: one definition of "inside", and every caller gets the
    resolve-then-compare order that makes it mean anything.
    """
    root = Path(root).resolve() if root else workspace_root()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate

    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise WorkspaceError(
            f"Path is outside the workspace: {path!r} resolves to {resolved}"
        )
    return resolved


def _relative(resolved: Path, root: Path | None = None) -> str:
    """The path as the user would recognise it, for summaries and results."""
    try:
        return str(resolved.relative_to(root or workspace_root())).replace("\\", "/")
    except ValueError:
        return str(resolved)


# ======================================================
# read_file
# ======================================================
def read_file(path: str, root: Path | None = None) -> dict:
    """Read a text file from inside the workspace.

    Decoded as UTF-8 with replacement rather than strict: a tool that raises
    on one bad byte is a tool that cannot read half the log files it will be
    pointed at, and the replacement character is visible in the output.
    """
    resolved = resolve_in_workspace(path, root=root)

    # Staged first. A model that edits a file and then reads it back
    # should see its own edit; reading the project's copy instead is how
    # it concludes the change did not happen and does it again.
    #
    # Imported lazily because ghost_workspace imports this module -- and
    # deliberately here rather than inside resolve_in_workspace, so the
    # confinement check still runs against the path the caller asked for.
    # The reported path stays the project one: what the reader wants to
    # know is which file this is, not which copy of it was on disk.
    try:
        from backend.core.ghost_workspace import get_effective_file

        effective = get_effective_file(str(resolved), root)
    except Exception:  # pragma: no cover - staging must never break a read
        effective = resolved

    if not effective.is_file():
        raise WorkspaceError(f"Not a file: {_relative(resolved, root)}")

    size = effective.stat().st_size
    data = effective.read_bytes()[:MAX_READ_BYTES]
    text = data.decode("utf-8", errors="replace")

    return {
        "path": _relative(resolved, root),
        "text": text,
        "bytes": size,
        "truncated": size > MAX_READ_BYTES,
    }


# ======================================================
# edit_file
# ======================================================
def _unified_preview(before: str, after: str, name: str, context: int = 3) -> str:
    """A unified diff of a proposed change, for a human to look at."""
    import difflib

    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
            n=context,
        )
    )


def edit_file(path: str, content: str, confirm: bool = False, root: Path | None = None) -> dict:
    """Replace a file's contents, previewing unless told to apply.

    `root` names which workspace to confine to, defaulting to the active
    one. Multi-workspace staging needs it: a write into a project that is
    not the one the environment points at is legitimate and has to be
    checked against THAT project's root, not the current one. Without it
    every non-primary workspace's staging read as "outside the
    workspace".

    confirm defaults to False, so the ordinary call computes the diff and
    writes nothing. That is the guard: a plan step that names the wrong file
    or the wrong content costs a preview, and applying is a second, explicit
    decision by whoever is driving the tool.

    Writing the same bytes that are already there is skipped and reported as
    unchanged. That makes a repeated invocation idempotent in the strong
    sense -- the second call does not touch the file's mtime, so nothing
    downstream sees a change that did not happen.
    """
    resolved = resolve_in_workspace(path, root=root)
    name = _relative(resolved, root)

    if resolved.exists() and not resolved.is_file():
        raise WorkspaceError(f"Not a file: {name}")

    encoded = str(content).encode("utf-8")
    if len(encoded) > MAX_WRITE_BYTES:
        raise WorkspaceError(
            f"Refusing to write {len(encoded)} bytes to {name}; the limit is {MAX_WRITE_BYTES}"
        )

    existed = resolved.is_file()
    before = resolved.read_text(encoding="utf-8", errors="replace") if existed else ""
    diff = _unified_preview(before, str(content), name)

    if existed and before == str(content):
        return {
            "path": name, "applied": False, "changed": False,
            "reason": "unchanged", "diff": "", "created": False,
        }

    if not confirm:
        return {
            "path": name, "applied": False, "changed": True,
            "reason": "preview_only", "diff": diff, "created": not existed,
        }

    if not resolved.parent.is_dir():
        raise WorkspaceError(f"Directory does not exist: {_relative(resolved.parent, root)}")

    # newline="" so writing is the exact inverse of reading. read_file
    # decodes raw bytes and keeps whatever line endings the file had;
    # write_text's default translates "\n" to os.linesep on Windows. A
    # read-modify-write round trip therefore turned "\r\n" into "\r\r\n"
    # and doubled every line ending -- which the tool_orchestrator's
    # rollback was the first caller to exercise, restoring a "prior"
    # version of a file that did not match the prior version.
    #
    # Also makes an edit deterministic across platforms: content written
    # with "\n" stays "\n", rather than depending on which machine the
    # assistant happened to be running on.
    resolved.write_text(str(content), encoding="utf-8", newline="")
    logger.info("edit_file applied to %s (%d bytes)", name, len(encoded))
    return {
        "path": name, "applied": True, "changed": True,
        "reason": "written", "diff": diff, "created": not existed,
    }


# ======================================================
# run_tests
# ======================================================
def _test_command() -> list[str]:
    configured = os.environ.get(ENV_TEST_COMMAND)
    if configured:
        return configured.split()
    return list(DEFAULT_TEST_COMMAND)


def run_test_files(paths, timeout: int | None = None) -> dict:
    """Run the configured test command over specific test files.

    run_tests takes ONE scope and appends it as a single argument, which
    is right for "narrow this to a folder" and useless for "run these
    nine files": pytest would receive one argument containing spaces and
    look for a path of that name.

    The same safety properties hold and for the same reasons. There is no
    shell. The command comes from configuration, never from the caller.
    Each path is resolved inside the workspace and must already exist, so
    a caller cannot turn an argument into an option -- "-x" is not a file
    -- and cannot reach outside the project.

    Not registered as a tool. Selecting which suites to run is ARIA
    checking her own work; letting a model choose the argument list would
    hand it the one part of this that is not validated by construction.
    """
    root = workspace_root()

    selected = []
    for path in paths or []:
        resolved = resolve_in_workspace(str(path), root=root)
        if not resolved.is_file():
            raise WorkspaceError(f"Not a test file: {path!r}")
        selected.append(resolved.relative_to(root).as_posix())

    if not selected:
        raise WorkspaceError("No test files were selected")

    base = _test_command()

    # Checking that a change does no harm must not itself be a change.
    # Measured: a verification run left .pytest_cache/ and __pycache__/
    # in the user's project -- ARIA introducing files nobody asked for,
    # during the step whose whole purpose is leaving the project alone.
    #
    # Only added when the configured command is actually pytest, since a
    # different runner would reject the flag and the run would fail for a
    # reason that has nothing to do with the tests.
    if any("pytest" in str(part) for part in base):
        base = [*base, "-p", "no:cacheprovider"]

    command = [*base, *selected]

    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    try:
        completed = subprocess.run(
            command,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout or _TEST_TIMEOUT_SECONDS,
            shell=False,
            env=environment,
        )
    except FileNotFoundError as error:
        raise WorkspaceError(f"Test command not found: {command[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise WorkspaceError(
            f"Tests timed out after {timeout or _TEST_TIMEOUT_SECONDS}s"
        ) from error

    output = (completed.stdout or "") + (completed.stderr or "")
    return {
        "command": " ".join(command),
        "files": selected,
        "exit_code": completed.returncode,
        "passed": completed.returncode == 0,
        "output": output[-MAX_READ_BYTES:],
    }


def run_tests(scope: str = "") -> dict:
    """Run the configured test command, optionally narrowed to `scope`.

    The command comes from configuration and the scope is appended as one
    argument. Nothing the caller passes can become a command, an option or a
    second command: there is no shell, the argument list is built here, and a
    scope containing anything but path and test-expression characters is
    refused outright.
    """
    scope = " ".join(str(scope or "").split())
    if scope and not _SCOPE_ALLOWED.match(scope):
        raise WorkspaceError(f"Test scope contains unsupported characters: {scope!r}")

    command = _test_command()
    if scope:
        command = [*command, scope]

    root = workspace_root()
    try:
        completed = subprocess.run(
            command,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=_TEST_TIMEOUT_SECONDS,
            shell=False,
        )
    except FileNotFoundError as error:
        raise WorkspaceError(f"Test command not found: {command[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise WorkspaceError(
            f"Test command timed out after {_TEST_TIMEOUT_SECONDS}s"
        ) from error

    output = (completed.stdout or "") + (completed.stderr or "")
    return {
        "command": " ".join(command),
        "scope": scope,
        "exit_code": completed.returncode,
        "passed": completed.returncode == 0,
        "output": output[-MAX_READ_BYTES:],
    }
