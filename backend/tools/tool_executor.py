"""ARIA Lite Phase 9.2 - running the tools a plan asked for.

NOT backend/core/tool_executor.py, which holds the hand-wired weather and
search paths. This one takes the invocations a router produced and puts each
through the registry's single execution path.

Every invocation goes through backend.core.tool_registry.execute_tool(),
never around it. That function is where argument validation, the permission
gate and the sandbox timeout live, and a second way in would be a second way
to skip all three. This module's whole job is the translation either side of
it: ToolInvocation in, ToolResult out.

Three properties are load-bearing, and each is a bug this module has already
had or could easily have:

    Nothing is dropped. Every invocation produces exactly one result, in
    order, whatever is wrong with it. A malformed entry becomes a result
    with status "invalid_invocation" rather than vanishing -- the previous
    version skipped those with a bare `continue` while its own docstring
    claimed none were ever dropped, which meant a caller comparing lengths
    could not tell which one went missing.

    Failure is never silent and never reads as success. A refused
    permission, a bad path, a timeout: each becomes status "error" with the
    registry's own message in the summary. There is no branch here that
    turns a failure into an empty success.

    Summaries state what happened, not what was hoped for. The prompt prints
    the summary and nothing else, so "read 412 bytes" is written only where
    412 bytes were actually read.

No model, no randomization, no retry. Running the same invocation twice
calls the tool twice -- this layer does not deduplicate, because a caller
that asked for two reads is entitled to two reads. Idempotence, where it
matters, is the tool's own property: see file_tools.edit_file, which skips a
write whose content is already on disk.
"""

from __future__ import annotations

try:
    from backend.tools.tool_registry import (
        STATUS_ERROR,
        STATUS_INVALID,
        STATUS_OK,
        ToolInvocation,
        ToolResult,
    )
except ImportError:  # running from inside the backend directory
    from tools.tool_registry import (
        STATUS_ERROR,
        STATUS_INVALID,
        STATUS_OK,
        ToolInvocation,
        ToolResult,
    )

from logger import get_logger

logger = get_logger(__name__)

__all__ = ["ToolExecutor", "execute_invocations", "summarize"]

# How much tool output reaches a summary line. The payload keeps everything;
# this is what a prompt can afford to read.
SUMMARY_CHARS = 200


def _shorten(text: str, limit: int = SUMMARY_CHARS) -> str:
    collapsed = " ".join(str(text or "").split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def summarize(tool_name: str, value) -> str:
    """One line describing what a tool returned.

    Per-tool rather than generic, because the useful fact differs: for a
    read it is the size, for an edit whether anything was written, for a
    test run the exit code. A generic dump would put 200 characters of file
    contents where "read 412 bytes" belongs.

    Every branch reports the outcome that actually occurred. In particular
    an edit that only previewed says so, because "edited build.md" for a
    call that wrote nothing is the exact false claim this layer exists to
    prevent.
    """
    if not isinstance(value, dict):
        return _shorten(value)

    if tool_name == "read_file":
        suffix = " (truncated)" if value.get("truncated") else ""
        return f"read {value.get('bytes', 0)} bytes from {value.get('path', '?')}{suffix}"

    if tool_name == "edit_file":
        path = value.get("path", "?")
        if value.get("applied"):
            verb = "created" if value.get("created") else "wrote"
            return f"{verb} {path}"
        if value.get("reason") == "unchanged":
            return f"no change needed to {path}; contents already match"
        return f"previewed a change to {path}; nothing written (confirm required)"

    if tool_name == "web_search":
        reply = _shorten(value.get("reply") or "")
        return reply or "search returned no summary"

    if tool_name == "weather":
        # Only the fields the provider actually filled. weather_router
        # guarantees these on success, but a summary that printed "None°"
        # for a partial result would read as a reading rather than a gap.
        parts = [str(value[key]) for key in ("temperature", "conditions") if value.get(key)]
        where = value.get("location") or value.get("place") or ""
        provider = value.get("provider")
        head = ", ".join(parts) if parts else "no reading returned"
        return _shorten(
            f"{where + ': ' if where else ''}{head}"
            + (f" (via {provider})" if provider else "")
        )

    if tool_name == "run_tests":
        outcome = "passed" if value.get("passed") else "failed"
        return (
            f"tests {outcome} (exit {value.get('exit_code')}) for "
            f"{value.get('scope') or 'the whole suite'}"
        )

    return _shorten(value)


class ToolExecutor:
    """Runs invocations through the core registry, in order.

    allowed_permissions is passed straight to execute_tool. None means the
    caller is trusted with everything, which is the registry's own
    convention; a host that should not be reaching the filesystem passes a
    narrower set and gets a permission error per invocation rather than a
    silent skip.
    """

    def __init__(self, allowed_permissions=None) -> None:
        self.allowed_permissions = allowed_permissions

    def execute(self, invocations) -> list[ToolResult]:
        """One ToolResult per invocation, in the order given.

        Total: every input produces an output. len(results) == len(inputs)
        always holds, and it is tested, because a caller comparing the two
        has no other way to learn that something went missing.
        """
        try:
            from backend.core.tool_registry import execute_tool
        except ImportError:  # running from inside the backend directory
            from core.tool_registry import execute_tool

        results: list[ToolResult] = []
        for position, invocation in enumerate(invocations or []):
            if not isinstance(invocation, ToolInvocation):
                results.append(
                    ToolResult(
                        tool_name=getattr(invocation, "tool_name", "unknown"),
                        step_id=getattr(invocation, "step_id", f"index{position}"),
                        status=STATUS_INVALID,
                        summary=(
                            "not a ToolInvocation, so nothing was run: "
                            f"{type(invocation).__name__}"
                        ),
                        payload=None,
                    )
                )
                continue

            outcome = execute_tool(
                invocation.tool_name,
                dict(invocation.args),
                allowed_permissions=self.allowed_permissions,
            )

            if outcome.ok:
                results.append(
                    ToolResult(
                        tool_name=invocation.tool_name,
                        step_id=invocation.step_id,
                        status=STATUS_OK,
                        summary=summarize(invocation.tool_name, outcome.value),
                        payload=outcome.value if isinstance(outcome.value, dict) else None,
                    )
                )
                continue

            logger.warning(
                "tool %s (%s) failed: %s", invocation.tool_name, invocation.step_id, outcome.error
            )
            results.append(
                ToolResult(
                    tool_name=invocation.tool_name,
                    step_id=invocation.step_id,
                    status=STATUS_ERROR,
                    summary=_shorten(outcome.error or "failed with no message"),
                    payload={"error_code": outcome.error_code} if outcome.error_code else None,
                )
            )
        return results


def execute_invocations(invocations, allowed_permissions=None) -> list[ToolResult]:
    """Convenience wrapper around a default ToolExecutor."""
    return ToolExecutor(allowed_permissions=allowed_permissions).execute(invocations)
