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

from dataclasses import dataclass
from datetime import datetime

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


# ============================================================
# NORMALIZING A TOOL RESULT
#
# A tool answers with structure and this layer renders it to one line, so
# whatever the line does not carry is gone by the time anything downstream
# sees it. For web_search that was almost everything: _search_handler
# returns {"raw": <the rich result>, "reply": <one string>}, where the rich
# result holds the heading, the source URL and every related topic -- and
# only `reply` was read.
#
# So a search that found "Microsoft Corporation - MSFT is trading at
# $412.30 - investopedia.com, plus four related results" reached the model
# as a single untitled, unattributed sentence.
#
# Normalizing first and rendering second keeps those fields. What cannot
# be recovered stays None: DuckDuckGo's instant-answer API carries no
# timestamp, so timestamp is None for a web_search rather than being
# filled in with "now" -- a fabricated date on a stale result is worse
# than no date.
TITLE_CHARS = 120
SNIPPET_CHARS = 300
MAX_RELATED_ITEMS = 4


@dataclass(frozen=True)
class NormalizedToolResult:
    """One finding from one tool, before it is rendered to a line."""

    tool: str
    source: str
    title: str | None = None
    snippet: str | None = None
    url: str | None = None
    timestamp: "datetime | None" = None
    rank: int = 1
    raw: object = None

    def as_dict(self) -> dict:
        return {
            "tool": self.tool,
            "source": self.source,
            "title": self.title,
            "snippet": self.snippet,
            "url": self.url,
            "timestamp": self.timestamp,
            "rank": self.rank,
        }


def _clip(text, limit: int):
    """Trim at a word boundary, or None if there is nothing to trim."""
    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return None
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[:limit]
    boundary = cut.rfind(" ")
    if boundary > limit // 2:
        cut = cut[:boundary]
    return cut.rstrip(" ,;:") + "\u2026"


def _url_or_none(candidate):
    """A URL only if it carries a scheme we would actually follow."""
    text = str(candidate or "").strip().rstrip(".,;)")
    lowered = text.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return text if len(text) > len("https://") else None
    return None


