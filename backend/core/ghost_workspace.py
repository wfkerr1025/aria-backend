"""ARIA Lite - a staging area, so an edit is a proposal until it isn't.

Actions became live in the previous change, and with no ARIA_TOOL_WORKSPACE
set the workspace root is the project directory itself -- so "apply the
changes" wrote straight into the source tree. That is the right
confinement and the wrong default: an edit the user has seen only as a
sentence should not already be on disk where they keep their work.

So writes land here first:

    <workspace_root>/.aria_staging/<project>/<same relative path>

and reach the project only when the user says to commit. A staged file is
a proposal with a diff attached. Nothing else about the pipeline changes:
the same edit_file writes it, the same workspace confinement applies, and
the orchestrator's own rollback still unwinds a failed run.

Three decisions the spec left ambiguous, and how they are settled:

    A new file can be staged. "Validate the target exists in the project
    root" and "Create backend/api.py" cannot both hold, so what is
    validated is the PARENT directory: a staged file must land somewhere
    the project already has, which stops an invented path without
    stopping a new file.

    Commit is not a tool. It is a user command. If commit were on
    ACTION_TOOLS the model could emit one in its answer, and the rule
    that only the user authorises a commit would be enforced by nothing
    but the model's good manners. Consent is read from the user's words
    here for the same reason it is in action_plan, and shares that
    module's negation table so the two cannot drift.

    The staging directory lives inside the workspace. That puts it inside
    the project when the two are the same, which is why it is excluded
    from staging itself, from diffs, and from git. A staging area that
    could stage itself is a loop waiting to be found.
"""

from __future__ import annotations

import difflib
import shutil
from pathlib import Path

from backend.core import file_tools
from backend.core.action_plan import NEGATION_VETO, _normalize

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "STAGING_DIRNAME",
    "COMMIT_PHRASES",
    "DISCARD_PHRASES",
    "GhostError",
    "commit",
    "diff_all",
    "diff_for",
    "discard",
    "ghost_path_for",
    "project_root",
    "requests_commit",
    "requests_discard",
    "stage_path",
    "staged_files",
    "staging_root",
]

STAGING_DIRNAME = ".aria_staging"

COMMIT_PHRASES: tuple[str, ...] = (
    "commit the changes", "commit changes", "commit it", "commit this",
    "apply the changes", "apply the change", "apply it",
    "write it to the project", "save it to the project",
)

DISCARD_PHRASES: tuple[str, ...] = (
    "discard the changes", "discard changes", "discard it",
    "throw them away", "throw it away", "forget the changes",
    "undo the staged changes", "clear the staging",
)


class GhostError(RuntimeError):
    """A staging operation that must not be guessed at."""


def project_root() -> Path:
    """The real project: what a commit writes into.

    The workspace root, unless something separates them later. Kept as a
    function rather than a constant for the same reason workspace_root is
    one: a test that points it somewhere else must take effect without
    reloading the module.
    """
    return file_tools.workspace_root()


def staging_root() -> Path:
    """This project's ghost workspace."""
    root = project_root()
    return root / STAGING_DIRNAME / root.name


def _is_staging(path: Path) -> bool:
    staging_base = project_root() / STAGING_DIRNAME
    return staging_base == path or staging_base in path.parents


def _relative_to_project(path: str) -> Path:
    """The project-relative path, refusing anything outside the project.

    Reuses file_tools' confinement rather than re-deriving it: one
    definition of "inside", and it is the one enforced at write time.
    """
    resolved = file_tools.resolve_in_workspace(path)

    if _is_staging(resolved):
        # Already a ghost path. Staging it again would nest a staging
        # directory inside itself.
        raise GhostError(f"Refusing to stage a path already inside staging: {path!r}")

    try:
        return resolved.relative_to(project_root())
    except ValueError:  # pragma: no cover - resolve_in_workspace already refused
        raise GhostError(f"Path is outside the project: {path!r}")


def ghost_path_for(path: str) -> Path:
    """Where an edit to `path` is written instead."""
    return staging_root() / _relative_to_project(path)


