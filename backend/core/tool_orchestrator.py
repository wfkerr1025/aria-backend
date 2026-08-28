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

from dataclasses import dataclass, field
from typing import Any, Callable

from backend.core import file_tools
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


def _record_undo(context: ExecutionContext, invocation) -> None:
    """Capture what it would take to undo this action, before it runs.

    Only for tools that change something. A read has nothing to undo, and
    inventing an undo for it would put entries on the stack that do
    nothing but obscure the ones that matter.
    """
    if invocation.tool_name not in _MUTATING_TOOLS:
        return

    path = invocation.args.get("path")
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
        # undo is a deletion. Deleting is not something this layer does
        # without being asked -- "no deletion without confirmation" --
        # so the undo is recorded as unavailable and said out loud.
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

    if tool_name == "edit_file":
        # A live edit means it. The router never sets this, and leaving
        # it to default would make every live edit a preview -- a run
        # that reports success and changes nothing.
        args["confirm"] = True

    _record_undo(context, invocation)

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
