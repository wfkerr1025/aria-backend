"""ARIA Lite Phase 9 - the tool layer for planned execution.

    tool_registry  what tools exist, as data
    tool_router    Plan -> ToolInvocations (deterministic)
    tool_executor  ToolInvocations -> results (currently: executes nothing)

Distinct from backend/core/tool_registry.py, tool_router.py and
tool_executor.py, which share these names and are the live path: registered
handlers, permission checks, real provider calls. This package is the
planning-side wiring, and every module in it is pure. See each module's
docstring for which of the two it is.
"""

from __future__ import annotations

__all__ = [
    "Tool",
    "ToolExecutor",
    "ToolInvocation",
    "ToolRouter",
    "execute_invocations",
    "route_plan",
]


def __getattr__(name: str):
    if name in ("Tool", "ToolInvocation"):
        from backend.tools import tool_registry

        return getattr(tool_registry, name)
    if name in ("ToolRouter", "route_plan"):
        from backend.tools import tool_router

        return getattr(tool_router, name)
    if name in ("ToolExecutor", "execute_invocations"):
        from backend.tools import tool_executor

        return getattr(tool_executor, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
