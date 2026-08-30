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
import json
import shutil
from datetime import datetime, timezone
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
    "SNAPSHOT_DIRNAME",
    "snapshot",
    "rollback_staged_changes",
    "discard_changes",
    "commit_additions",
    "commit_changes",
    "get_all_diffs",
    "get_diff",
    "get_pending_changes",
    "list_effective_directory",
    "get_effective_file",
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

# What lives in the staging root and is NOT the user's work. Anything
# here is skipped by staged_files(), so it is never diffed, never listed
# as a pending change, and never committed into the project.
_BOOKKEEPING = frozenset({".snapshots", ".fs_plan.json", ".deleted"})

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


def project_root(root: Path | None = None) -> Path:
    """The real project: what a commit writes into.

    `root` names a workspace explicitly. Every function here takes it and
    passes it down, because a multi-workspace operation has to act on a
    project that is not the one the environment points at -- and because
    the alternative, temporarily repointing the environment, would make
    two concurrent operations disagree about which project they are in.

    The workspace root, unless something separates them later. Kept as a
    function rather than a constant for the same reason workspace_root is
    one: a test that points it somewhere else must take effect without
    reloading the module.
    """
    return Path(root).resolve() if root else file_tools.workspace_root()


def staging_root(root: Path | None = None) -> Path:
    """This project's ghost workspace.

    Inside the project it stages for, never in a shared location. That is
    what makes workspaces isolated by construction rather than by a rule
    someone has to remember: one project's staging cannot be reached from
    another's root, because it is not under it.
    """
    base = project_root(root)
    return base / STAGING_DIRNAME / base.name


def _is_staging(path: Path, root: Path | None = None) -> bool:
    staging_base = project_root(root) / STAGING_DIRNAME
    return staging_base == path or staging_base in path.parents


def _relative_to_project(path: str, root: Path | None = None) -> Path:
    """The project-relative path, refusing anything outside the project.

    Reuses file_tools' confinement rather than re-deriving it: one
    definition of "inside", and it is the one enforced at write time.
    """
    resolved = file_tools.resolve_in_workspace(path, root=project_root(root))

    if _is_staging(resolved, root):
        # Already a ghost path. Staging it again would nest a staging
        # directory inside itself.
        raise GhostError(f"Refusing to stage a path already inside staging: {path!r}")

    try:
        return resolved.relative_to(project_root(root))
    except ValueError:  # pragma: no cover - resolve_in_workspace already refused
        raise GhostError(f"Path is outside the project: {path!r}")


def ghost_path_for(path: str, root: Path | None = None) -> Path:
    """Where an edit to `path` is written instead."""
    return staging_root(root) / _relative_to_project(path, root)


def stage_path(path: str, root: Path | None = None,
               allow_new_parent: bool = False) -> str:
    """The ghost path for `path`, with its directory ready to write into.

    Validates the PARENT rather than the file: a staged file must land
    somewhere the project already has, which refuses an invented
    directory without refusing a new file. Editing "backend/api.py" is a
    legitimate edit; editing "made/up/tree/api.py" is a hallucinated
    path wearing an edit's clothes.

    WHY A NEW FILE MAY BRING ITS FOLDER
    -----------------------------------
    That guard reads a missing parent as evidence the model invented the
    path, which is right when the file is supposed to already exist and
    wrong when it is not supposed to exist at all. Asked for
    "src/player_inventory.cs" on a project with no src/, ARIA refused --
    and the refusal was not even shown, so the reply said "Staged:" for
    work that never happened.

    Creating a folder along with a new file is not a hallucination, it is
    the ordinary shape of "put this somewhere sensible", and it is what
    the user asked for when they named the path. So `allow_new_parent`
    is set for a CREATE -- a target that does not exist in the project --
    and left off for an EDIT, where a missing directory still means the
    model is describing a file that is not there.

    Nothing is hidden by this. `new_directories_for` names the folders a
    commit would add, and the renderer says so before the user agrees.
    """
    relative = _relative_to_project(path, root)
    parent = project_root(root) / relative.parent

    if not parent.is_dir() and not allow_new_parent:
        raise GhostError(
            f"Refusing to stage {path!r}: {relative.parent.as_posix()!r} is not a "
            f"directory in the project"
        )

    ghost = staging_root(root) / relative
    ghost.parent.mkdir(parents=True, exist_ok=True)

    # Before it is replaced, keep what is there now. This is what
    # rollback_staged_changes restores, and taking it here means the
    # caller cannot forget to.
    snapshot(relative.as_posix(), root)

    logger.info("staging %s -> %s", relative.as_posix(), ghost)
    return str(ghost)


