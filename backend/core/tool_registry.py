# backend/core/tool_registry.py

"""
Unified tool registry — Phase 2, item 2.

backend.core.tool_executor.py has two real, working tools (weather,
web search) hardcoded into backend/websocket/handlers.py and
backend/rest/router.py's intent-detection branches. There is no
general-purpose registry anywhere in the codebase (confirmed by
research before writing this) — this is genuinely new infrastructure,
not a replacement for anything.

register_tool() takes a ToolSchema (name/description/JSON-schema-like
parameters/permission level) plus a plain callable handler. execute_tool()
is the single path every provider/plugin/pipeline should call through —
it runs the handler inside backend.core.sandbox.run_in_sandbox() (real
timeout, monitored memory) and returns a uniform ToolResult regardless
of which tool ran or how it failed.

The two existing tools are registered below as thin wrappers around
tool_executor.py's real functions — this does NOT change
handlers.py/rest/router.py's existing hand-wired weather/search
short-circuit branches (those keep working exactly as before); it gives
NEW code (providers, plugins, the execution pipeline) one consistent way
to reach the same underlying tools instead of re-deriving the wiring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .sandbox import run_in_sandbox, SandboxLimits, SandboxResult
from . import perf_profiler
from .errors import AriaError, ErrorCode

# Phase 3 memory tool wrappers (backend/tool_*.py). Imported here so the
# six memory tools are registered on the same import that already brings
# up weather/web_search, with no separate wiring step for callers.
from ..tool_save_note import tool_save_note, TOOL_SPEC as SAVE_NOTE_SPEC
from ..tool_search_notes import tool_search_notes, TOOL_SPEC as SEARCH_NOTES_SPEC
from ..tool_set_context import tool_set_context, TOOL_SPEC as SET_CONTEXT_SPEC
from ..tool_get_context import tool_get_context, TOOL_SPEC as GET_CONTEXT_SPEC
from ..tool_set_self import tool_set_self, TOOL_SPEC as SET_SELF_SPEC
from ..tool_get_self import tool_get_self, TOOL_SPEC as GET_SELF_SPEC
from ..tool_list_notes import tool_list_notes, TOOL_SPEC as LIST_NOTES_SPEC
from ..tool_delete_note import tool_delete_note, TOOL_SPEC as DELETE_NOTE_SPEC
from ..tool_list_context import tool_list_context, TOOL_SPEC as LIST_CONTEXT_SPEC
from ..tool_list_self import tool_list_self, TOOL_SPEC as LIST_SELF_SPEC

from logger import get_logger

logger = get_logger(__name__)

# Permission levels a tool declares it needs — checked by execute_tool()
# against the caller-supplied `allowed_permissions`. "safe" tools (pure
# computation, no I/O) can always run; anything else must be explicitly
# allowed by whoever's invoking the registry (a provider, a plugin host,
# the execution pipeline), matching the spirit of
# backend.llm.tool_router.SAFE_TASKS's existing allowlist concept,
# generalized from "task name in a fixed set" to "declared capability".
PERMISSION_SAFE = "safe"
PERMISSION_NETWORK = "network"
PERMISSION_FILESYSTEM = "filesystem"
ALL_PERMISSIONS = frozenset({PERMISSION_SAFE, PERMISSION_NETWORK, PERMISSION_FILESYSTEM})


@dataclass
class ToolSchema:
    name: str
    description: str
    # JSON-schema-*like* (not a full JSON Schema implementation — just
    # {"param_name": {"type": "string", "required": True, "description": "..."}}),
    # enough to validate presence/basic type without pulling in a
    # dependency this project doesn't otherwise need.
    parameters: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    permission: str = PERMISSION_SAFE
    timeout_seconds: float = 10.0
    # Which concurrency pool this tool belongs to when several run at
    # once. Not a performance knob -- the three families fail in
    # different ways under fan-out, and the right width for one is the
    # wrong width for another:
    #
    #   search   bounded by network latency        -- wide
    #   ludo     bounded by MONEY and rate limits  -- narrow, and gated
    #   cli      bounded by CPU and RAM            -- one Blender is a core
    #   registry bounded by run_in_sandbox threads -- moderate
    #
    # Declared here rather than in a table beside the router, so a new
    # tool cannot silently land in the widest pool.
    family: str = "registry"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "description": self.description,
            "parameters": self.parameters, "permission": self.permission,
            "timeout_seconds": self.timeout_seconds, "family": self.family,
        }


@dataclass
class ToolResult:
    ok: bool
    value: Any = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    sandbox: Optional[Dict[str, Any]] = None


@dataclass
class _RegisteredTool:
    schema: ToolSchema
    handler: Callable[..., Any]


_REGISTRY: Dict[str, _RegisteredTool] = {}


def register_tool(schema: ToolSchema, handler: Callable[..., Any]) -> None:
    if schema.permission not in ALL_PERMISSIONS:
        raise ValueError(f"Unknown permission '{schema.permission}' for tool '{schema.name}'")
    _REGISTRY[schema.name] = _RegisteredTool(schema=schema, handler=handler)
    logger.debug(f"register_tool() → registered '{schema.name}' (permission={schema.permission})")


def unregister_tool(name: str) -> bool:
    return _REGISTRY.pop(name, None) is not None


def list_tools() -> List[Dict[str, Any]]:
    return [t.schema.to_dict() for t in _REGISTRY.values()]


def get_tool_schema(name: str) -> Optional[ToolSchema]:
    entry = _REGISTRY.get(name)
    return entry.schema if entry else None


def _validate_args(schema: ToolSchema, args: Dict[str, Any]) -> Optional[str]:
    for param_name, spec in schema.parameters.items():
        if spec.get("required") and param_name not in args:
            return f"Missing required parameter '{param_name}'"
        expected_type = spec.get("type")
        if param_name in args and expected_type:
            type_map = {"string": str, "number": (int, float), "boolean": bool, "object": dict, "array": list}
            py_type = type_map.get(expected_type)
            if py_type and not isinstance(args[param_name], py_type):
                return f"Parameter '{param_name}' must be of type {expected_type}"
    return None


def execute_tool(name: str, args: Optional[Dict[str, Any]] = None, allowed_permissions: Optional[set] = None) -> ToolResult:
    """
    The single execution path for every tool, regardless of who's
    calling (a provider, a plugin, the unified execution pipeline).
    allowed_permissions=None means "everything allowed" (the caller is
    fully trusted, e.g. this backend's own built-in tools) — pass an
    explicit set (even an empty one) to actually gate less-trusted
    callers (a plugin's declared tool access, say).
    """
    args = args or {}
    entry = _REGISTRY.get(name)

    if entry is None:
        return ToolResult(ok=False, error=f"Unknown tool: {name}", error_code=ErrorCode.TOOL_NOT_FOUND)

    schema = entry.schema

    if allowed_permissions is not None and schema.permission not in allowed_permissions:
        logger.warning(f"execute_tool() → '{name}' denied: requires '{schema.permission}', caller allows {allowed_permissions}")
        return ToolResult(
            ok=False,
            error=f"Tool '{name}' requires permission '{schema.permission}', which the caller did not grant",
            error_code=ErrorCode.TOOL_PERMISSION_DENIED,
        )

    validation_error = _validate_args(schema, args)
    if validation_error:
        return ToolResult(ok=False, error=validation_error, error_code=ErrorCode.TOOL_INVALID_ARGS)

    with perf_profiler.timed(f"tool_registry.execute_tool.{name}"):
        result: SandboxResult = run_in_sandbox(
            entry.handler, **args,
            limits=SandboxLimits(timeout_seconds=schema.timeout_seconds),
        )

    if not result.ok:
        code = ErrorCode.TOOL_TIMEOUT if result.timed_out else ErrorCode.TOOL_EXECUTION_FAILED
        return ToolResult(ok=False, error=result.error, error_code=code, sandbox=result.__dict__)

    return ToolResult(ok=True, value=result.value, sandbox=result.__dict__)


# ============================================================
# Built-in tools — thin wrappers around backend.core.tool_executor's
# existing, real weather/search implementations. Registered here so
# provider/plugin/pipeline code has one consistent way to reach them;
# does not change how handlers.py/rest/router.py already invoke the
# underlying functions directly for their own hand-wired short-circuits.
# ============================================================

def _weather_handler(location: str) -> dict:
    # Batch 3: routed through backend.core.weather_router — the single
    # source of truth for a fully-populated, truthful weather result
    # (provider always reflects whichever of NOAA/WeatherAPI/Open-Meteo
    # actually served it, never a guess; temperature/conditions/timestamp
    # are guaranteed non-null on success) — rather than the raw,
    # inconsistently-shaped dict tools/get_weather.py's three providers
    # each return on their own.
    from . import weather_router
    packet, error = weather_router.get_weather_truthful(location)
    if error is not None:
        return {"ok": False, "error": error.message, "error_code": error.code}
    return {"ok": True, **packet.to_dict()}


def _search_handler(query: str) -> dict:
    from .tool_executor import run_search_tool, format_search_reply
    result = run_search_tool(query)
    return {"raw": result, "reply": format_search_reply(result)}


def register_builtin_tools() -> None:
    register_tool(
        ToolSchema(
            name="weather",
            description="Get current weather for a named location.",
            parameters={"location": {"type": "string", "required": True, "description": "City/place name"}},
            permission=PERMISSION_NETWORK,
            timeout_seconds=8.0,
            family="search",
        ),
        _weather_handler,
    )
    register_tool(
        ToolSchema(
            name="web_search",
            description="Run a web search and return summarized results.",
            parameters={"query": {"type": "string", "required": True, "description": "Search query"}},
            permission=PERMISSION_NETWORK,
            # web_search is not one request: it is a chain of specialist
            # providers and, when none of them answers, a general-web
            # fallback behind them. At 10.0 this budget was the same as
            # one HTTP request's, so a single unreachable host cancelled
            # the whole tool -- 2.1 seconds before the fallback returned
            # ten good results into a turn that had already given up.
            #
            # The providers now bound themselves (3s for the best-effort
            # news tier, 6s otherwise), so this is a ceiling over the
            # whole chain rather than the thing that ends it. Every
            # provider hanging until its own deadline sums to 27s; this
            # sits above that, and is pinned by a test so the two cannot
            # drift apart. In practice a query costs 3-7 seconds -- the
            # ceiling only matters when something is already broken,
            # which is exactly when the fallback tier must still run.
            timeout_seconds=30.0,
            family="search",
        ),
        _search_handler,
    )


register_builtin_tools()


# ============================================================
# Phase 9.2 file and test tools -- handlers live in
# backend/core/file_tools.py, which owns the workspace confinement,
# the write preview and the fixed test command. Registered here so the
# planning tool layer reaches them through the same execute_tool() path
# as everything else, with the same argument validation and the same
# permission gate.
#
# All three are FILESYSTEM, including run_tests: it starts a process
# whose whole purpose is to read and execute the project's files, so a
# caller granted only 'safe' must not be able to reach it.
# ============================================================

def _read_file_handler(path: str) -> dict:
    from backend.core.file_tools import read_file

    return read_file(path)


def _edit_file_handler(path: str, content: str, confirm: bool = False) -> dict:
    from backend.core.file_tools import edit_file

    return edit_file(path, content, confirm=confirm)


def _run_tests_handler(scope: str = "") -> dict:
    from backend.core.file_tools import run_tests

    return run_tests(scope)


# The five shape-changing operations. Every one of these handlers STAGES
# and none of them touches the project.
#
# That is the safety argument, and it is structural rather than
# procedural: there is no os.remove in the delete handler, so there is no
# argument, flag or permission that makes it delete during a turn. The
# only code that removes anything is fs_plan.apply_operations(), reached
# from the commit path, which needs the user to have asked twice.
#
# `confirm` here means "record this in the plan" rather than "do it".
# tool_orchestrator sets it exactly where it sets edit_file's, so a dry
# run previews and a chat-consented turn stages -- one rule for all six
# mutating tools instead of two rules that could drift apart.
def _staging_handler(op: str):
    def handler(path: str, dest: str = "", new_name: str = "",
                confirm: bool = False, **unexpected) -> dict:
        """Stage or preview one shape-changing operation.

        **unexpected exists because a model supplies arguments the tool
        does not have. Measured live: nemo-12b emitted
        {"tool": "create_folder", "path": "src", "content": "..."} and
        this raised TypeError, eleven times, so the reply was a wall of
        "_staging_handler.<locals>.handler() got an unexpected keyword
        argument 'content'".

        They are REFUSED rather than ignored. Ignoring "content" on a
        create_folder would make a folder and throw away the file the
        model was trying to write -- silent data loss dressed as
        tolerance. Refusing says what was wrong in words the user can act
        on, and costs the turn nothing it was going to get anyway.
        """
        from backend.core import fs_plan

        if unexpected:
            named = ", ".join(sorted(unexpected))
            raise ValueError(
                f"{op} does not take {named}. If you meant to write file "
                f"contents, use edit_file; {op} only changes the shape of "
                f"the tree."
            )

        target = new_name or dest or None
        if confirm:
            return fs_plan.stage_operation(op, path, target)
        return fs_plan.preview_operation(op, path, target)

    handler.__name__ = f"_{op}_handler"
    return handler


def register_fs_operation_tools() -> None:
    common = {
        "path": {"type": "string", "required": True,
                 "description": "Workspace-relative path"},
        "confirm": {"type": "boolean", "required": False,
                    "description": "Stage the operation. Omit or false to preview it."},
    }
    described = {
        "delete_file": ("Stage a file or folder for deletion. Nothing is removed "
                        "until the workspace is committed.", {}),
        "create_folder": ("Stage the creation of a folder.", {}),
        "move_file": ("Stage moving a file to another path.",
                      {"dest": {"type": "string", "required": True,
                                "description": "Workspace-relative destination path"}}),
        "rename_file": ("Stage renaming a file in place.",
                        {"new_name": {"type": "string", "required": True,
                                      "description": "The new file name, not a path"}}),
        "copy_file": ("Stage copying a file to another path.",
                      {"dest": {"type": "string", "required": True,
                                "description": "Workspace-relative destination path"}}),
    }

    for name, (description, extra) in described.items():
        register_tool(
            ToolSchema(
                name=name,
                description=description,
                parameters={**common, **extra},
                permission=PERMISSION_FILESYSTEM,
                timeout_seconds=10.0,
            ),
            _staging_handler(name),
        )


def register_unity_cli_tools() -> None:
    """Expose the Unity CLI's operations to a model.

    Each tool is one fixed subcommand -- see UNITY_TOOLS. The model
    picks which operation to run and supplies named values; it never
    composes a command line, and nothing outside the table can be
    called.

    They are FILESYSTEM permission and generously timed on purpose: a
    Unity build writes to disk and takes minutes, and describing it as
    a safe ten-second call would be describing something else.

    Registration is conditional. A machine with no Unity CLI configured
    should not be told about five tools that can only fail -- an
    unusable tool in the brief spends a small model's attention and
    teaches it that tools do not work.
    """
    from backend.unity import unity_cli_engine as engine

    try:
        from backend.plugins import plugin_settings

        plugin = plugin_settings.load_plugins().get(engine.PLUGIN_ID) or {}
        available = bool(plugin.get("enabled")) and not plugin.get("dismissed")
    except Exception:  # pragma: no cover - a registry read is not a tool
        logger.debug("could not read the Unity CLI plugin", exc_info=True)
        available = False

    if not available:
        logger.debug("register_unity_cli_tools() -> Unity CLI not enabled; skipped")
        for spec in engine.UNITY_TOOLS:
            _REGISTRY.pop(spec["name"], None)
        return

    def _make_handler(tool_name: str):
        def handler(**arguments):
            outcome = engine.run_tool(tool_name, arguments)
            if not outcome.get("success"):
                raise RuntimeError(outcome.get("error") or "the command failed")
            # The JSON when the CLI produced some, the text when it did
            # not. A caller that gets a dict has structure to read; one
            # that gets a string has what the tool printed.
            return outcome.get("json") if outcome.get("json") is not None                 else (outcome.get("output") or "")
        return handler

    for spec in engine.UNITY_TOOLS:
        register_tool(
            ToolSchema(
                name=spec["name"],
                description=spec["description"],
                parameters=dict(spec["parameters"]),
                permission=PERMISSION_FILESYSTEM,
                timeout_seconds=float(engine.DEFAULT_TIMEOUT_SECONDS),
                family="cli",
            ),
            _make_handler(spec["name"]),
        )

    logger.info("register_unity_cli_tools() -> %d Unity CLI tool(s)",
                len(engine.UNITY_TOOLS))


def register_file_tools() -> None:
    register_tool(
        ToolSchema(
            name="read_file",
            description="Read a text file from inside the workspace.",
            parameters={
                "path": {"type": "string", "required": True,
                         "description": "Workspace-relative or absolute path"},
            },
            permission=PERMISSION_FILESYSTEM,
            timeout_seconds=10.0,
        ),
        _read_file_handler,
    )
    register_tool(
        ToolSchema(
            name="edit_file",
            description=(
                "Replace a file's contents. Previews the diff and writes nothing "
                "unless confirm=true."
            ),
            parameters={
                "path": {"type": "string", "required": True,
                         "description": "Workspace-relative or absolute path"},
                "content": {"type": "string", "required": True,
                            "description": "The complete new contents"},
                "confirm": {"type": "boolean", "required": False,
                            "description": "Write to disk. Omit or false to preview only."},
            },
            permission=PERMISSION_FILESYSTEM,
            timeout_seconds=15.0,
        ),
        _edit_file_handler,
    )
    register_tool(
        ToolSchema(
            name="run_tests",
            description="Run the configured test command, optionally narrowed to a scope.",
            parameters={
                "scope": {"type": "string", "required": False,
                          "description": "Test path or expression. The command itself is configuration."},
            },
            permission=PERMISSION_FILESYSTEM,
            # Longer than any other tool because a suite legitimately takes
            # minutes; file_tools caps the subprocess itself at 600s, and
            # this outer bound is what stops a hung child from holding the
            # sandbox thread indefinitely.
            timeout_seconds=660.0,
            family="cli",
        ),
        _run_tests_handler,
    )


register_file_tools()
register_fs_operation_tools()

# Conditional on the plugin being enabled, and re-run by the plugin
# update handler -- so turning Unity CLI on does not need a restart to
# make its tools callable.
register_unity_cli_tools()


# ============================================================
# Phase 3 memory tools -- backend/tool_*.py wrappers over the
# aria_memory_* modules (notes, personal context, self-knowledge).
# Each wrapper ships its own TOOL_SPEC, so those specs stay the single
# source of truth and are translated into ToolSchema here rather than
# restated. Permission is FILESYSTEM, not SAFE: these read and write
# backend/aria_memory.db, so a caller that only grants 'safe' (a
# plugin, say) must not be able to read or overwrite ARIA's memory.
# ============================================================

# _validate_args()'s type_map has no "integer" entry, so an "integer"
# parameter would silently skip type checking. "number" is the closest
# equivalent it does understand, so specs are mapped over on the way in.
_SPEC_TYPE_TO_REGISTRY_TYPE = {"integer": "number"}


def _schema_from_tool_spec(
    spec: Dict[str, Any],
    permission: str = PERMISSION_FILESYSTEM,
    timeout_seconds: float = 5.0,
) -> ToolSchema:
    parameters: Dict[str, Dict[str, Any]] = {}
    for arg_name, arg_spec in spec.get("args", {}).items():
        translated = dict(arg_spec)
        declared = translated.get("type")
        if declared in _SPEC_TYPE_TO_REGISTRY_TYPE:
            translated["type"] = _SPEC_TYPE_TO_REGISTRY_TYPE[declared]
        parameters[arg_name] = translated
    return ToolSchema(
        name=spec["name"],
        description=spec["description"],
        parameters=parameters,
        permission=permission,
        timeout_seconds=timeout_seconds,
    )


MEMORY_TOOLS = (
    (SAVE_NOTE_SPEC, tool_save_note),
    (SEARCH_NOTES_SPEC, tool_search_notes),
    (SET_CONTEXT_SPEC, tool_set_context),
    (GET_CONTEXT_SPEC, tool_get_context),
    (SET_SELF_SPEC, tool_set_self),
    (GET_SELF_SPEC, tool_get_self),
    (LIST_NOTES_SPEC, tool_list_notes),
    (DELETE_NOTE_SPEC, tool_delete_note),
    (LIST_CONTEXT_SPEC, tool_list_context),
    (LIST_SELF_SPEC, tool_list_self),
)


def register_memory_tools() -> None:
    for spec, handler in MEMORY_TOOLS:
        register_tool(_schema_from_tool_spec(spec), handler)


register_memory_tools()
