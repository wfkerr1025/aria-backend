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
    "reset_registry",
    "ensure_default_workspace",
    "rollback_workspace",
    "discard_workspace",
    "commit_workspace",
    "get_effective_file",
    "get_workspace_details",
    "get_workspace_list",
    "get_workspace",
    "set_active_workspaces",
    "set_primary_workspace",
    "remove_workspace",
    "add_workspace",
    "WorkspaceInfo",
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


def _pending_ops(root) -> list:
    from backend.core import fs_plan

    try:
        return fs_plan.pending_operations(root)
    except Exception:  # pragma: no cover - a listing fault is not fatal
        logger.exception("could not list staged operations")
        return []


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
        # How many projects are registered, so the one-line status can say
        # "Workspaces: 2" without a second round trip. Counted here rather
        # than in the UI: the page that owns the list is not always open,
        # and a status line that has to wait for another packet shows a
        # wrong number until it arrives.
        "workspace_count": len(_state.workspaces),
        # Counted alongside staged files: "0 staged" while a delete is
        # waiting would be a true statement and a misleading one.
        "staged_operations": len(_pending_ops(project)),
    }


# ======================================================
# Several projects at once
# ======================================================
# One ARIA, more than one project. The rule that makes this safe is
# stated once and enforced by construction rather than by care:
#
#   A workspace's staging lives INSIDE that workspace
#   (<root>/.aria_staging/<name>/), so one project's staged files are not
#   reachable from another project's root. Isolation is a consequence of
#   where the directory is, not of a check somebody has to remember.
#
# Every operation names the workspace it acts on. Nothing falls back to
# another workspace's staged files when a path is not found -- a read
# that quietly answered from a different project would be the worst
# failure available here, because it would look exactly like a correct
# answer.
#
# The primary workspace IS the one the environment points at, which is
# what every existing caller of file_tools already uses. The
# single-workspace path is not a special case of this; it is this, with
# one entry.

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class WorkspaceInfo:
    """One project ARIA is working in."""

    id: str
    name: str
    root_path: str
    active: bool = True
    last_used: float = 0.0

    @property
    def root(self) -> Path:
        return Path(self.root_path)

    # Read on demand rather than stored. A cached staged count disagrees
    # with the directory the moment anything writes to it, and this is
    # the number a user decides whether to commit from.
    @property
    def ghost_directory_path(self) -> str:
        return str(ghost_workspace.staging_root(self.root))

    @property
    def staged_files(self) -> list:
        try:
            return ghost_workspace.staged_files(self.root)
        except Exception:  # pragma: no cover - a listing fault is not fatal
            logger.exception("could not list staged files for %s", self.name)
            return []

    def describe(self) -> dict:
        from backend.core import model_capability

        model = model_capability.active_model_id()
        capable = model_capability.supports_tool_use(model)
        staged = self.staged_files

        return {
            "id": self.id,
            "name": self.name,
            "root_path": self.root_path,
            "ghost_directory_path": self.ghost_directory_path,
            "staged_files": staged,
            "staged_count": len(staged),
            "tool_capable_model": model if capable else None,
            "tool_capable": capable,
            "capability_warning": model_capability.capability_warning(model),
            "active": self.active,
            "last_used": self.last_used,
            "primary": self.id == _state.primary,
        }


@dataclass
class _Registry:
    workspaces: dict = field(default_factory=dict)
    primary: str = None


_state = _Registry()


def _slug(path: Path) -> str:
    return path.name or "workspace"


def _validated(path) -> Path:
    """The same validation set_project_root uses, for the same reason."""
    requested = str(path or "").strip()
    if not requested:
        raise WorkspaceError("A workspace path cannot be empty")

    resolved = Path(requested).expanduser().resolve(strict=False)
    if not resolved.is_dir():
        raise WorkspaceError(f"Not a directory: {resolved}")
    return resolved


def add_workspace(root_path, name=None) -> WorkspaceInfo:
    """Register a project. Adding the same one twice returns the first.

    Identity is the resolved path, not the name: two entries pointing at
    one directory would each keep their own staging view of the same
    files, and committing one would silently make the other's diffs
    wrong.
    """
    resolved = _validated(root_path)

    for existing in _state.workspaces.values():
        if existing.root == resolved:
            existing.last_used = time.time()
            return existing

    info = WorkspaceInfo(
        id=uuid.uuid4().hex[:12],
        name=(str(name).strip() if name else "") or _slug(resolved),
        root_path=str(resolved),
        last_used=time.time(),
    )
    _state.workspaces[info.id] = info
    if _state.primary is None:
        set_primary_workspace(info.id)

    logger.info("workspace added: %s (%s)", info.name, info.root_path)
    return info


def remove_workspace(workspace_id: str) -> bool:
    """Forget a project. Its files and its staging are left alone.

    Removing is bookkeeping, never a delete: staged work in a project
    ARIA has stopped tracking is still the user's work, and throwing it
    away because a list got shorter is not a trade anyone asked for.
    """
    info = _state.workspaces.pop(workspace_id, None)
    if info is None:
        return False

    if _state.primary == workspace_id:
        _state.primary = None
        remaining = sorted(_state.workspaces.values(), key=lambda w: -w.last_used)
        if remaining:
            set_primary_workspace(remaining[0].id)

    logger.info("workspace removed: %s (staging left on disk)", info.name)
    return True


