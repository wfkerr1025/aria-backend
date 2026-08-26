"""ARIA Lite - memory-aware prompting.

Assembles what ARIA knows into a structured bundle that can be folded into
an LLM call: the highest-ranked memory items for the question being asked, a
summary of them, which retrieval path produced them, what ARIA knows about
itself, and what the user has told it to remember.

Sits on top of the retrieval stack rather than duplicating it --
semantic_embeddings encodes, aria_memory.search_hybrid retrieves,
memory_ranking orders, and this module only decides what to say about the
result.

On summarization
----------------
summarize_items() is EXTRACTIVE and deterministic: it states counts, types,
scores and quoted item text, and nothing else. It cannot hallucinate because
it never generates -- every word not in the template comes verbatim from an
item field.

That is a deliberate choice over generating the summary with the LLM. A
generated summary would be (a) non-deterministic in general, (b) unable to
guarantee it stays inside the item fields, and (c) an extra model call on
the critical path of every single chat turn -- and, if the summarizer ran
through this same prompt builder, recursive. An LLM path is available via
summarize_items_with_llm() for callers who want prose and can accept those
costs; it is not the default and is never used by build_memory_context()
unless explicitly enabled.
"""

from __future__ import annotations

import os

from logger import get_logger

try:
    from backend import aria_memory
    from backend.aria_memory_context import list_context
    from backend.aria_memory_self import list_self
    from backend.llm import semantic_embeddings
except ImportError:  # running from inside the backend directory
    import aria_memory
    from aria_memory_context import list_context
    from aria_memory_self import list_self
    from llm import semantic_embeddings

logger = get_logger(__name__)

# Set to "1" to let summarize_items() generate prose through the LLM instead
# of composing it from fields. Off by default; see the module docstring.
ENV_LLM_SUMMARY = "ARIA_MEMORY_LLM_SUMMARY"

DEFAULT_LIMIT = 8

# How much of an item's text a summary or prompt line quotes. Long enough to
# recognise the item, short enough that eight of them do not crowd out the
# conversation.
SNIPPET_CHARS = 160

# Below this, recency is called out as a caveat in the summary. Matches the
# ~30-day point of the ranking module's decay curve.
STALE_RECENCY = 0.4

# The item type that gets file-specific wording in a summary.
FILE_CHUNK_TYPE = "file_chunk"


# ======================================================
# Sources
# ======================================================
def load_self_knowledge() -> dict:
    """What ARIA knows about itself, from the aria_self table."""
    try:
        return list_self()
    except Exception:
        logger.exception("Self-knowledge could not be loaded; continuing without it.")
        return {}


def load_user_preferences() -> dict:
    """What the user has told ARIA to remember, from the user_context table."""
    try:
        return list_context()
    except Exception:
        logger.exception("User preferences could not be loaded; continuing without them.")
        return {}


# ======================================================
# Summarization
# ======================================================
def _snippet(item: dict) -> str:
    """One line of an item's own text, flattened and clipped."""
    text = item.get("text") or item.get("chunk") or item.get("value") or ""
    flat = " ".join(str(text).split())
    if len(flat) <= SNIPPET_CHARS:
        return flat
    return flat[: SNIPPET_CHARS - 1].rstrip() + "…"


def _score(item: dict, key: str) -> float:
    value = item.get(key)
    return float(value) if value is not None else 0.0


def _humanize_type(type_name) -> str:
    """The type field as prose: "file_chunk" is a column value, not English."""
    return str(type_name or "item").replace("_", " ")


def _describe(item: dict) -> str:
    return (
        f'a {_humanize_type(item.get("type"))} '
        f'(semantic {_score(item, "semantic_score"):.2f}, '
        f'recency {_score(item, "recency_score"):.2f}): "{_snippet(item)}"'
    )


def _type_breakdown(items: list[dict]) -> str:
    counts: dict[str, int] = {}
    for item in items:
        name = _humanize_type(item.get("type"))
        counts[name] = counts.get(name, 0) + 1
    # Sorted by name so the same set of items always reads the same way.
    parts = [f"{count} {name}{'s' if count != 1 else ''}" for name, count in sorted(counts.items())]
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + f" and {parts[-1]}"


def _headline(items: list[dict]) -> str:
    """The opening sentence, worded for what the items actually are.

    A bundle that is entirely file chunks is describing a document, not
    something the user told ARIA, so "memory item" reads wrong inside a
    File Context block. A mixed bundle keeps the memory wording: it is a
    memory bundle that happens to contain a chunk from a file, and the
    per-type breakdown still names each kind.

    Still extractive -- the counts and the file ids come from the items.
    """
    count = len(items)
    plural = "" if count == 1 else "s"

    if all(item.get("type") == FILE_CHUNK_TYPE for item in items):
        file_ids = {item.get("file_id") for item in items if item.get("file_id") is not None}
        if file_ids:
            file_plural = "" if len(file_ids) == 1 else "s"
            return (
                f"Found {count} relevant file chunk{plural} "
                f"from {len(file_ids)} file{file_plural}."
            )
        return f"Found {count} relevant file chunk{plural}."

    return f"Recalled {count} memory item{plural}: {_type_breakdown(items)}."