def stage_path(path: str) -> str:
    """The ghost path for `path`, with its directory ready to write into.

    Validates the PARENT rather than the file: a staged file must land
    somewhere the project already has, which refuses an invented
    directory without refusing a new file. Creating "backend/api.py" is a
    legitimate edit; creating "made/up/tree/api.py" is a hallucinated
    path wearing an edit's clothes.
    """
    relative = _relative_to_project(path)
    parent = project_root() / relative.parent

    if not parent.is_dir():
        raise GhostError(
            f"Refusing to stage {path!r}: {relative.parent.as_posix()!r} is not a "
            f"directory in the project"
        )

    ghost = staging_root() / relative
    ghost.parent.mkdir(parents=True, exist_ok=True)
    logger.info("staging %s -> %s", relative.as_posix(), ghost)
    return str(ghost)


def staged_files() -> list[str]:
    """Project-relative paths with something staged, sorted."""
    root = staging_root()
    if not root.is_dir():
        return []
    return sorted(
        str(p.relative_to(root)).replace("\\", "/")
        for p in root.rglob("*")
        if p.is_file()
    )


def _read(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_bytes().decode("utf-8", errors="replace").splitlines(keepends=True)


def diff_for(relative: str) -> str:
    """A unified diff of one staged file against the project's copy.

    Deterministic: same inputs, same text, no timestamps in the header.
    A file that exists only in staging diffs against nothing, which is
    how a new file reads.
    """
    ghost = staging_root() / relative
    real = project_root() / relative

    return "".join(difflib.unified_diff(
        _read(real), _read(ghost),
        fromfile=f"project/{relative}",
        tofile=f"staged/{relative}",
    ))


def diff_all() -> dict[str, str]:
    """Every staged file's diff, keyed by project-relative path."""
    return {name: diff_for(name) for name in staged_files()}


# ------------------------------------------------------
# Consent
# ------------------------------------------------------
def _asked_for(user_text: str, phrases) -> bool:
    normalized = _normalize(user_text)
    if any(f" {marker} " in normalized for marker in NEGATION_VETO):
        return False
    return any(f" {phrase} " in normalized for phrase in phrases)


def requests_commit(user_text: str) -> bool:
    """Whether the USER asked for staged changes to reach the project.

    The same negation table action_plan uses, imported rather than
    copied: "don't apply the changes yet" has to veto a commit for
    exactly the reason it vetoes a live run, and two tables would be two
    things to keep in step.
    """
    return _asked_for(user_text, COMMIT_PHRASES)


def requests_discard(user_text: str) -> bool:
    return _asked_for(user_text, DISCARD_PHRASES)


# ------------------------------------------------------
# Commit and discard
# ------------------------------------------------------
def commit(user_text: str) -> dict:
    """Copy staged files into the project, if the user asked.

    Refuses rather than raises when consent is missing: a commit nobody
    asked for is not an error condition, it is a thing that must not
    happen, and the caller wants to say so rather than handle an
    exception.
    """
    if not requests_commit(user_text):
        logger.info("commit refused: no explicit request from the user")
        return {"status": "refused", "reason": "no explicit request", "files": []}

    names = staged_files()
    if not names:
        return {"status": "empty", "reason": "nothing staged", "files": []}

    written: list[str] = []
    failed: list[dict] = []

    for name in names:
        source = staging_root() / name
        destination = project_root() / name
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Bytes, not text: the staged file is already exactly what
            # should land, and re-encoding it would be one more chance to
            # change a line ending that nobody asked to change.
            destination.write_bytes(source.read_bytes())
            written.append(name)
            logger.info("committed %s", name)
        except Exception as error:
            logger.exception("commit failed for %s", name)
            failed.append({"file": name, "error": str(error)})

    return {
        "status": "committed" if not failed else "partial",
        "files": written,
        "failed": failed,
        "diffs": {name: diff_for(name) for name in written},
    }


def discard(user_text: str) -> dict:
    """Throw the staging area away, if the user asked."""
    if not requests_discard(user_text):
        return {"status": "refused", "reason": "no explicit request", "files": []}

    names = staged_files()
    root = staging_root()
    if root.is_dir():
        shutil.rmtree(root, ignore_errors=True)
        logger.info("discarded %d staged file(s)", len(names))

    return {"status": "discarded", "files": names}