def _timestamp_or_none(candidate):
    from datetime import datetime as _dt

    if isinstance(candidate, _dt):
        return candidate
    if not candidate:
        return None
    try:
        return _dt.fromisoformat(str(candidate).strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_web_search(value) -> list:
    """The instant answer, then the related topics, in the order given.

    Rank is assigned by position rather than by score, because DuckDuckGo
    does not return one -- and an invented ranking would read exactly like
    a real one.
    """
    raw = value.get("raw") if isinstance(value, dict) else None
    if not isinstance(raw, dict):
        # Nothing structured survived; fall back to the rendered reply so
        # the finding is not lost entirely.
        snippet = _clip(value.get("reply") if isinstance(value, dict) else value, SNIPPET_CHARS)
        if not snippet:
            return []
        return [NormalizedToolResult(
            tool="web_search", source="web_search", snippet=snippet, raw=value,
        )]

    items = []
    abstract = _clip(raw.get("summary"), SNIPPET_CHARS)
    if abstract:
        items.append(NormalizedToolResult(
            tool="web_search",
            source=_url_or_none(raw.get("source_url")) or "web_search",
            title=_clip(raw.get("heading"), TITLE_CHARS),
            snippet=abstract,
            url=_url_or_none(raw.get("source_url")),
            timestamp=_timestamp_or_none(raw.get("timestamp")),
            rank=len(items) + 1,
            raw=raw,
        ))

    for related in (raw.get("related") or [])[:MAX_RELATED_ITEMS]:
        if not isinstance(related, dict):
            continue
        snippet = _clip(related.get("text"), SNIPPET_CHARS)
        if not snippet:
            continue
        url = _url_or_none(related.get("url"))
        items.append(NormalizedToolResult(
            tool="web_search",
            source=url or "web_search",
            title=None,
            snippet=snippet,
            url=url,
            timestamp=None,
            rank=len(items) + 1,
            raw=related,
        ))

    return items


def _normalize_read_file(value) -> list:
    if not isinstance(value, dict):
        return []
    path = value.get("path")
    snippet = _clip(value.get("text") or value.get("content"), SNIPPET_CHARS)
    if not (path or snippet):
        return []
    return [NormalizedToolResult(
        tool="read_file", source=str(path or "read_file"),
        title=_clip(path, TITLE_CHARS), snippet=snippet, url=None,
        timestamp=_timestamp_or_none(value.get("modified")), rank=1, raw=value,
    )]


def _normalize_search_notes(value) -> list:
    """One item per note, so a five-note answer is five findings."""
    rows = value.get("results") or value.get("notes") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        return []

    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        snippet = _clip(row.get("text") or row.get("content"), SNIPPET_CHARS)
        if not snippet:
            continue
        items.append(NormalizedToolResult(
            tool="search_notes",
            source=str(row.get("id") or "search_notes"),
            title=_clip(row.get("title"), TITLE_CHARS),
            snippet=snippet, url=None,
            timestamp=_timestamp_or_none(row.get("created") or row.get("timestamp")),
            rank=len(items) + 1, raw=row,
        ))
    return items


def _normalize_weather(value) -> list:
    if not isinstance(value, dict):
        return []
    parts = [str(value[key]) for key in ("temperature", "conditions") if value.get(key)]
    if not parts:
        return []
    where = value.get("location") or value.get("place")
    return [NormalizedToolResult(
        tool="weather", source=str(value.get("provider") or "weather"),
        title=_clip(where, TITLE_CHARS), snippet=_clip(", ".join(parts), SNIPPET_CHARS),
        url=None, timestamp=_timestamp_or_none(value.get("timestamp")), rank=1, raw=value,
    )]


_NORMALIZERS = {
    "web_search": _normalize_web_search,
    "read_file": _normalize_read_file,
    "search_notes": _normalize_search_notes,
    "weather": _normalize_weather,
}


def normalize_tool_value(tool_name: str, value) -> list:
    """Every finding this tool returned, in the unified shape.

    An empty list means the tool ran and found nothing -- which is a
    different thing from failing, and a different thing again from
    succeeding, and the caller needs to be able to tell all three apart.
    """
    normalizer = _NORMALIZERS.get(tool_name)
    if normalizer is None:
        return []
    try:
        return normalizer(value)
    except Exception:  # pragma: no cover - a normalizer must never fail a turn
        logger.exception("normalize_tool_value(%s) failed; falling back to the summary", tool_name)
        return []


# Delimiters for rendering several findings into the one line this layer
# is allowed to produce. Chosen to be unambiguous to parse and readable in
# a prompt: a bracketed title cannot be confused with prose, and " | also:
# " does not occur in ordinary sentences.
TITLE_OPEN, TITLE_CLOSE = "[", "]"
ALSO_SEPARATOR = " | also: "


def render_normalized(items) -> str:
    """The findings as one line, keeping title and source attached.

    One line because that is the shape the prompt builder consumes -- it
    renders "- {result.line}" per result and is not ours to change. So the
    structure rides inside the line in a form that survives being read
    back out.
    """
    if not items:
        return ""

    def render(item) -> str:
        head = f"{TITLE_OPEN}{item.title}{TITLE_CLOSE} " if item.title else ""
        tail = f" ({item.url})" if item.url else ""
        stamp = f" [{item.timestamp.isoformat()}]" if item.timestamp else ""
        return f"{head}{item.snippet or ''}{tail}{stamp}".strip()

    primary = render(items[0])
    extras = [render(item) for item in items[1:] if item.snippet]
    return primary + (ALSO_SEPARATOR + "; ".join(extras) if extras else "")


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
        # Normalized first: the heading, the source URL and the related
        # topics all live in value["raw"] and used to be dropped here.
        rendered = render_normalized(normalize_tool_value("web_search", value))
        if rendered:
            return rendered
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