def summarize_items(items: list[dict]) -> str:
    """Summarize ranked items in three to six sentences.

    Deterministic and extractive: identical input always produces identical
    output, and every claim comes from a field of an item. Returns "" for an
    empty list -- an empty summary is honest, whereas "no relevant memories
    were found" is a sentence the caller can write itself if it wants one.
    """
    if not items:
        return ""

    if os.environ.get(ENV_LLM_SUMMARY) == "1":
        generated = summarize_items_with_llm(items)
        if generated:
            return generated

    sentences = [_headline(items)]
    sentences.append(f"The closest match is {_describe(items[0])}.")

    for follow_up in items[1:3]:
        sentences.append(f"Also relevant: {_describe(follow_up)}.")

    semantic_scores = [_score(item, "semantic_score") for item in items]
    sentences.append(
        f"Semantic relevance ranges from {min(semantic_scores):.2f} to {max(semantic_scores):.2f}."
    )

    recency_scores = [_score(item, "recency_score") for item in items]
    if min(recency_scores) < STALE_RECENCY:
        sentences.append(
            f"Some of this is old: recency ranges from {min(recency_scores):.2f} "
            f"to {max(recency_scores):.2f}."
        )

    return " ".join(sentences)


def summarize_items_with_llm(items: list[dict]) -> str:
    """Generate the summary with the local model instead of composing it.

    Opt-in. Greedy decoding with a fixed seed makes this reproducible on one
    machine and model, but not across either, and nothing constrains the
    model to the item fields -- which is why it is not the default. Returns
    "" on any failure so the caller falls back to the extractive summary.
    """
    if not items:
        return ""
    try:
        from backend.core import local_inference_engine
    except Exception:
        logger.exception("LLM summary requested but the local engine is unavailable.")
        return ""

    lines = [
        f'- {item.get("type") or "item"} '
        f'(semantic {_score(item, "semantic_score"):.2f}, '
        f'recency {_score(item, "recency_score"):.2f}): {_snippet(item)}'
        for item in items
    ]
    prompt = (
        "Summarize these memory items in three to six sentences. "
        "Use only the information given; do not add anything.\n\n" + "\n".join(lines)
    )
    try:
        generate = getattr(local_inference_engine, "generate", None)
        if generate is None:
            return ""
        return str(generate(prompt, temperature=0.0, seed=0)).strip()
    except Exception:
        logger.exception("LLM summary failed; falling back to the extractive summary.")
        return ""


# ======================================================
# Context bundle
# ======================================================
def build_memory_context(query: str, limit: int = DEFAULT_LIMIT) -> dict:
    """Gather everything the model should know for this query."""
    items = aria_memory.search_ranked(query, limit=limit)
    return {
        "query": query,
        "items": items,
        "summary": summarize_items(items),
        "routing": {
            "semantic_used": True,
            "hybrid_used": True,
            "ranking_used": True,
            "backend": semantic_embeddings.backend_id(),
            "embedding_dim": semantic_embeddings.dimension(),
        },
        "self_knowledge": load_self_knowledge(),
        "preferences": load_user_preferences(),
    }


def load_file_context(query: str, limit: int = 6) -> dict:
    """Ranked file chunks for a query, in the same shape as memory context.

    Deliberately NOT called by build_memory_context(): folding file chunks
    into every turn would double retrieval cost and let a document outrank
    something the user wrote deliberately, before anything has decided that
    files are relevant. semantic_routing already classifies files.query --
    this is what that route will call when it is wired up.
    """
    try:
        from backend.files.file_search import search_files_ranked
    except ImportError:  # running from inside the backend directory
        from files.file_search import search_files_ranked

    items = search_files_ranked(query, limit=limit)
    return {
        "query": query,
        "items": items,
        "summary": summarize_items(items),
        "routing": {
            "semantic_used": True,
            "ranking_used": True,
            "file_search_used": True,
            "backend": semantic_embeddings.backend_id(),
            "embedding_dim": semantic_embeddings.dimension(),
        },
    }


# ======================================================
# Prompt assembly
# ======================================================
def _item_lines(memory_context: dict) -> list[str]:
    return [
        f'  - {_snippet(item)} '
        f'(semantic={_score(item, "semantic_score"):.3f}, '
        f'keyword={_score(item, "keyword_score"):.3f}, '
        f'recency={_score(item, "recency_score"):.3f}, '
        f'type={item.get("type") or "item"})'
        for item in memory_context.get("items") or []
    ]


def _mapping_lines(mapping: dict) -> list[str]:
    # Sorted so the same stored state always renders the same prompt, which
    # matters for both reproducibility and prompt caching.
    return [f"  - {key}: {value}" for key, value in sorted((mapping or {}).items())]