def set_primary_workspace(workspace_id: str) -> WorkspaceInfo:
    """Make this the workspace the file tools are pointed at.

    The primary IS the environment's workspace root. Keeping them one
    thing is what stops a second definition of "where ARIA is"
    disagreeing with the boundary enforced at write time.
    """
    info = _state.workspaces.get(workspace_id)
    if info is None:
        raise WorkspaceError(f"No such workspace: {workspace_id!r}")

    set_project_root(info.root_path)
    _state.primary = workspace_id
    info.active = True
    info.last_used = time.time()
    logger.info("primary workspace: %s", info.name)
    return info


def set_active_workspaces(workspace_ids) -> list:
    """Which workspaces are in scope. The primary is always among them.

    Active means "operations may name this one", not "operations may
    reach into it": every read and every commit still names the
    workspace it acts on.
    """
    wanted = {str(i) for i in (workspace_ids or [])}
    if _state.primary:
        wanted.add(_state.primary)

    for workspace_id, info in _state.workspaces.items():
        info.active = workspace_id in wanted

    return sorted(w.id for w in _state.workspaces.values() if w.active)


def get_workspace(workspace_id: str) -> WorkspaceInfo:
    info = _state.workspaces.get(workspace_id)
    if info is None:
        raise WorkspaceError(f"No such workspace: {workspace_id!r}")
    return info


def get_workspace_list() -> list:
    """Every registered project, most recently used first."""
    return [
        info.describe()
        for info in sorted(_state.workspaces.values(), key=lambda w: -w.last_used)
    ]


def get_workspace_details(workspace_id: str) -> dict:
    """One project, with its pending changes and their diffs."""
    from backend.core import fs_plan

    info = get_workspace(workspace_id)
    details = info.describe()
    details["pending_changes"] = ghost_workspace.get_pending_changes(info.root)
    # The staged OPERATIONS -- delete, move, rename, copy, new folder.
    # They carry no diff because they are not content changes, so the
    # page has to render them separately or a plan that only deletes
    # looks like an empty workspace with a live Commit button.
    details["pending_operations"] = fs_plan.pending_operations(info.root)
    return details


def get_effective_file(path: str, workspace_id: str) -> Path:
    """Read `path` as this workspace sees it. Never any other workspace.

    The workspace is named, not searched for. Falling back to another
    workspace's staged copy when this one has none is the merge this
    subsystem exists to prevent, and it would be invisible: the caller
    gets a file, and nothing about it says which project it came from.
    """
    return ghost_workspace.get_effective_file(path, get_workspace(workspace_id).root)


def commit_workspace(workspace_id: str, user_text: str, files=None,
                     on_progress=None) -> dict:
    """Commit this workspace, and check that doing so broke nothing.

    Commit is the moment a staged EDIT reaches the project -- the
    counterpart of the auto-commit that a new file takes. A creation is
    verified there; everything else is verified here, by the same rule
    and with the same consequence: if the change takes the project from
    green to red it is undone, and the reply says which tests went red.

    The check is added around the commit rather than inside it. What
    "commit" means is ghost_workspace's business; whether the result
    stands is this one's, and keeping them apart means a fault in the
    checker cannot corrupt a commit.
    """
    from backend.core import fs_plan

    info = get_workspace(workspace_id)
    info.last_used = time.time()

    names, _unknown = ghost_workspace._selected(files, info.root)
    operations = fs_plan.pending_operations(info.root) if files is None else []

    def apply_commit():
        return ghost_workspace.commit_changes(user_text, files=files, root=info.root)

    try:
        from backend.core import change_verification

        result, verification = change_verification.verify_commit(
            names, operations, apply_commit, info.root, on_progress=on_progress)
    except Exception:
        # A fault in the checker costs the check, never the commit. The
        # user asked for their work to land; a broken verifier is not a
        # reason to refuse them.
        logger.exception("commit verification failed; committing unchecked")
        return apply_commit()

    if verification is not None:
        result = dict(result or {})
        result["verification"] = {
            "ran": verification.ran,
            "passed": verification.passed,
            "new_failures": verification.new_failures,
            "pre_existing": verification.pre_existing,
            "suites": len(verification.suites),
            "whole_suite": verification.whole_suite,
            "seconds": round(verification.seconds, 1),
            "skipped_because": verification.skipped_because,
            "message": verification.describe(),
        }
    return result


def discard_workspace(workspace_id: str, user_text: str, files=None) -> dict:
    info = get_workspace(workspace_id)
    info.last_used = time.time()
    return ghost_workspace.discard_changes(user_text, files=files, root=info.root)


def rollback_workspace(workspace_id: str, files=None) -> dict:
    info = get_workspace(workspace_id)
    info.last_used = time.time()
    return ghost_workspace.rollback_staged_changes(files=files, root=info.root)


def touch_workspace(workspace_id: str) -> None:
    info = _state.workspaces.get(workspace_id)
    if info is not None:
        info.last_used = time.time()


def ensure_default_workspace() -> None:
    """Seed the registry with the directory ARIA is already in.

    The registry starts empty, but ARIA is always working somewhere --
    file_tools has a root whether or not anyone registered it. An empty
    Control Center would say "no workspaces" about a session that is
    demonstrably editing files in one.
    """
    if _state.workspaces:
        return
    try:
        add_workspace(get_project_root())
    except WorkspaceError:  # pragma: no cover - the cwd is always a directory
        logger.exception("could not seed the workspace registry")


def reset_registry() -> None:
    """Forget every workspace. For tests, and for a fresh session."""
    _state.workspaces.clear()
    _state.primary = None
