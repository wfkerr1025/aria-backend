"""ARIA Lite Phase 9 - what tools a plan step could call for.

NOT backend/core/tool_registry.py. That module is the live one: it holds
registered handlers, validates arguments against permissions and actually
runs things through execute_tool(). This module describes tools as data so
that a plan can be mapped onto them, and runs nothing at all.

The two exist for different questions. The core registry answers "what can
be executed right now, and with what permission"; this answers "what kind of
tool would a step of this kind need". Keeping the second out of the first
means the planning layer can be reasoned about, tested and changed without
touching anything that has side effects -- and the day these are joined up,
the join is one function in tool_executor rather than a rewrite.

Pure data. Nothing here imports a handler, holds a callable, or knows how
any tool would be carried out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "EDIT_FILE",
    "READ_FILE",
    "RUN_TESTS",
    "STATUS_ERROR",
    "STATUS_INVALID",
    "STATUS_NOT_EXECUTED",
    "STATUS_OK",
    "TOOLS",
    "TOOLS_BY_KIND",
    "WEATHER",
    "WEB_SEARCH",
    "Tool",
    "ToolInvocation",
    "ToolResult",
    "tool_for_kind",
]

# What became of one invocation. A closed set, because the prompt decides
# what to print from it and a status nobody anticipated would be rendered as
# though it had succeeded.
STATUS_OK = "ok"                      # the tool ran and returned
STATUS_ERROR = "error"                # the tool ran and failed, or was refused
STATUS_INVALID = "invalid_invocation"  # the request was malformed; nothing ran
STATUS_NOT_EXECUTED = "not_executed"   # deliberately skipped; nothing ran

# The statuses that mean nothing reached the disk or the network. Kept as a
# set rather than tested inline so that adding a fourth non-running status
# cannot quietly start rendering as a result.
NOT_RUN_STATUSES = frozenset({STATUS_INVALID, STATUS_NOT_EXECUTED})


@dataclass(frozen=True)
class Tool:
    """A description of a tool, not a way to run one.

    input_schema and output_schema are deliberately loose -- a name mapped
    to a type name, not a validating JSON Schema. Their job here is to
    document what a tool would take and give back so a router can build
    plausible arguments; the validation that matters happens where execution
    happens, which is not this layer.
    """

    name: str
    kind: str
    description: str
    input_schema: dict = field(default_factory=dict)
    output_schema: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolInvocation:
    """One tool a plan step would call, with the arguments it would take.

    A record of an intention, never of an event. Nothing about holding a
    ToolInvocation means anything ran -- see tool_executor for why that
    distinction is worth guarding rather than merely noting.
    """

    tool_name: str
    args: dict
    step_id: str

    @property
    def label(self) -> str:
        """How this reads in a listing: "read_file build.md"."""
        target = self.args.get("path") or self.args.get("target") or ""
        return f"{self.tool_name} {target}".strip()


@dataclass(frozen=True)
class ToolResult:
    """What one invocation produced.

    Distinct from backend.core.tool_registry.ToolResult, which is the
    registry's own (ok / value / error / error_code / sandbox) return type.
    This is the planning layer's view: which step asked, what happened, and
    a line short enough to put in a prompt.

    summary is the only field the prompt prints, so it carries the whole
    truth of the outcome -- a summary that reads like success when status is
    an error would be a lie no downstream reader could catch. payload holds
    the tool's full return value for a caller that wants it.
    """

    tool_name: str
    step_id: str
    status: str
    summary: str
    payload: dict | None = None

    @property
    def ran(self) -> bool:
        """Whether this actually reached the tool."""
        return self.status not in NOT_RUN_STATUSES

    @property
    def line(self) -> str:
        """How this reads in a prompt listing."""
        return f"{self.tool_name} ({self.step_id}): {self.summary}"


READ_FILE = Tool(
    name="read_file",
    kind="read",
    description="Read a file's contents so the answer can work from more than an excerpt.",
    input_schema={"path": "string"},
    output_schema={"path": "string", "text": "string", "bytes": "integer"},
)

EDIT_FILE = Tool(
    name="edit_file",
    kind="edit",
    description="Apply a described change to a file.",
    input_schema={"path": "string", "change": "string"},
    output_schema={"path": "string", "applied": "boolean", "diff": "string"},
)

RUN_TESTS = Tool(
    name="run_tests",
    kind="test",
    description="Run the project's tests and report what passed and failed.",
    input_schema={"scope": "string"},
    output_schema={"passed": "integer", "failed": "integer", "output": "string"},
)

WEB_SEARCH = Tool(
    name="web_search",
    kind="search",
    description="Search the web and return summarized results.",
    input_schema={"query": "string"},
    output_schema={"raw": "object", "reply": "string"},
)

WEATHER = Tool(
    name="weather",
    kind="weather",
    description="Get current weather for a named location.",
    input_schema={"location": "string"},
    output_schema={"temperature": "number", "conditions": "string", "provider": "string"},
)

TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in (READ_FILE, EDIT_FILE, RUN_TESTS, WEB_SEARCH, WEATHER)
}

# Which tool a plan step of each kind would need. "analyze" and "summarize"
# are absent on purpose: they are reasoning, and a tool that claimed to do
# them would be a tool that does the model's job badly.
TOOLS_BY_KIND: dict[str, Tool] = {
    READ_FILE.kind: READ_FILE,
    EDIT_FILE.kind: EDIT_FILE,
    RUN_TESTS.kind: RUN_TESTS,
    WEB_SEARCH.kind: WEB_SEARCH,
    WEATHER.kind: WEATHER,
}


def tool_for_kind(kind: str) -> Tool | None:
    """The tool a plan step of this kind would call, or None for reasoning."""
    return TOOLS_BY_KIND.get(kind)