def _file_snippet(item: dict) -> str:
    """A file chunk's text, clipped to SNIPPET_CHARS on a word boundary.

    Memory items use _snippet(), which clips at the character limit wherever
    it falls. A file chunk is up to 1200 characters of prose, so that lands
    mid-word far more often and leaves a fragment the model has to guess at.
    Breaking at the last space instead costs a few characters and reads as
    language.

    The returned string, ellipsis included, never exceeds SNIPPET_CHARS. A
    single word longer than the whole budget has no boundary to break on and
    is cut hard -- there is no better answer for a 200-character token.
    """
    flat = " ".join(str(item.get("text") or item.get("chunk") or "").split())
    if len(flat) <= SNIPPET_CHARS:
        return flat

    budget = SNIPPET_CHARS - 1  # one character reserved for the ellipsis
    cut = flat.rfind(" ", 0, budget + 1)
    if cut <= 0:
        cut = budget
    return flat[:cut].rstrip() + "\u2026"


def _file_item_lines(file_context: dict) -> list[str]:
    """One line per file chunk.

    Deliberately not the same line as a memory item. File search is semantic
    only, so there is no keyword score to report -- printing keyword=0.000
    would read as "no keyword match" rather than "keyword was never
    measured". In exchange a file chunk carries provenance a note does not:
    which file it came from, which chunk of it, and where in the text, so a
    model quoting it can say where the quote is from.

    Fields whose values are missing are omitted rather than printed as None:
    an absent offset is not an offset of nothing.
    """
    lines = []
    for item in file_context.get("items") or []:
        fields = []
        if item.get("file_id") is not None:
            fields.append(f'file={item["file_id"]}')
        if item.get("chunk_index") is not None:
            fields.append(f'chunk={item["chunk_index"]}')
        if item.get("section"):
            fields.append(f'section={item["section"]}')
        fields.append(f'semantic={_score(item, "semantic_score"):.3f}')
        fields.append(f'recency={_score(item, "recency_score"):.3f}')
        if item.get("start_offset") is not None and item.get("end_offset") is not None:
            fields.append(f'offsets={item["start_offset"]}-{item["end_offset"]}')
        fields.append("type=file chunk")
        lines.append(f'  - {_file_snippet(item)} ({", ".join(fields)})')
    return lines


def build_file_section(file_context: dict) -> str:
    """The ### File Context block, or "" when there is nothing to show."""
    context = file_context or {}
    items = context.get("items") or []
    if not items and not context.get("summary"):
        return ""

    block = ["### File Context", f'Query: {context.get("query", "")}']
    if context.get("summary"):
        block.append(f'Summary: {context["summary"]}')
    if items:
        block.append("Items:")
        block.extend(_file_item_lines(context))
    routing = context.get("routing") or {}
    if routing:
        block.append("Routing:")
        block.extend(f"  {key}={value}" for key, value in sorted(routing.items()))
    return "\n".join(block)


def append_file_context(prompt: str, file_context: dict) -> str:
    """Append the file section to a prompt, if there is one."""
    section = build_file_section(file_context)
    if not section:
        return prompt
    return f"{prompt.rstrip()}\n\n{section}" if prompt else section


def build_file_user_prompt(user_message: str, file_context: dict) -> str:
    """Wrap the user's message with the file chunks that bear on it."""
    context = file_context or {}
    sections = ["### User Message", user_message or ""]

    if context.get("summary"):
        sections.append("\n### Relevant File Summary\n" + context["summary"])

    items = context.get("items") or []
    if items:
        sections.append("\n### Relevant File Chunks\n" + "\n".join(_file_item_lines(context)))

    return "\n".join(sections).rstrip()


def build_system_prompt(base_prompt: str, memory_context: dict) -> str:
    """Append memory, self-knowledge and preferences to the system prompt.

    Sections with nothing in them are omitted rather than rendered empty: a
    heading followed by nothing invites the model to invent what should have
    been under it.
    """
    sections = [base_prompt.rstrip()] if base_prompt else []
    context = memory_context or {}

    items = context.get("items") or []
    routing = context.get("routing") or {}
    if items or context.get("summary"):
        block = ["### Memory Context", f'Query: {context.get("query", "")}']
        if context.get("summary"):
            block.append(f'Summary: {context["summary"]}')
        if items:
            block.append("Items:")
            block.extend(_item_lines(context))
        if routing:
            block.append("Routing:")
            block.extend(f"  {key}={value}" for key, value in sorted(routing.items()))
        sections.append("\n".join(block))

    self_knowledge = context.get("self_knowledge") or {}
    if self_knowledge:
        sections.append("\n".join(["### Self Knowledge", *_mapping_lines(self_knowledge)]))

    preferences = context.get("preferences") or {}
    if preferences:
        sections.append("\n".join(["### User Preferences", *_mapping_lines(preferences)]))

    return "\n\n".join(sections)


def build_user_prompt(user_message: str, memory_context: dict) -> str:
    """Wrap the user's message with the memory that bears on it."""
    context = memory_context or {}
    sections = ["### User Message", user_message or ""]

    if context.get("summary"):
        sections.append("\n### Relevant Memory Summary\n" + context["summary"])

    items = context.get("items") or []
    if items:
        sections.append("\n### Relevant Items\n" + "\n".join(_item_lines(context)))

    return "\n".join(sections).rstrip()
