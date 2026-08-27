"""ARIA Lite - matching the model to the turn when evidence is involved.

Reading three sources, noticing they disagree, and writing one answer that
says which is which is a different job from answering a question. A model
can be perfectly good at the second and not up to the first, and the
failure is quiet: it produces fluent text that ignores the evidence it was
handed.

Measured on this repo, same query, same stubbed lookup, price sitting in
the prompt:

    nemo-12b / mistral-7b / gpt-4   use the evidence
    phi-3-mini (3.8B)               "I'm unable to provide real-time stock
                                     prices, but I can perform a web search
                                     for you"
    qwen-0.5b (0.5B)                uses the evidence, then degenerates

Note what that rules out. A parameter floor was the obvious rule and it
does not work: phi-3-mini is 3.8B and clears any threshold that qwen-0.5b
fails, yet phi-3 is the one that ignores evidence and qwen is not. The
capability that matters here is not size, so this is an allowlist of models
observed to do the job rather than a number.

Three routes when the resolved model is not on the list, chosen by what is
actually available:

    cloud available      hand the turn to cloud routing
    local-only mode      keep the model, simplify what is asked of it
    neither              do not ask a model at all; show the evidence

The third is the important one. A small model given evidence it cannot use
will not say so -- it will answer anyway, from its weights, which is the
failure the whole evidence pipeline exists to prevent. Printing what the
lookup returned is a worse answer and a truthful one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

__all__ = [
    "EVIDENCE_MODEL_ALLOWLIST",
    "EVIDENCE_TOOLS",
    "ROUTE_CLOUD",
    "ROUTE_NORMAL",
    "ROUTE_RAW_EVIDENCE",
    "ROUTE_SIMPLIFIED",
    "ROUTE_DEFERRED_TO_ROUTER",
    "SYNTHESIS_FULL",
    "SYNTHESIS_RAW_EVIDENCE",
    "SYNTHESIS_SIMPLIFIED",
    "NormalizedEvidenceItem",
    "SNIPPET_MAX_CHARS",
    "TITLE_MAX_CHARS",
    "choose_route",
    "extract_tool_results",
    "format_evidence_block",
    "has_usable_evidence",
    "normalize_evidence",
    "normalize_tool_line",
    "normalize_tool_line_all",
    "format_raw_evidence",
    "is_evidence_bearing",
    "model_can_synthesize_evidence",
    "simplified_prompt",
]


# Tools whose output is evidence a model has to read and reconcile.
EVIDENCE_TOOLS = frozenset({
    "web_search",
    "file_lookup",
    "news_search",
    "product_search",
})

# Models observed to read evidence and answer from it rather than around
# it. Matched by prefix, because registry ids carry a quantisation suffix
# ("mistral-7b" is registered as "mistral-7b-q4km") and a re-quantised
# rebuild of the same model is the same model for this purpose.
EVIDENCE_MODEL_ALLOWLIST = frozenset({
    "nemo-12b",
    "mistral-7b",
})

# What the orchestrator decided to do about it.
ROUTE_NORMAL = "normal"
ROUTE_CLOUD = "cloud_fallback_for_evidence"
ROUTE_SIMPLIFIED = "simplified_local_synthesis"
ROUTE_RAW_EVIDENCE = "raw_evidence_fallback"
# Automatic mode: no model id exists yet, so the allowlist has nothing to
# check and ProviderRouter decides. Recorded rather than acted on --
# complexity_router floors the tier for an evidence-bearing prompt, and
# pinning a model here would expose the turn to the safety gate that
# model_id=None skips. See turn_orchestrator step 5c.
ROUTE_DEFERRED_TO_ROUTER = "deferred_to_provider_router"

# What the turn ends up asking of the model, reported on the TurnResult.
SYNTHESIS_FULL = "full"
SYNTHESIS_SIMPLIFIED = "simplified_local"
SYNTHESIS_RAW_EVIDENCE = "raw_evidence"


def is_evidence_bearing(tool_runs) -> bool:
    """Whether this turn produced evidence a model has to read.

    Takes what actually ran, not what was expected to. Weather never
    reaches here (the fusion engine answers it without a model),
    self-knowledge answers from the registry, and trivial and model-switch
    intents run no tools at all -- so all four are False by construction
    rather than by a special case.
    """
    return any(name in EVIDENCE_TOOLS for name in (tool_runs or ()))


def model_can_synthesize_evidence(model_id: str | None) -> bool:
    """Whether this model is trusted to answer from supplied evidence.

    model_id is None for cloud and Automatic routing, where ProviderRouter
    picks a frontier model -- those are trusted.
    """
    if not model_id:
        return True
    lowered = model_id.lower()
    return any(lowered.startswith(allowed) for allowed in EVIDENCE_MODEL_ALLOWLIST)


def choose_route(
    *,
    expects_evidence: bool,
    model_id: str | None,
    mode: str,
    cloud_available: bool,
) -> str:
    """Which of the three fallbacks this turn needs, if any.

    Called at model-resolution time, before the tools have run, so
    `expects_evidence` is a prediction. It has to be: the model is resolved
    and safety-checked several steps before Engine B executes anything, and
    swapping the model afterwards would mean the resource projection was
    made against a model that is no longer the one being loaded.

    Local Mode never routes to cloud, whatever else is true. Absolute mode
    separation is not a preference to be traded against answer quality --
    a user in Local Mode has said their data does not leave the machine,
    and a better answer is not worth breaking that.
    """
    if not expects_evidence or model_can_synthesize_evidence(model_id):
        return ROUTE_NORMAL

    if mode == "local":
        return ROUTE_SIMPLIFIED
    if cloud_available:
        return ROUTE_CLOUD
    return ROUTE_RAW_EVIDENCE


# ======================================================
# Reading the evidence back out
# ======================================================
# Engine B renders tool output into the synthesis prompt under a fixed
# heading and returns the assembled string. Options B and C need those
# lines on their own, so they are read back out of the prompt rather than
# by running the tools a second time -- one execution, one set of results,
# and no duplicate of the plan/route/execute sequence that Engine B owns.
#
# The coupling to that heading is real and is pinned by a test, so a change
# to the section format fails loudly here instead of silently returning
# nothing.
_TOOL_SECTION_HEADING = "Tool Results:"


def extract_tool_results(prompt: str) -> list[str]:
    """The Tool Results lines from an assembled synthesis prompt."""
    if not prompt or _TOOL_SECTION_HEADING not in prompt:
        return []

    _, _, rest = prompt.partition(_TOOL_SECTION_HEADING)
    lines = []
    for line in rest.splitlines():
        stripped = line.strip()
        if not stripped:
            if lines:
                break          # blank line ends the section
            continue
        if not stripped.startswith("-"):
            if stripped.startswith("("):
                continue       # the "(N of these failed...)" note
            break              # the next section has started
        lines.append(stripped.lstrip("- ").strip())
    return lines


def format_raw_evidence(evidence: list[str], query: str = "") -> str:
    """The lookup's own words, with one line of framing and no model.

    Deliberately plain. This path exists because no model available to this
    turn can be trusted to summarise the evidence without talking around
    it, so nothing here paraphrases: the user reads what the lookup
    returned and draws their own conclusion.
    """
    items = evidence if _already_normalized(evidence) else normalize_evidence(evidence)

    if not has_usable_evidence(items):
        return (
            "I looked this up but the search returned nothing usable, and the "
            "model available for this turn is not one I'd trust to answer "
            "without evidence."
        )

    lead = (
        "Here is what the search returned. I'm showing it as-is rather than "
        "summarising it, because the model available for this turn isn't one "
        "I'd trust to read evidence accurately."
    )
    return "\n".join([lead, "", format_evidence_block(items)])


def simplified_prompt(query: str, evidence: list[str]) -> str:
    """A small prompt for a small model: one question, the findings, answer.

    Everything the full synthesis prompt does to help a capable model --
    the evidence summary, the plan, conflict detection, the eight synthesis
    rules -- is removed, because on a small model that structure is the
    problem rather than the help. It is a document to be continued, and it
    gets continued: the labels come back in the answer, and the
    instructions get restated. What is left here is a question and some
    facts.
    """
    items = evidence if _already_normalized(evidence) else normalize_evidence(evidence)
    lines = [f"Question: {query}", "", "What the lookup returned:",
             format_evidence_block(items)]
    lines += [
        "",
        "Answer the question in one or two sentences, using only the lines "
        "above. Do not add anything you were not told. If they do not answer "
        "the question, say so.",
    ]
    return "\n".join(lines)

# ======================================================
# Normalizing what a lookup returned
# ======================================================
# A tool result reaches this layer as one rendered line, because the
# structured results live inside Engine B and are not ours to reshape.
# What that line carries is recoverable; what it never carried cannot be
# invented, so title and timestamp come back None for a web_search rather
# than being guessed from the snippet. Fabricating a field here would be
# the same class of error as fabricating an answer.
#
# The shape is fixed and total: every field is always present, None when
# absent, so no consumer has to write `.get(...)` and no consumer can be
# surprised by a missing key.
TITLE_MAX_CHARS = 120
SNIPPET_MAX_CHARS = 300

# Tool priority for ordering. Only web_search exists today; the others are
# listed because the routing layer already names them as evidence tools,
# so the order is decided once rather than when they arrive.
_TOOL_PRIORITY = {
    "web_search": 0,
    "news_search": 1,
    "product_search": 2,
    "file_lookup": 3,
}

# Text a tool emits when it ran and found nothing. These are not evidence,
# and treating them as evidence is what let a turn tell the model "use ONLY
# the evidence above" over an empty string.
_EMPTY_PLACEHOLDERS = (
    "search returned no summary",
    # What the provider-backed web_search says when no provider could
    # serve the query. Listed here for the same reason as the others: it
    # is the tool reporting a miss, and a miss read as a finding is how a
    # model ends up told to answer from "No results found."
    "no results found",
    "no summary",
    "none",
    "n/a",
)

# "web_search (step1): the finding" -- the prefix Engine B renders.
_LINE_PREFIX = re.compile(r"^(?P<tool>[a-z][a-z0-9_]*)\s*\(step(?P<rank>\d+)\)\s*:\s*", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s)\]}>,]+", re.IGNORECASE)

# The structure backend/tools/tool_executor.py renders into that line.
# It has to ride inside a string because the prompt builder emits one
# bullet per tool result and is not ours to change -- so the tool layer
# encodes, and this reads it back. The two are a pair: a change to either
# delimiter breaks the other, which is why a test asserts the round trip
# rather than each half separately.
_TITLE_PREFIX = re.compile(r"^\[(?P<title>[^\]]{1,200})\]\s*")
_TRAILING_TIMESTAMP = re.compile(r"\s*\[(?P<stamp>\d{4}-\d{2}-\d{2}[T ][^\]]+)\]\s*$")
_ALSO_SEPARATOR = " | also: "


@dataclass(frozen=True)
class NormalizedEvidenceItem:
    """One piece of evidence, in the one shape every consumer reads.

    `raw` keeps the line exactly as it arrived, so nothing here is lossy --
    a caller that needs what normalization discarded can still get at it.
    """

    source: str
    title: str | None = None
    snippet: str | None = None
    url: str | None = None
    timestamp: datetime | None = None
    tool: str | None = None
    rank: int = 0
    raw: str = ""

    @property
    def usable(self) -> bool:
        """Whether this actually says anything.

        A result that ran and returned nothing is not evidence. Neither is
        a serialized API envelope: a DuckDuckGo reply with no answer comes
        back as its own empty JSON, and printing that under "what the
        lookup returned" tells the model a blob is a finding.
        """
        text = (self.snippet or "").strip()
        if not text:
            return False
        if text.lower().strip(" .") in _EMPTY_PLACEHOLDERS:
            return False
        return not _looks_like_serialized_json(text)

    def as_dict(self) -> dict:
        return {
            "tool": self.tool,
            "rank": self.rank,
            "source": self.source,
            "title": self.title,
            "snippet": self.snippet,
            "url": self.url,
            "timestamp": self.timestamp,
        }


def _looks_like_serialized_json(text: str) -> bool:
    """Whether this is an API envelope rather than a sentence.

    Checked by parsing, not by pattern: a sentence that happens to start
    with a brace is rare, and a blob that parses to a structure with no
    text in it is exactly what should not reach a model as evidence.
    """
    stripped = text.strip()
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return False
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return False
    return not _json_carries_text(parsed)


def _json_carries_text(value) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_json_carries_text(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_json_carries_text(v) for v in value)
    return False


def _trim(text: str | None, limit: int) -> str | None:
    """Cut at a word boundary, never mid-word."""
    if text is None:
        return None
    cleaned = " ".join(text.split())
    if not cleaned:
        return None
    if len(cleaned) <= limit:
        return cleaned

    cut = cleaned[:limit]
    boundary = cut.rfind(" ")
    if boundary > limit // 2:
        cut = cut[:boundary]
    return cut.rstrip(" ,;:") + "\u2026"


def _valid_url(candidate: str | None) -> str | None:
    """A URL only if it has a scheme we would actually follow."""
    if not candidate:
        return None
    candidate = candidate.strip().rstrip(".,;)")
    lowered = candidate.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        return None
    return candidate if len(candidate) > len("https://") else None


def _parse_timestamp(candidate) -> datetime | None:
    if isinstance(candidate, datetime):
        return candidate
    if not candidate:
        return None
    text = str(candidate).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _parse_one(body: str, tool, rank: int, raw: str) -> NormalizedEvidenceItem:
    """One finding out of one rendered segment."""
    text = body.strip()

    title = None
    title_match = _TITLE_PREFIX.match(text)
    if title_match:
        title = _trim(title_match.group("title"), TITLE_MAX_CHARS)
        text = text[title_match.end():].strip()

    timestamp = None
    stamp_match = _TRAILING_TIMESTAMP.search(text)
    if stamp_match:
        timestamp = _parse_timestamp(stamp_match.group("stamp"))
        if timestamp:
            text = text[: stamp_match.start()].strip()

    url_match = _URL.search(text)
    url = _valid_url(url_match.group(0)) if url_match else None
    if url:
        # Printed on its own line by format_evidence_block, so leaving it
        # in the snippet too would show the same link twice and eat the
        # snippet's character budget.
        text = text.replace(url_match.group(0), "").strip(" \t()-\u2013\u2014")

    return NormalizedEvidenceItem(
        source=url or tool or "tool",
        title=title,
        snippet=_trim(text, SNIPPET_MAX_CHARS),
        url=url,
        timestamp=timestamp,
        tool=tool,
        rank=rank,
        raw=raw,
    )


def normalize_tool_line(line: str, rank: int = 0) -> NormalizedEvidenceItem:
    """One rendered Tool Results line, in the unified shape.

    Never returns None: an unusable line is still an item, marked unusable,
    so the caller can count what ran separately from what it found. A line
    carrying several findings yields the first here; use
    normalize_tool_line_all to get every one.
    """
    return normalize_tool_line_all(line, rank)[0]


def normalize_tool_line_all(line: str, rank: int = 0) -> list:
    """Every finding in one rendered line.

    A search returns an instant answer and its related topics, and the
    prompt builder renders one bullet per tool result -- so several
    findings arrive inside one string. Splitting them back out is what
    lets each keep its own URL and rank instead of being read as one
    undifferentiated paragraph.
    """
    raw = line or ""
    match = _LINE_PREFIX.match(raw.strip())

    tool = match.group("tool").lower() if match else None
    base_rank = int(match.group("rank")) if match else (rank or 1)
    body = raw.strip()[match.end():].strip() if match else raw.strip()

    primary, _, extras = body.partition(_ALSO_SEPARATOR)
    segments = [primary] + [part for part in extras.split("; ") if part.strip()]

    items = [
        _parse_one(segment, tool, base_rank + offset, raw)
        for offset, segment in enumerate(segments)
    ]
    return items or [_parse_one("", tool, base_rank, raw)]


def _already_normalized(evidence) -> bool:
    return bool(evidence) and isinstance(evidence[0], NormalizedEvidenceItem)


def _dedupe(items: list) -> list:
    """Same URL, or same title+snippet, is the same finding said twice."""
    seen_urls: set[str] = set()
    seen_text: set[tuple] = set()
    unique = []

    for item in items:
        if item.url and item.url in seen_urls:
            continue
        key = (item.title, item.snippet)
        if key in seen_text:
            continue
        if item.url:
            seen_urls.add(item.url)
        seen_text.add(key)
        unique.append(item)
    return unique


def _sort_key(item):
    # Newest first, so the timestamp is negated -- and an item without one
    # sorts after those that have one rather than before, because "unknown
    # when" is not "just now". rank breaks any remaining tie so the order
    # is total and the same evidence always merges the same way.
    priority = _TOOL_PRIORITY.get(item.tool or "", len(_TOOL_PRIORITY))
    has_time = 0 if item.timestamp else 1
    stamp = -item.timestamp.timestamp() if item.timestamp else 0.0
    # rank before title, deliberately. rank is the tool's own ordering --
    # a search's instant answer is rank 1 and its related topics follow --
    # and sorting on title first buried the direct answer beneath its own
    # footnotes, because the related items carry no title and "" sorts
    # ahead of every real one. Title only breaks a tie that rank left.
    return (priority, has_time, stamp, item.rank, (item.title or "").lower())


def normalize_evidence(lines) -> list:
    """Rendered evidence lines, normalized, deduplicated and ordered.

    Deterministic by construction: the sort key is total, so the same
    input always produces the same list in the same order.
    """
    items = []
    for index, line in enumerate(lines or []):
        if str(line or "").strip():
            items.extend(normalize_tool_line_all(line, rank=index + 1))
    return sorted(_dedupe(items), key=_sort_key)


def has_usable_evidence(items) -> bool:
    """Whether any of this actually says something."""
    return any(item.usable for item in (items or []))


NO_USABLE_TEXT = "The lookup returned results, but none contained usable text."


def format_evidence_block(items) -> str:
    """The "what the lookup returned" body, one item per block.

    Title, then snippet, then URL, then timestamp -- and any field that is
    absent is simply not printed. A heading over a blank line, or a label
    with nothing after it, invites the model to fill the gap.
    """
    usable = [item for item in (items or []) if item.usable]
    if not usable:
        return NO_USABLE_TEXT

    blocks = []
    for item in usable:
        lines = []
        if item.title:
            lines.append(_trim(item.title, TITLE_MAX_CHARS))
        if item.snippet:
            lines.append(item.snippet)
        if item.url:
            lines.append(item.url)
        if item.timestamp:
            lines.append(item.timestamp.isoformat())
        blocks.append("\n".join(f"- {line}" if index == 0 else f"  {line}"
                                for index, line in enumerate(lines)))
    return "\n\n".join(blocks)