def new_directories_for(path: str, root: Path | None = None) -> list[str]:
    """Project folders a commit of `path` would have to create, outermost first.

    The visible half of allow_new_parent. A new tree is a bigger change
    than a new file, and the user should read it as one before it lands.
    """
    try:
        relative = _relative_to_project(path, root)
    except Exception:
        return []

    missing = []
    for parent in reversed(relative.parents):
        if parent == Path("."):
            continue
        if not (project_root(root) / parent).is_dir():
            missing.append(parent.as_posix())
    return missing


def staged_files(root: Path | None = None) -> list[str]:
    """Project-relative paths with something staged, sorted.

    ARIA's own bookkeeping is not a staged file. Snapshots and the
    operation journal live under the staging root because that is where
    ARIA's scratch belongs, but listing them here would put
    .snapshots/index.json and .fs_plan.json in the pending-changes list,
    show each a diff, and -- on the next commit -- write ARIA's
    bookkeeping into the user's project.

    Named explicitly rather than excluded by the leading dot. A blanket
    dotfile rule reads as tidier and would silently refuse to stage
    .gitignore, .env and .editorconfig, which are files a user edits.
    _BOOKKEEPING is the list, and a test fails if something is added to
    the staging root without being added to it.
    """
    base = staging_root(root)
    if not base.is_dir():
        return []

    def is_bookkeeping(relative: Path) -> bool:
        return relative.parts and relative.parts[0] in _BOOKKEEPING

    return sorted(
        str(relative).replace("\\", "/")
        for p in base.rglob("*")
        if p.is_file() and not is_bookkeeping(relative := p.relative_to(base))
    )


def _read(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_bytes().decode("utf-8", errors="replace").splitlines(keepends=True)


def diff_for(relative: str, root: Path | None = None) -> str:
    """A unified diff of one staged file against the project's copy.

    Deterministic: same inputs, same text, no timestamps in the header.
    A file that exists only in staging diffs against nothing, which is
    how a new file reads.
    """
    ghost = staging_root(root) / relative
    real = project_root(root) / relative

    return "".join(difflib.unified_diff(
        _read(real), _read(ghost),
        fromfile=f"project/{relative}",
        tofile=f"staged/{relative}",
    ))


def diff_all(root: Path | None = None) -> dict[str, str]:
    """Every staged file's diff, keyed by project-relative path."""
    return {name: diff_for(name, root) for name in staged_files(root)}


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
def commit(user_text: str, root: Path | None = None) -> dict:
    """Copy staged files into the project, if the user asked.

    Refuses rather than raises when consent is missing: a commit nobody
    asked for is not an error condition, it is a thing that must not
    happen, and the caller wants to say so rather than handle an
    exception.
    """
    if not requests_commit(user_text):
        logger.info("commit refused: no explicit request from the user")
        return {"status": "refused", "reason": "no explicit request", "files": []}

    names = staged_files(root)
    if not names:
        return {"status": "empty", "reason": "nothing staged", "files": []}

    written: list[str] = []
    failed: list[dict] = []

    for name in names:
        source = staging_root(root) / name
        destination = project_root(root) / name
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
        "diffs": {name: diff_for(name, root) for name in written},
    }


def discard(user_text: str, root: Path | None = None) -> dict:
    """Throw the staging area away, if the user asked."""
    if not requests_discard(user_text):
        return {"status": "refused", "reason": "no explicit request", "files": []}

    names = staged_files(root)
    root = staging_root()
    if root.is_dir():
        shutil.rmtree(root, ignore_errors=True)
        logger.info("discarded %d staged file(s)", len(names))

    return {"status": "discarded", "files": names}


# ======================================================
# Staged reads: one view of the project
# ======================================================
# A model that edits a file and then reads it back should see its own
# edit. Without this it reads the project's copy, concludes the edit did
# not happen, and either repeats it or reports it as failed -- a whole
# class of "why didn't my change stick" confusion invented by the
# staging layer itself.
#
# What this must never become is a way to read the staging directory as
# though it were project content. A path pointing INTO .aria_staging is
# refused by _relative_to_project; this only ever maps a project path
# onto its staged copy.

def get_effective_file(path: str, root: Path | None = None) -> Path:
    """The version of `path` to read: staged if there is one.

    Returns a real filesystem path, so a caller reads it with whatever it
    already uses. The project's copy comes back unchanged when nothing is
    staged, which is the common case.
    """
    try:
        relative = _relative_to_project(path, root)
    except Exception:
        # Not a project path -- outside the workspace, or already inside
        # staging. Confinement is file_tools' job and it has already
        # ruled; this layer does not get a second opinion on where a file
        # may be.
        return Path(path)

    ghost = staging_root(root) / relative
    return ghost if ghost.is_file() else project_root(root) / relative


