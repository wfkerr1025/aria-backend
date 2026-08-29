"""ARIA Lite - which project ARIA is working in, and where it stages.

The working directory has been implicit until now: file_tools reads
ARIA_TOOL_WORKSPACE, or falls back to the process's cwd, and nothing
displayed it or let anyone change it. On this install that means the
workspace is the ARIA-Lite source tree, which is a surprising thing for
a user to discover by watching a commit land.

So this is the small amount of state a "working directory" needs, and
one rule about where it lives: file_tools.workspace_root() stays the
single authority. Setting the directory here sets the environment
variable that function already reads, rather than keeping a second copy
that could disagree with the boundary actually enforced at write time.
A second copy is how a path check passes in one place and fails in
another.

Not persisted, deliberately. Writing it to a config file would be the
fourth mutable file under backend/config, and the suite has already been
observed rewriting two of them -- one of which cost a confusing
debugging session. The setting lasts for the life of the process, and
the caller that knows how a session should be restored is the one that
should restore it.
"""

from __future__ import annotations

import os
from pathlib import Path

from backend.core import file_tools, ghost_workspace

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "WorkspaceError",
    "describe_workspace",
    "get_ghost_root",
    "get_project_root",
    "refresh_workspace",
    "set_project_root",
]

# Reused rather than redefined: a caller catching one kind of workspace
# error should not have to catch two.
WorkspaceError = file_tools.WorkspaceError


def get_project_root() -> Path:
    """The directory ARIA is working in."""
    return file_tools.workspace_root()


def get_ghost_root() -> Path:
    """Where edits are staged for that project."""
    return ghost_workspace.staging_root()


def set_project_root(path: str) -> Path:
    """Point ARIA at a different project.

    Validated before it takes effect: a working directory that does not
    exist would make every read fail with a confusing error a long way
    from the setting that caused it.

    Sets the environment variable file_tools already reads, so the
    boundary enforced at write time moves with it and there is nothing to
    keep in step.
    """
    # Checked as text, before it becomes a Path. Path("") is Path("."),
    # whose str() is "." and is therefore truthy -- so testing the Path
    # let an empty setting through and resolved it to the current
    # directory, silently pointing ARIA at wherever the process happened
    # to start.
    requested = str(path or "").strip()
    if not requested:
        raise WorkspaceError("A working directory cannot be empty")

    resolved = Path(requested).expanduser().resolve(strict=False)
    if not resolved.is_dir():
        raise WorkspaceError(f"Not a directory: {resolved}")

    previous = get_project_root()
    os.environ[file_tools.ENV_WORKSPACE] = str(resolved)
    logger.info("working directory changed: %s -> %s", previous, resolved)
    return resolved


def refresh_workspace() -> dict:
    """Re-read everything the UI displays. No side effects."""
    return describe_workspace()


def describe_workspace() -> dict:
    """What the working-directory panel shows.

    Includes the staged count, because "you have changes waiting" is the
    thing a user most needs to know before changing directory: staging is
    per-project, and moving away leaves them behind rather than carrying
    them along.
    """
    from backend.core import model_capability

    project = get_project_root()
    ghost = get_ghost_root()

    try:
        staged = ghost_workspace.staged_files()
    except Exception:  # pragma: no cover - a listing fault is not a fatal one
        logger.exception("could not list staged files")
        staged = []

    active = model_capability.active_model_id()

    return {
        "project_root": str(project),
        "ghost_root": str(ghost),
        "ghost_exists": ghost.is_dir(),
        "staged_count": len(staged),
        "staged_files": staged,
        "active_model": active,
        "tool_capable": model_capability.supports_tool_use(active),
        "capability_warning": model_capability.capability_warning(active),
    }
