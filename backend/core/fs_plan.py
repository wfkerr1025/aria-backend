"""ARIA Lite - file and folder operations, staged rather than performed.

edit_file could be staged by redirecting its path: write the new contents
into the ghost workspace and the project is untouched until commit. That
trick does not extend to the operations added here. A delete has no
contents to redirect. Neither does a move, a rename, a copy, or a new
folder -- they are changes to the SHAPE of the tree, and there is nothing
to put in a staging directory that represents one.

So they are recorded instead. This module is a journal of intended
operations, kept beside the staged content in the same ghost workspace,
and applied only when that workspace is committed.

    proposed   the model wrote an action; nothing is recorded
    staged     the user asked for it in words; the operation is in the
               journal, validated, with a summary they can read
    applied    the user committed the workspace; the operation ran

THE TOOLS CANNOT DELETE ANYTHING
--------------------------------
Worth stating plainly, because it is the whole safety argument. The
handler behind delete_file does not call os.remove; it appends to this
journal. There is no argument, no flag and no permission that makes it
delete during a turn. The only code that removes a file is apply(), and
apply() runs from the commit path, which needs the user to have asked
twice.

That is stronger than checking a confirm flag, because a check can be
passed. A tool that has no delete in it cannot delete.

VALIDATION HAPPENS AT STAGING TIME
----------------------------------
A refusal is only useful while the user is still thinking about the
request. Staging a delete for a file that does not exist and discovering
it at commit -- ten minutes and five accepted proposals later -- reports
the failure to somebody who has forgotten what they asked for. So the
sandbox check, the existence check and the shape of a rename are all
decided here, and the answer comes back in the same turn.

ORDER OF APPLICATION
--------------------
Staged CONTENT is written first, then these operations. "Rewrite main.py,
then move it into src/" is a sequence a user would say out loud and
expect to work; the reverse order would write to a path that no longer
exists. Within the journal, operations apply in the order they were
staged.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "FILE_OPERATIONS",
    "PLAN_FILENAME",
    "PlanError",
    "StagedOperation",
    "apply_operations",
    "describe_plan",
    "discard_operations",
    "pending_operations",
    "preview_operation",
    "stage_operation",
]

# The journal lives inside the staging directory, so it travels with the
# workspace it belongs to and a second project cannot see it. The leading
# dot keeps it out of staged_files(), which lists what will be written --
# committing the journal into the project would be committing ARIA's
# bookkeeping as if it were the user's work.
PLAN_FILENAME = ".fs_plan.json"

OP_DELETE = "delete_file"
OP_CREATE_FOLDER = "create_folder"
OP_MOVE = "move_file"
OP_RENAME = "rename_file"
OP_COPY = "copy_file"

FILE_OPERATIONS = (OP_DELETE, OP_CREATE_FOLDER, OP_MOVE, OP_RENAME, OP_COPY)

# Operations that destroy or displace something that already exists. They
# are not treated differently HERE -- everything in this module is staged
# -- but the UI says so, and the summary leads with them.
DESTRUCTIVE = frozenset({OP_DELETE, OP_MOVE, OP_RENAME})


class PlanError(RuntimeError):
    """A staging request that must be refused, with a reason to show."""


@dataclass
class StagedOperation:
    op: str
    path: str
    dest: str | None = None
    staged_at: float = field(default_factory=time.time)

    def describe(self) -> str:
        if self.op == OP_DELETE:
            return f"delete {self.path}"
        if self.op == OP_CREATE_FOLDER:
            return f"create folder {self.path}"
        if self.op == OP_MOVE:
            return f"move {self.path} to {self.dest}"
        if self.op == OP_RENAME:
            return f"rename {self.path} to {self.dest}"
        if self.op == OP_COPY:
            return f"copy {self.path} to {self.dest}"
        return f"{self.op} {self.path}"

    @property
    def destructive(self) -> bool:
        return self.op in DESTRUCTIVE

    def as_dict(self) -> dict:
        return {
            "op": self.op, "path": self.path, "dest": self.dest,
            "staged_at": self.staged_at, "destructive": self.destructive,
            "summary": self.describe(),
        }


# ======================================================
# Where the journal lives
# ======================================================
def _plan_file(root: Path | None = None) -> Path:
    from backend.core import ghost_workspace

    return ghost_workspace.staging_root(root) / PLAN_FILENAME


def _load(root: Path | None = None) -> list[StagedOperation]:
    path = _plan_file(root)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # A corrupt journal is not a reason to lose a turn, and it is
        # certainly not a reason to guess at what it meant. An empty plan
        # applies nothing, which is the safe reading of "unreadable".
        logger.exception("could not read the staged operation plan at %s", path)
        return []

    operations = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or not entry.get("op"):
            continue
        operations.append(StagedOperation(
            op=str(entry["op"]), path=str(entry.get("path", "")),
            dest=entry.get("dest"), staged_at=float(entry.get("staged_at") or 0),
        ))
    return operations


def _save(operations: list[StagedOperation], root: Path | None = None) -> None:
    path = _plan_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([{"op": o.op, "path": o.path, "dest": o.dest,
                     "staged_at": o.staged_at} for o in operations],
                   indent=2),
        encoding="utf-8", newline="",
    )


# ======================================================
# Validation
# ======================================================
def _inside(path: str, root: Path | None = None) -> Path:
    """Resolve a path, or refuse it. The sandbox, and the only one."""
    from backend.core import file_tools

    try:
        return file_tools.resolve_in_workspace(path, root=root)
    except Exception as error:
        raise PlanError(str(error)) from error


def _projected(root: Path | None):
    """What the tree will look like once the staged plan has run.

    Operations are validated against this rather than against the disk,
    because a plan is a SEQUENCE and its steps are not independent.
    "Move main.py into src/, then copy main.py" is two individually valid
    operations and one impossible plan: by the time the copy runs, its
    source has moved.

    Without this, both stage cleanly and the contradiction surfaces at
    commit -- which is exactly the failure this module's docstring
    promises not to produce. Refusing at staging time puts the refusal in
    front of the person who can still fix it.

    Returns (gone, made): paths the plan removes, and paths it creates.
    """
    gone: set[Path] = set()
    made: set[Path] = set()

    for staged in _load(root):
        try:
            source = _inside(staged.path, root)
        except PlanError:  # pragma: no cover - a stored entry was valid once
            continue

        if staged.op == OP_DELETE:
            gone.add(source)
            made.discard(source)
        elif staged.op == OP_CREATE_FOLDER:
            made.add(source)
            gone.discard(source)
        elif staged.dest:
            try:
                target = _inside(staged.dest, root)
            except PlanError:  # pragma: no cover
                continue
            made.add(target)
            gone.discard(target)
            if staged.op in (OP_MOVE, OP_RENAME):
                gone.add(source)
                made.discard(source)

    return gone, made


def _check(op: str, path: str, dest: str | None, root: Path | None,
           *, use_plan: bool = True):
    """Everything that can be known before the operation runs.

    Returns (resolved_source, resolved_destination). Raises PlanError with
    a sentence worth showing the user.

    `use_plan` is the difference between the two moments this runs.

    While STAGING, the plan is the future and the disk is the past: an
    operation has to be judged against the tree the already-staged steps
    will produce. While APPLYING, the disk has become the truth -- the
    earlier steps have run -- and consulting the plan would make each
    operation read its own entry and refuse itself, which is exactly what
    happened the first time this was written.
    """
    gone, made = _projected(root) if use_plan else (set(), set())

    def will_exist(candidate: Path) -> bool:
        if candidate in made:
            return True
        if candidate in gone:
            return False
        return candidate.exists()

    if not str(path or "").strip():
        raise PlanError(f"{op} needs a path.")

    source = _inside(path, root)

    if op == OP_CREATE_FOLDER:
        if source.is_file() and source not in gone:
            raise PlanError(f"{path} is a file, so it cannot also be a folder.")
        return source, None

    if not will_exist(source):
        already = source in gone
        raise PlanError(
            f"{path} does not exist, so it cannot be {_verbed(op)}."
            if not already else
            f"{path} is already being removed by an earlier staged step, "
            f"so it cannot also be {_verbed(op)}.")

    if op == OP_DELETE:
        return source, None

    if op == OP_RENAME:
        name = str(dest or "").strip()
        if not name:
            raise PlanError("rename_file needs a new name.")
        # A NAME, not a path. "../../etc/passwd" is a valid string and
        # would turn a rename into a move out of the project; the sandbox
        # below would catch it, but refusing it here says something the
        # user can act on rather than "path escapes the workspace".
        if "/" in name or "\\" in name or name in (".", ".."):
            raise PlanError(
                "rename_file takes a new name, not a path. Use move_file to "
                "put a file somewhere else.")
        target = _inside(str(Path(path).parent / name), root)
    else:
        if not str(dest or "").strip():
            raise PlanError(f"{op} needs a destination.")
        target = _inside(dest, root)

    if target == source:
        raise PlanError(f"{path} is already where {op} would put it.")
    if will_exist(target):
        raise PlanError(f"{_relative(target, root)} already exists; "
                        f"{op} will not overwrite it.")

    return source, target


def _verbed(op: str) -> str:
    return {OP_DELETE: "deleted", OP_MOVE: "moved",
            OP_RENAME: "renamed", OP_COPY: "copied"}.get(op, "changed")


def _relative(resolved: Path, root: Path | None = None) -> str:
    from backend.core import ghost_workspace

    try:
        return str(resolved.relative_to(ghost_workspace.project_root(root)))
    except ValueError:  # pragma: no cover - resolve_in_workspace prevents this
        return str(resolved)


# ======================================================
# Staging
# ======================================================
def preview_operation(op: str, path: str, dest: str | None = None,
                      root: Path | None = None) -> dict:
    """Validate an operation and describe it. Records nothing.

    What a dry run gets. The validation is the same validation staging
    does, so a preview that says "this will work" is not a different
    opinion from the one that runs.
    """
    if op not in FILE_OPERATIONS:
        raise PlanError(f"{op} is not a file operation ARIA has.")

    source, target = _check(op, path, dest, root)
    staged = StagedOperation(op=op, path=_relative(source, root),
                             dest=_relative(target, root) if target else None)
    return {
        "op": op, "path": staged.path, "dest": staged.dest,
        "staged": False, "destructive": staged.destructive,
        "summary": staged.describe(),
        "preview": f"Would {staged.describe()}.",
    }


def stage_operation(op: str, path: str, dest: str | None = None,
                    root: Path | None = None) -> dict:
    """Record an operation to run when the workspace is committed.

    Nothing on disk changes here except the journal itself.
    """
    if op not in FILE_OPERATIONS:
        raise PlanError(f"{op} is not a file operation ARIA has.")

    source, target = _check(op, path, dest, root)
    staged = StagedOperation(op=op, path=_relative(source, root),
                             dest=_relative(target, root) if target else None)

    operations = _load(root)
    # Same operation twice in one plan is a duplicate, not two changes.
    if not any(o.op == staged.op and o.path == staged.path and o.dest == staged.dest
               for o in operations):
        operations.append(staged)
        _save(operations, root)

    logger.info("staged: %s", staged.describe())
    return {
        "op": op, "path": staged.path, "dest": staged.dest,
        "staged": True, "destructive": staged.destructive,
        "summary": staged.describe(),
        "preview": f"Staged: {staged.describe()}. Nothing has changed yet.",
    }


def pending_operations(root: Path | None = None) -> list[dict]:
    return [o.as_dict() for o in _load(root)]


def describe_plan(root: Path | None = None) -> str:
    """The plan as a sentence, for the consent prompt.

    Destructive operations first, because they are what the user is
    actually deciding about.
    """
    operations = _load(root)
    if not operations:
        return ""

    ordered = ([o for o in operations if o.destructive]
               + [o for o in operations if not o.destructive])
    return "\n".join(f"  - {o.describe()}" for o in ordered)


def discard_operations(root: Path | None = None) -> int:
    operations = _load(root)
    _plan_file(root).unlink(missing_ok=True)
    return len(operations)


# ======================================================
# Applying, which happens only from a commit
# ======================================================
# Where a deleted file waits, inside ARIA's own scratch rather than the
# project. Listed in ghost_workspace._BOOKKEEPING so it is never mistaken
# for staged work and never committed back into the project.
ARCHIVE_DIRNAME = ".deleted"


def archive_root(root: Path | None = None) -> Path:
    from backend.core import ghost_workspace

    return ghost_workspace.staging_root(root) / ARCHIVE_DIRNAME


def _archive(source: Path, relative: str, root: Path | None = None) -> Path | None:
    """Keep a copy of what a delete is about to destroy."""
    target = archive_root(root) / relative
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    except Exception:  # pragma: no cover - an archive fault is not a verdict
        logger.exception("could not archive %s before deleting it", relative)
        return None

    logger.info("archived %s before deleting it", relative)
    return target


def restore_archived(relative: str, root: Path | None = None) -> bool:
    """Put an archived delete back where it came from."""
    source = archive_root(root) / relative
    if not source.exists():
        return False

    from backend.core import ghost_workspace

    target = ghost_workspace.project_root(root) / relative
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    except Exception:  # pragma: no cover
        logger.exception("could not restore %s", relative)
        return False

    logger.info("restored %s from the archive", relative)
    return True


def apply_operations(root: Path | None = None) -> dict:
    """Run the staged operations against the project. Called by commit.

    Each operation is re-validated immediately before it runs. The
    project can have changed since staging -- the user may have deleted
    the file themselves, or another commit may have moved it -- and
    applying a plan against a tree that no longer matches it is how a
    "safe" staged operation destroys the wrong thing.

    A failure stops the rest. A half-applied plan is confusing; a plan
    that stopped at step 3 and says so is not.
    """
    operations = _load(root)
    if not operations:
        return {"applied": [], "failed": [], "status": "empty"}

    applied: list[str] = []
    performed: list[dict] = []
    failed: list[dict] = []

    for operation in operations:
        try:
            source, target = _check(operation.op, operation.path, operation.dest,
                                    root, use_plan=False)
        except PlanError as error:
            failed.append({"summary": operation.describe(), "error": str(error)})
            logger.warning("not applying %s: %s", operation.describe(), error)
            break

        try:
            if operation.op == OP_DELETE:
                # Kept before it goes. A staged delete has two consents
                # behind it, so this is not second-guessing the user --
                # it is what makes the delete REVERSIBLE, which is what
                # lets a verification failure be undone rather than just
                # reported. Without it, "the tests went red, I put it
                # back" would be a promise about a file that no longer
                # existed.
                _archive(source, operation.path, root)
                if source.is_dir():
                    shutil.rmtree(source)
                else:
                    source.unlink()
            elif operation.op == OP_CREATE_FOLDER:
                source.mkdir(parents=True, exist_ok=True)
            elif operation.op in (OP_MOVE, OP_RENAME):
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
            elif operation.op == OP_COPY:
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, target)
                else:
                    shutil.copy2(source, target)
            applied.append(operation.describe())
            # The same event, structured. The description is for a
            # person; this is what reversing the operation needs, and a
            # sentence cannot be un-applied.
            performed.append({
                "op": operation.op, "path": operation.path,
                "dest": operation.dest, "summary": operation.describe(),
            })
        except Exception as error:  # pragma: no cover - filesystem faults
            failed.append({"summary": operation.describe(), "error": str(error)})
            logger.exception("applying %s failed", operation.describe())
            break

    # Only what actually ran is cleared. A plan that stopped at step 3
    # keeps steps 3 onward, so the user can fix the cause and commit the
    # rest rather than re-describing all of it.
    remaining = _load(root)[len(applied):]
    if remaining:
        _save(remaining, root)
    else:
        _plan_file(root).unlink(missing_ok=True)

    return {
        "applied": applied,
        "applied_operations": performed,
        "failed": failed,
        "status": "failed" if failed else "applied",
    }