def list_effective_directory(path: str = "", root: Path | None = None) -> list[str]:
    """Names in a directory, staged versions overriding real ones.

    Sorted, no duplicates: a file in both places is one file and appears
    once. The staging directory itself is never listed as project
    content.
    """
    try:
        relative = _relative_to_project(path, root) if path else Path()
    except Exception:
        return []

    names: set[str] = set()
    real_base = project_root(root) / relative

    for base in (real_base, staging_root(root) / relative):
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            if base == real_base and entry.name == STAGING_DIRNAME:
                continue
            names.add(entry.name)

    return sorted(names)


# ======================================================
# Snapshots
# ======================================================
SNAPSHOT_DIRNAME = ".snapshots"


def _snapshot_root(root: Path | None = None) -> Path:
    return staging_root(root) / SNAPSHOT_DIRNAME


def snapshot(relative: str, root: Path | None = None) -> bool:
    """Keep the current staged version before it is replaced.

    One slot per file, holding the previous staged content, which is what
    "restore the last snapshot" means for a staging area. Deeper history
    would be a version control system, and the project already has one.

    Returns whether anything was kept: a file staged for the first time
    has no previous version, and that is not a failure.
    """
    source = staging_root(root) / relative
    if not source.is_file():
        return False

    target = _snapshot_root(root) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())

    index = _snapshot_root(root) / "index.json"
    try:
        existing = json.loads(index.read_text(encoding="utf-8")) if index.is_file() else {}
    except (ValueError, OSError):
        existing = {}
    existing[relative] = {"taken_at": datetime.now(timezone.utc).isoformat()}
    index.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")

    logger.info("snapshotted staged %s", relative)
    return True


def rollback_staged_changes(files=None, root: Path | None = None) -> dict:
    """Restore staged files from their snapshots.

    Touches nothing in the project: this restores one staged version over
    another, entirely inside ARIA's own scratch.
    """
    wanted = list(files) if files else staged_files(root)
    restored: list[str] = []
    missing: list[str] = []

    for relative in wanted:
        source = _snapshot_root(root) / relative
        if not source.is_file():
            missing.append(relative)
            continue
        target = staging_root(root) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        restored.append(relative)
        logger.info("rolled staged %s back to its snapshot", relative)

    return {
        "status": "rolled_back" if restored else "no_snapshot",
        "files": restored,
        "no_snapshot": missing,
    }


# ======================================================
# The commit UX
# ======================================================
STATUS_STAGED_NEW = "new"
STATUS_STAGED_MODIFIED = "modified"
STATUS_STAGED_UNCHANGED = "unchanged"


def get_pending_changes(root: Path | None = None) -> list:
    """Every staged file, with enough to render a review screen.

    "unchanged" is reported rather than hidden: a staged file identical
    to the project's is a proposal that turned out to be a no-op, and a
    reviewer who sees it listed learns something a filtered list would
    have kept from them.
    """
    pending = []
    for relative in staged_files(root):
        real = project_root(root) / relative
        ghost = staging_root(root) / relative
        if not real.is_file():
            status = STATUS_STAGED_NEW
        elif real.read_bytes() == ghost.read_bytes():
            status = STATUS_STAGED_UNCHANGED
        else:
            status = STATUS_STAGED_MODIFIED

        pending.append({
            "path": relative,
            "status": status,
            "staged_path": str(ghost),
            "project_path": str(real),
            "diff": diff_for(relative, root),
            "has_snapshot": (_snapshot_root(root) / relative).is_file(),
        })
    return pending


def get_diff(path: str, root: Path | None = None) -> str:
    return diff_for(path, root)


def get_all_diffs(root: Path | None = None) -> dict:
    return diff_all(root)


def _selected(files, root: Path | None = None):
    """The staged files a command applies to, and the names it did not know.

    A caller naming something that is not staged is told so, rather than
    quietly getting a successful-looking report about nothing.
    """
    staged = staged_files(root)
    if not files:
        return staged, []

    wanted = [str(f).replace("\\", "/").strip() for f in files]
    known = [f for f in wanted if f in staged]
    unknown = [f for f in wanted if f not in staged]
    return known, unknown


