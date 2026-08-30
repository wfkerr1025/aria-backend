"""ARIA Lite - running a plan, rather than describing one.

backend/tools/tool_router.py turns a Plan into ToolInvocations and says
plainly what it is not: "routing decides what would be called, executing
decides what happens, and keeping them apart is what lets the first be
exercised without the second existing." This is the second half. The
seam was left deliberately; nothing here widens it.

What this adds is sequencing, a context, a rollback stack and structured
results. What it deliberately does not add is a new way to run anything:
every action goes through backend.core.tool_registry.execute_tool, which
already validates arguments, checks the permission the tool declares, and
runs the handler inside backend.core.sandbox with a timeout and a memory
ceiling. An orchestrator that reached past that would be a second, less
tested execution path -- which is the shape of every incident this
codebase has already written a comment about.

Three properties worth stating, because they are the design:

    Dry run is the default. An orchestrator that executes unless told
    otherwise is one that executes by accident -- during a test, an
    import, a refactor, an exploratory call. Live execution is opt-in per
    run, and the flag is threaded rather than global so two runs cannot
    disagree about which mode they are in.

    Permissions are granted, never assumed. execute_tool takes the set
    the caller allows; the default here is the empty set, which permits
    only "safe" tools. Reading and writing files, and reaching the
    network, each have to be handed over explicitly by whoever called.

    A failed step stops the run and unwinds. The rollback stack holds
    what each completed step needs to undo itself, newest first, and is
    replayed on failure. A half-applied multi-file edit is worse than no
    edit, because it leaves a state nobody designed and nobody expected.

NOT IN THIS PASS, and the reason:

    Arbitrary code execution and arbitrary CLI tools -- linters,
    formatters, compilers, build tools -- are not here. They need an
    allowlist somebody decides on, and they sit against the rule the
    request itself set out ("no arbitrary shell execution"). run_tests
    already exists in file_tools with a FIXED command and a validated
    scope argument, and that shape is the one to extend when the
    allowlist exists: a registry of named commands with their own safety
    profiles, not a shell. Building a general command runner first and
    constraining it afterwards is the wrong order for the highest-risk
    subsystem in the app.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from backend.core import file_tools
from backend.core import ghost_workspace
from backend.core.tool_registry import (
    PERMISSION_FILESYSTEM,
    PERMISSION_NETWORK,
    PERMISSION_SAFE,
    execute_tool,
    get_tool_schema,
)

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "ActionResult",
    "ExecutionContext",
    "OrchestrationResult",
    "STATUS_BLOCKED",
    "STATUS_FAILED",
    "STATUS_OK",
    "STATUS_ROLLED_BACK",
    "STATUS_SKIPPED",
    "execute_invocations",
    "execute_plan",
]

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"          # dry run, or a dependency failed
STATUS_BLOCKED = "blocked"          # the permission was not granted
STATUS_ROLLED_BACK = "rolled_back"

# Tools whose effect outlives the turn, and which therefore need an undo
# recorded before they run. Everything else is a read.
_MUTATING_TOOLS = frozenset({"edit_file"})

# The operations that change the SHAPE of the tree rather than a file's
# contents. They cannot be staged by redirecting a path -- a delete has
# no contents to redirect -- so they are recorded in fs_plan's journal
# and applied at commit. `confirm` means "record it", never "do it".
_STAGING_TOOLS = frozenset({
    "delete_file", "create_folder", "move_file", "rename_file", "copy_file",
})


@dataclass
class ActionResult:
    """What one action did, or did not do."""

    step_id: str
    tool_name: str
    status: str
    value: Any = None
    error: str | None = None
    error_code: str | None = None
    # The preview a dry run produced, when the tool offers one.
    preview: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


@dataclass
class OrchestrationResult:
    """What a whole run did."""

    results: list[ActionResult] = field(default_factory=list)
    rolled_back: list[str] = field(default_factory=list)
    dry_run: bool = True

    @property
    def ok(self) -> bool:
        """True when nothing failed. A skipped step is not a failure."""
        return not any(r.status == STATUS_FAILED for r in self.results)

    @property
    def failed(self) -> list[ActionResult]:
        return [r for r in self.results if r.status == STATUS_FAILED]


@dataclass
class ExecutionContext:
    """The conditions one run happens under.

    Threaded rather than global, so two runs cannot disagree about
    whether they are live, and a test cannot leave a live flag behind for
    whatever runs next.
    """

    dry_run: bool = True
    # Empty means "safe tools only". Reading files, writing files and
    # reaching the network are each handed over explicitly by the caller.
    allowed_permissions: set[str] = field(default_factory=set)
    # Newest first. Each entry undoes one completed action.
    rollback_stack: list[tuple[str, Callable[[], None]]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # What the user actually asked for. Read for the same reason
    # consent is: the model does not get to authorise a new folder
    # tree by proposing one.
    user_text: str = ""
    # Project folders this turn would add, in the order they were
    # first needed. A new tree is a bigger change than a new file
    # and is named to the user before it lands.
    new_folders: list[str] = field(default_factory=list)

    @property
    def workspace_root(self):
        """Read from file_tools rather than stored.

        One definition of "inside the project", and it is the one the
        file tools already enforce. A second copy here could disagree
        with the boundary actually applied at write time, which is the
        kind of disagreement that only shows up as a file written
        somewhere nobody expected.
        """
        return file_tools.workspace_root()

    def grant(self, *permissions: str) -> "ExecutionContext":
        self.allowed_permissions.update(permissions)
        return self


def _permission_for(tool_name: str) -> str:
    schema = get_tool_schema(tool_name)
    return schema.permission if schema else PERMISSION_FILESYSTEM


def _record_undo(context: ExecutionContext, tool_name: str, path: str | None) -> None:
    """Capture what it would take to undo this action, before it runs.

    Takes the EFFECTIVE path -- the one the write will actually use --
    rather than reading it back off the invocation. Since edits are
    redirected into the ghost workspace, the invocation still carries the
    project path, and an undo built from that would restore a file in the
    project: a write to the project during a rollback, from the one layer
    whose whole purpose is that the project is not written to until a
    commit.

    Only for tools that change something. A read has nothing to undo, and
    inventing an undo for it would put entries on the stack that do
    nothing but obscure the ones that matter.
    """
    if tool_name not in _MUTATING_TOOLS:
        return

    if not path:
        return

    # read_file raises WorkspaceError when the path is not a file, and
    # returns the contents under "text" -- not "content", and with no
    # "status" key. Read off the function rather than assumed: guessing
    # at that shape produced an undo that silently captured None.
    try:
        before = file_tools.read_file(path)
        prior = before.get("text")
        truncated = bool(before.get("truncated"))
    except file_tools.WorkspaceError:
        # The file does not exist yet, so this edit creates it and the
        # undo is a deletion.
        #
        # Inside the ghost workspace that is fine and is the right thing:
        # a staged file this run created is ARIA's own scratch, not the
        # user's work, and leaving half a proposal behind after a failed
        # run is the "state nobody designed" this unwinding exists to
        # prevent -- just moved into staging.
        #
        # Anywhere else it is not. "No deletion without confirmation"
        # holds for the project, so the gap is recorded and said out loud
        # rather than worked around.
        target = Path(path)
        if ghost_workspace.staging_root() in target.parents:
            def undo_created() -> None:
                target.unlink(missing_ok=True)
                logger.info("removed staged file %s", target.name)

            context.rollback_stack.insert(0, (str(target), undo_created))
            return

        logger.info("%s does not exist; a rollback would have to delete it, "
                    "which this layer does not do unasked", path)
        context.errors.append(f"no rollback for {path}: it would have to be deleted")
        return
    except Exception:
        logger.exception("no undo could be captured for %s; rollback will be partial", path)
        context.errors.append(f"no rollback captured for {path}")
        return

    if truncated:
        # read_file caps at MAX_READ_BYTES. Restoring a truncated copy
        # would not undo the edit, it would destroy the tail of the file.
        logger.warning("%s is larger than the read cap; refusing to record a "
                       "rollback that would truncate it", path)
        context.errors.append(f"no rollback for {path}: too large to capture whole")
        return

    def undo() -> None:
        file_tools.edit_file(path, prior, confirm=True)

    context.rollback_stack.insert(0, (path, undo))


def _unwind(context: ExecutionContext) -> list[str]:
    """Replay the rollback stack, newest first."""
    undone: list[str] = []
    for label, undo in list(context.rollback_stack):
        try:
            undo()
            undone.append(label)
            logger.info("rolled back %s", label)
        except Exception:
            # One undo failing must not strand the rest. The remaining
            # entries are the older changes, and leaving those applied
            # too would compound the problem this is here to limit.
            logger.exception("rollback failed for %s; continuing with the rest", label)
            context.errors.append(f"rollback failed for {label}")
    context.rollback_stack.clear()
    return undone


def _run_one(invocation, context: ExecutionContext) -> ActionResult:
    tool_name = invocation.tool_name
    args = dict(invocation.args or {})
    permission = _permission_for(tool_name)

    if permission != PERMISSION_SAFE and permission not in context.allowed_permissions:
        logger.info("blocked %s: needs %r, which this run did not grant",
                    tool_name, permission)
        return ActionResult(
            step_id=invocation.step_id, tool_name=tool_name,
            status=STATUS_BLOCKED,
            error=f"{tool_name} requires the {permission!r} permission, "
                  f"which this run did not grant",
        )

    if context.dry_run:
        # edit_file previews natively: confirm=False renders the diff and
        # writes nothing, which is exactly what a dry run wants and is
        # already the tool's default. Anything else is simply not run --
        # a dry run that executed a test suite would not be one.
        if tool_name in _STAGING_TOOLS:
            # Previewed, not skipped. A dry run whose answer is "nothing
            # ran" tells the user nothing about what WOULD run, and this
            # is the turn where they decide whether to allow it.
            args["confirm"] = False
            result = execute_tool(tool_name, args, context.allowed_permissions)
            value = result.value if result.ok else None
            return ActionResult(
                step_id=invocation.step_id, tool_name=tool_name,
                status=STATUS_OK if result.ok else STATUS_FAILED,
                value=value, error=result.error, error_code=result.error_code,
                preview=(value or {}).get("preview") if isinstance(value, dict) else None,
            )

        if tool_name == "edit_file":
            args["confirm"] = False
            result = execute_tool(tool_name, args, context.allowed_permissions)
            value = result.value if result.ok else None
            return ActionResult(
                step_id=invocation.step_id, tool_name=tool_name,
                status=STATUS_OK if result.ok else STATUS_FAILED,
                value=value, error=result.error, error_code=result.error_code,
                # "diff", not "preview": that is what edit_file returns.
                preview=(value or {}).get("diff") if isinstance(value, dict) else None,
            )

        logger.info("dry run: %s not executed", invocation.label)
        return ActionResult(
            step_id=invocation.step_id, tool_name=tool_name,
            status=STATUS_SKIPPED,
        )

    if tool_name in _STAGING_TOOLS:
        # "Live" here still writes nothing to the project. The handler
        # appends to fs_plan's journal and returns; the operation runs
        # when the workspace is committed, which is the second consent.
        args["confirm"] = True
        result = execute_tool(tool_name, args, context.allowed_permissions)
        value = result.value if result.ok else None
        return ActionResult(
            step_id=invocation.step_id, tool_name=tool_name,
            status=STATUS_OK if result.ok else STATUS_FAILED,
            value=value, error=result.error, error_code=result.error_code,
            preview=(value or {}).get("preview") if isinstance(value, dict) else None,
        )

    if tool_name == "edit_file":
        # A live edit means it. The router never sets this, and leaving
        # it to default would make every live edit a preview -- a run
        # that reports success and changes nothing.
        args["confirm"] = True

        # And it lands in the ghost workspace, not the project. An edit
        # the user has seen only as a sentence should not already be on
        # disk where they keep their work; staged, it is a proposal with
        # a diff attached, and ghost_workspace.commit() is what makes it
        # real.
        #
        # Redirected here rather than inside edit_file. Staging every
        # write at the primitive would be the stronger boundary, and is
        # what to do if this ever needs defence in depth -- but it would
        # also change what edit_file means for every existing caller and
        # test, which is a large blast radius for a change whose whole
        # point is to be reviewable. This is the only path that writes
        # during a turn, and a test asserts it never hands edit_file a
        # path outside staging.
        # A create may bring its folder; an edit may not. The distinction
        # is whether the target is already in the project: if it is not,
        # there is no existing file being described, so a missing parent
        # is a folder the user asked for rather than one the model
        # imagined.
        try:
            # Where the file goes was settled before this ran, by
            # _honour_the_requested_location. This step only decides
            # whether the folder may be created, and records it.
            wanted = str(args.get("path", ""))
            folders = ghost_workspace.new_directories_for(wanted)
            args["path"] = ghost_workspace.stage_path(
                wanted,
                allow_new_parent=_is_a_new_file(wanted)
                and _the_user_named_the_folder(wanted, context.user_text))

            # Recorded only now, because stage_path did not refuse. A
            # folder listed for an action that failed would have the
            # reply saying "could not write it" and "this will add the
            # folder" in the same breath.
            for folder in folders:
                if folder not in context.new_folders:
                    context.new_folders.append(folder)
        except Exception as error:
            logger.warning("refusing to stage %r: %s", args.get("path"), error)
            return ActionResult(
                step_id=invocation.step_id, tool_name=tool_name,
                status=STATUS_FAILED, error=str(error),
            )

    # The staged path, not the invocation's. See _record_undo.
    _record_undo(context, tool_name, args.get("path"))

    result = execute_tool(tool_name, args, context.allowed_permissions)
    if not result.ok:
        logger.warning("%s failed: %s", invocation.label, result.error)
        context.errors.append(f"{invocation.label}: {result.error}")
        return ActionResult(
            step_id=invocation.step_id, tool_name=tool_name,
            status=STATUS_FAILED, error=result.error, error_code=result.error_code,
        )

    logger.info("%s ok", invocation.label)
    return ActionResult(
        step_id=invocation.step_id, tool_name=tool_name,
        status=STATUS_OK, value=result.value,
    )


def execute_invocations(invocations, context: ExecutionContext | None = None
                        ) -> OrchestrationResult:
    """Run invocations in order, stopping and unwinding on the first failure.

    Fail-fast on purpose. A plan's steps are ordered because the later
    ones assume the earlier ones happened; running step three after step
    two failed asks it to work against a state nobody planned for.
    """
    context = context or ExecutionContext()
    outcome = OrchestrationResult(dry_run=context.dry_run)

    invocations = list(invocations or [])
    logger.info("orchestrating %d action(s), dry_run=%s",
                len(invocations), context.dry_run)

    for index, invocation in enumerate(invocations):
        result = _run_one(invocation, context)
        outcome.results.append(result)

        if result.status != STATUS_FAILED:
            continue

        remaining = invocations[index + 1:]
        for later in remaining:
            outcome.results.append(ActionResult(
                step_id=later.step_id, tool_name=later.tool_name,
                status=STATUS_SKIPPED,
                error="an earlier action failed",
            ))

        outcome.rolled_back = _unwind(context)
        for done in outcome.results:
            if done.status == STATUS_OK and done.tool_name in _MUTATING_TOOLS:
                done.status = STATUS_ROLLED_BACK
        logger.warning("run stopped at %s; %d action(s) rolled back",
                       invocation.label, len(outcome.rolled_back))
        break

    return outcome


def execute_plan(plan, current_goal=None, context: ExecutionContext | None = None
                 ) -> OrchestrationResult:
    """Route a plan to its tools and run them.

    Routing is not repeated here: ToolRouter already decides which tool a
    step of each kind needs, and which steps are the model's work rather
    than a tool's. A second opinion about that in this module would be a
    second table to drift.
    """
    from backend.tools.tool_router import route_plan

    return execute_invocations(route_plan(plan, current_goal), context)


# ======================================================
# One call for a turn
# ======================================================
STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"

# Which permissions an action run is allowed to use. edit_file and
# run_tests are both filesystem tools; nothing here grants network, so a
# model cannot turn an "action" into an outbound request.
ACTION_PERMISSIONS = frozenset({PERMISSION_FILESYSTEM})


def _report_status(outcome: OrchestrationResult) -> str:
    """success / partial / failed / blocked, in that order of precedence."""
    if outcome.failed:
        return STATUS_FAILED if not outcome.rolled_back else STATUS_PARTIAL
    if any(r.status == STATUS_BLOCKED for r in outcome.results):
        return STATUS_BLOCKED
    return STATUS_SUCCESS


# Create a new file straight away rather than staging it for a second
# confirmation. Overwrites, deletes, moves and renames are unaffected --
# see ghost_workspace.commit_additions, which refuses anything that
# already exists.
AUTO_COMMIT_NEW_FILES = True


def _content_problems(actions) -> dict:
    """Paths whose proposed content does not parse, with the reason."""
    from backend.core.content_check import check_content, describe_problem

    problems: dict[str, str] = {}
    for action in actions:
        if action.tool_name != "edit_file":
            continue
        path = str(action.args.get("path") or "")
        if not path:
            continue
        result = check_content(path, action.args.get("content") or "")
        if result.failed:
            key = path.replace(chr(92), "/")
            problems[key] = describe_problem(key, result)
            logger.warning("content check: %s", problems[key])
    return problems


# A folder the user did not ask for is the model's idea, not theirs.
#
# stage_path refuses a file whose parent directory does not exist,
# because a missing parent usually means the model invented the path.
# That guard is right about invented paths and wrong about new ones: the
# user asks for "src/player_inventory.cs" on a project with no src/, and
# the answer should be the file, not a refusal.
#
# The filesystem cannot tell those apart -- the directory is missing
# either way. The user's own words can, and they are the same authority
# this module already uses for consent: the model does not get to
# authorise a new folder tree by proposing one.
_WORD = re.compile(r"[A-Za-z0-9_.-]+")


def _the_user_named_the_folder(path: str, user_text: str) -> bool:
    """Whether every new folder in `path` appears in what the user wrote.

    "create src/player_inventory.cs" names src/, so src/ is theirs to
    create. A bare "make me an inventory script" names nothing, and a
    directory in the answer came from the model.
    """
    from backend.core import ghost_workspace

    missing = ghost_workspace.new_directories_for(path)
    if not missing:
        return True

    said = {word.lower() for word in _WORD.findall(str(user_text or ""))}
    return all(
        # The deepest segment is what the user would have typed; the
        # ancestors come with it. "docs/api/spec.md" is named by the
        # phrase "docs/api", so each segment is checked on its own name.
        PurePosixPath(folder).name.lower() in said
        for folder in missing
    )


def _where_the_user_asked_for_it(path: str, user_text: str) -> str:
    """The path, with a directory the user never mentioned removed.

    Measured, on the user's own request: "create a player_inventory.cs
    with stackable slots" produced src/player_inventory.cs. The user
    asked for a file and got a subdirectory they had not mentioned --
    and because src/ did not exist, staging refused and the turn
    silently did nothing.

    Refusing would be honest and useless; the file was wanted. So when
    the user named no folder at all and the model supplied one that is
    not in the project, the file goes where it was asked for. A folder
    the user DID name is kept and created, and a folder that already
    exists is always kept -- a model filing something into an existing
    tree is being helpful, not inventive.
    """
    wanted = str(path or "").replace(chr(92), "/").strip()
    if "/" not in wanted.strip("/"):
        return wanted

    try:
        from backend.core import ghost_workspace

        if not ghost_workspace.new_directories_for(wanted):
            return wanted  # the tree is already there
    except Exception:  # pragma: no cover - fall through to the guard below
        return wanted

    if _the_user_named_the_folder(wanted, user_text):
        return wanted

    # The user mentioned no directory anywhere in the request. Not just
    # this one -- if they were talking about folders at all, the model's
    # choice may be a reading of something said earlier in the sentence,
    # and quietly relocating the file would be its own kind of wrong.
    if "/" in str(user_text or "") or _FOLDER_WORD.search(str(user_text or "")):
        return wanted

    bare = PurePosixPath(wanted).name
    logger.info("dropping unrequested folder from %r; the user asked for %r",
                wanted, bare)
    return bare


_FOLDER_WORD = re.compile(r"\b(folder|directory|dir|subfolder|subdirectory)\b",
                          re.IGNORECASE)


def _honour_the_requested_location(actions, user_text: str) -> dict:
    """Rewrite action paths to where the user asked for the file.

    Mutates the invocations in place and returns {proposed: final} for
    the ones that moved, so the reply can name the file that was really
    written rather than the one the model first suggested.
    """
    moved = {}
    for action in actions:
        if action.tool_name != "edit_file":
            continue
        proposed = str(action.args.get("path") or "")
        final = _where_the_user_asked_for_it(proposed, user_text)
        if final and final != proposed:
            action.args["path"] = final
            moved[proposed] = final
    return moved


def _is_a_new_file(path: str) -> bool:
    """Whether this path is absent from the project.

    A resolve that refuses is not a new file -- it is a path outside the
    workspace, and stage_path must get its own refusal rather than an
    extra permission.
    """
    from backend.core import file_tools

    try:
        return not file_tools.resolve_in_workspace(str(path or "")).exists()
    except Exception:
        return False


def _paths_that_do_not_exist(actions) -> set:
    """Targets of edit_file actions that are not in the project yet."""
    from backend.core import file_tools

    missing = set()
    for action in actions:
        if action.tool_name != "edit_file":
            continue
        path = str(action.args.get("path") or "")
        if not path:
            continue
        try:
            if not file_tools.resolve_in_workspace(path).exists():
                missing.add(path.replace(chr(92), "/"))
        except Exception:
            # An unresolvable path is refused later by the tool itself.
            continue
    return missing


def run_answer_actions(answer_text: str, user_text: str,
                       on_progress=None, history=()) -> dict | None:
    """Parse the actions in an answer and run them for one turn.

    The seam a transport calls, kept here so wiring it in is one call
    rather than a copy of this logic in each of the three chat entry
    points -- which is how backend/rest/router.py and
    backend/websocket/handlers.py came to have three independent
    orchestrations of everything else.

    Returns None when the answer asked for nothing, which is almost every
    turn and is not a failure. A caller that gets None should behave
    exactly as it did before actions existed.

    Dry run unless the USER asked for a live one. The licence is read
    from user_text and never from answer_text: a model that writes
    "apply the changes" in its own answer has described an intention, not
    granted itself one.
    """
    from backend.core.action_plan import (
        parse_actions, requests_live_execution, unsupported_actions,
    )

    actions = parse_actions(answer_text)

    # A tool ARIA does not have is a refusal that has to be SAID. Before
    # this, an answer whose only action block named delete_file parsed to
    # nothing, returned None, and the turn reported nothing -- so a user
    # who read "I'll remove notes.md" saw no error and had every reason
    # to think it had happened.
    unsupported = unsupported_actions(answer_text)

    if not actions:
        if unsupported:
            return {
                "actions": [], "results": [], "rollback": [],
                "status": "unsupported", "dry_run": True,
                "unsupported": unsupported,
                "notes": [
                    f"{name} is not a tool ARIA has, so nothing was done."
                    for name in unsupported
                ],
            }
        return None

    # With the conversation. "ok, I need you to add some things to the
    # inventory" is an instruction to change a file when the message
    # before it created one, and reads as chat without that context --
    # so the turn ran as a dry run, staged nothing, and reported nothing
    # wrong.
    live = requests_live_execution(user_text, history)

    # Filesystem either way, and dry_run is what holds the line. A dry
    # run still needs the permission, because edit_file's preview reads
    # the file to build the diff -- so gating the grant on `live` would
    # only turn every preview into a "blocked" and teach nobody anything.
    # What stops a dry run writing is that confirm stays False.
    #
    # Network is never granted. A model cannot turn an "action" into an
    # outbound request.
    context = ExecutionContext(dry_run=not live, user_text=user_text)
    context.allowed_permissions.update(ACTION_PERMISSIONS)

    # Which targets do not exist yet, recorded BEFORE anything runs.
    # Asking afterwards would be asking about a file the turn had just
    # staged, and every write would look like an addition.
    # Where each file actually goes, settled BEFORE anything reads a
    # path. This used to happen inside the staging step, which runs
    # after new_paths is captured -- so a relocated file was staged
    # under its new name, looked for under the old one, and never
    # auto-committed. One decision, made once, and every later stage
    # sees the same path.
    relocated = _honour_the_requested_location(actions, user_text)

    new_paths = _paths_that_do_not_exist(actions)

    logger.info("running %d action(s) for this turn, live=%s", len(actions), live)
    outcome = execute_invocations(actions, context)

    # A file that did not exist is created now, not queued behind a
    # second confirmation in another window.
    #
    # The two-consent flow protects against LOSING something -- an
    # overwrite loses the old contents, a delete loses the file. Creating
    # a new file destroys nothing, and making the user visit the Control
    # Center to approve it is friction wearing safety's clothes.
    # Measured: one hello_world.py took five attempts, a trip to the
    # workspace page and three commit clicks.
    #
    # commit_additions refuses any path that already exists, so this
    # cannot become an overwrite however the actions were ordered, and
    # deletes and moves never reach it -- they live in the fs_plan
    # journal and are applied only by a real commit.
    # Does what the model wrote parse as what its extension says it is?
    #
    # Nothing checked this before. nemo-12b proposed "def open_world:" --
    # not Python, the parentheses missing -- and it would have gone into
    # the project as a file that cannot be imported. "ARIA created the
    # file" and "ARIA created a working file" were the same claim and
    # only the first was true.
    #
    # A failure loses the SHORTCUT, not the work: the file stays staged
    # with the reason attached, where its diff can be read and committed
    # deliberately. A model that writes broken code has still done most
    # of the job, and the checker can be wrong about a dialect.
    problems = _content_problems(actions)

    created: list[str] = []
    verification = None
    if live and AUTO_COMMIT_NEW_FILES:
        additions = [
            name for name in ghost_workspace.staged_files()
            if name in new_paths and name not in problems
        ]
        if additions:
            # Parsing is not the same question as "does this break
            # anything". A file can be perfectly well-formed and still
            # take the project from green to red -- measured here, when a
            # new test file that imported nothing broke a guard asserting
            # every suite is registered with the runner.
            #
            # So the tests that bear on this change run before and after
            # it, and only NEW failures are laid at its door. If it broke
            # something the file comes back out and stays staged: the
            # shortcut is withdrawn, the work is not.
            from backend.core import change_verification

            try:
                created, verification = change_verification.verify_new_files(
                    additions,
                    lambda names: ghost_workspace.commit_additions(names)["created"],
                    file_tools.workspace_root(),
                    on_progress=on_progress,
                )
            except Exception:
                # A fault in the checker must not cost the user their
                # file. It costs them the check, and the report says so.
                logger.exception("verification failed; creating the file unchecked")
                created = ghost_workspace.commit_additions(additions)["created"]
                verification = None

    return {
        "actions": [
            {"tool": a.tool_name, "args": a.args, "step_id": a.step_id}
            for a in actions
        ],
        "results": [
            {
                "step_id": r.step_id, "tool": r.tool_name, "status": r.status,
                "error": r.error, "preview": r.preview,
            }
            for r in outcome.results
        ],
        "rollback": list(outcome.rolled_back),
        "status": _report_status(outcome),
        "dry_run": outcome.dry_run,
        "created": created,
        "problems": problems,
        "unsupported": unsupported,
        "new_folders": list(context.new_folders),
        "relocated": relocated,
        "verification": verification,
        "notes": list(context.errors) + [
            f"{message} It is staged rather than written, so you can read it "
            f"before it reaches the project."
            for message in problems.values()
        ] + [
            # An empty create is legal and is almost never what was
            # wanted: a model that proposes a file without writing its
            # contents produces a real, committable, useless file. The
            # staged diff shows it, but only to someone who looks -- this
            # says it in the turn.
            f"{a.args.get('path')} would be created empty; ARIA proposed no contents."
            for a in actions
            if a.tool_name == "edit_file" and not str(a.args.get("content") or "").strip()
        ] + [
            f"{name} is not a tool ARIA has, so that step was skipped."
            for name in unsupported
        ],
    }