def commit_changes(user_text: str, files=None, root: Path | None = None) -> dict:
    """Copy staged files into the project, if the user asked.

    files=None commits everything staged; a list commits only those.
    Refused without consent either way.
    """
    if not requests_commit(user_text):
        logger.info("commit refused: no explicit request from the user")
        return {"status": "refused", "reason": "no explicit request",
                "files": [], "unknown": []}

    from backend.core import fs_plan

    names, unknown = _selected(files, root)

    # Staged OPERATIONS are part of the same commit. A plan that deletes
    # notes.md and stages no content would otherwise report "nothing
    # staged" and quietly do nothing -- the user asked twice and got
    # silence.
    #
    # Applied only on a whole-workspace commit. A per-file commit names
    # content, and there is no sensible reading of "commit main.py" that
    # also means "and delete notes.md".
    operations = fs_plan.pending_operations(root) if files is None else []

    if not names and not operations:
        return {
            "status": "empty",
            "reason": "nothing staged" if not unknown else "none of those are staged",
            "files": [], "unknown": unknown, "operations": [],
        }

    written: list[str] = []
    failed: list[dict] = []

    for name in names:
        source = staging_root(root) / name
        destination = project_root(root) / name
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Bytes, not text: the staged file is already exactly what
            # should land, and re-encoding it is one more chance to
            # change a line ending nobody asked to change.
            destination.write_bytes(source.read_bytes())
            written.append(name)
            logger.info("committed %s", name)
        except Exception as error:
            logger.exception("commit failed for %s", name)
            failed.append({"file": name, "error": str(error)})

    # Content first, then the shape of the tree. "Rewrite main.py, then
    # move it into src/" is a sequence a user would say out loud and
    # expect to work; the reverse order writes to a path that has just
    # stopped existing.
    applied = (fs_plan.apply_operations(root) if operations
               else {"applied": [], "applied_operations": [], "failed": []})
    failed.extend({"file": entry["summary"], "error": entry["error"]}
                  for entry in applied["failed"])

    return {
        "status": "committed" if not failed else "partial",
        "files": written,
        "operations": applied["applied"],
        # Structured, for whoever may have to reverse them. See
        # change_verification._undo_commit.
        "applied_operations": applied.get("applied_operations") or [],
        "failed": failed,
        "unknown": unknown,
        "diffs": {name: diff_for(name, root) for name in written},
    }


def commit_additions(paths, root: Path | None = None) -> dict:
    """Write staged files into the project, but ONLY ones that are new.

    The two-consent flow exists to protect against losing something:
    an overwrite loses the old contents, a delete loses the file, a move
    loses the path. Creating a file that was not there destroys nothing,
    and there is nothing to undo.

    Measured against the alternative. Creating one hello_world.py took
    five attempts, a trip to the Control Center and three commit clicks,
    and the ceremony protected a file that did not exist. That is not
    safety, it is friction wearing safety's clothes.

    So this exists for additions and refuses everything else. The guard
    is HERE rather than in the caller: a path that already exists in the
    project is skipped whatever the caller believed, so there is no
    argument, ordering or mistake upstream that turns this into an
    overwrite. Deletes, moves and renames never reach it -- they are
    operations in the fs_plan journal, and this only copies staged
    content.

    Consent is the user's request itself, which named the file they
    wanted. It is not read again here; requests_live_execution has
    already decided it, and asking twice for a file that does not exist
    is the friction this removes.
    """
    written: list[str] = []
    skipped: list[str] = []
    failed: list[dict] = []

    for name in list(paths or []):
        destination = project_root(root) / name
        source = staging_root(root) / name

        if destination.exists():
            # Not an addition. It keeps the staged copy and the diff, and
            # the user commits it deliberately.
            skipped.append(name)
            continue
        if not source.is_file():
            skipped.append(name)
            continue

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
            source.unlink(missing_ok=True)
            written.append(name)
            logger.info("created %s", name)
        except Exception as error:
            logger.exception("could not create %s", name)
            failed.append({"file": name, "error": str(error)})

    return {"created": written, "skipped": skipped, "failed": failed}


def discard_changes(user_text: str, files=None, root: Path | None = None) -> dict:
    """Throw staged files away, if the user asked. Never touches the project.

    A whole-workspace discard drops the staged OPERATIONS too. Leaving
    them behind would mean a user who discarded everything still had a
    delete waiting for their next commit -- which is the single worst
    thing a staging system can get wrong.
    """
    if not requests_discard(user_text):
        return {"status": "refused", "reason": "no explicit request",
                "files": [], "unknown": []}

    from backend.core import fs_plan

    names, unknown = _selected(files, root)
    operations = fs_plan.pending_operations(root) if not files else []

    if not names and not operations:
        return {"status": "empty", "files": [], "unknown": unknown, "operations": 0}

    if not files:
        # Everything: the directory goes, snapshots and the operation
        # journal with it.
        #
        # The early return above used to fire whenever no CONTENT was
        # staged, which meant a plan holding only "delete notes.md"
        # survived a discard and waited for the next commit. A staged
        # deletion outliving the discard that was meant to cancel it is
        # the single worst thing this system could get wrong.
        dropped = len(operations)
        shutil.rmtree(staging_root(root), ignore_errors=True)
        logger.info("discarded %d staged file(s) and %d operation(s)",
                    len(names), dropped)
        return {"status": "discarded", "files": names, "unknown": unknown,
                "operations": dropped}

    for name in names:
        (staging_root(root) / name).unlink(missing_ok=True)
        logger.info("discarded staged %s", name)

    return {"status": "discarded", "files": names, "unknown": unknown}
