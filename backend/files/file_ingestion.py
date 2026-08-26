"""ARIA Lite Phase 4 - file ingestion.

Reads a text file off disk, records it, splits it into chunks, embeds the
chunks and makes them searchable by meaning. The pipeline mirrors the one
notes already use, and shares its encoder -- a file chunk and a note chunk
land in the same vector space, so the two can be compared if a later phase
wants to.

Where the vectors live
----------------------
File chunk vectors are stored in file_chunks, NOT in semantic_index.
semantic_index.note_id is NOT NULL with a cascade to notes; putting file
rows there would mean relaxing that constraint, which is the thing keeping a
deleted note from leaving orphaned chunks behind. file_chunks already had
vector/chunk columns for exactly this purpose, so files get their own index
and the notes guarantee stays intact.

Section headings
----------------
While chunking, the enclosing markdown heading is tracked and stored on each
chunk, and it is prepended to the text before embedding. This is the standard
contextual-chunk-header trick: a chunk whose body reads only "use the
self-service portal" carries no hint of what it is about, but embedded as
"Passwords\n\nuse the self-service portal" it retrieves for a question about
passwords. The heading is stored separately as well as embedded, so a prompt
can cite the section without re-deriving it.

The section is the heading in force where the chunk STARTS. A chunk that
straddles a boundary reports the section its opening text belongs to, which
is the one its first words actually came from.

Two columns are written twice on purpose. file_chunks.chunk predates this
module and is NOT NULL, so every insert must supply it; the ingestion code
reads and writes `text`, and mirrors the same value into `chunk` so the two
never disagree. Likewise `vector` is NOT NULL, so chunk_file() writes an
empty blob that embed_file_chunks() later replaces -- a chunk with no vector
is simply one that has not been embedded yet.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from logger import get_logger

try:
    from backend.aria_memory_db import get_connection
    from backend.llm import semantic_embeddings
    from backend.context.current_topic import current_topic_of
    from backend.context.evidence_selection import select_turns
    from backend.context.goal_tracking import current_goal_of
    from backend.llm.query_expansion import embed_expanded_query
except ImportError:  # running from inside the backend directory
    from aria_memory_db import get_connection
    from llm import semantic_embeddings
    from context.current_topic import current_topic_of
    from context.evidence_selection import select_turns
    from context.goal_tracking import current_goal_of
    from llm.query_expansion import embed_expanded_query

logger = get_logger(__name__)

DEFAULT_MAX_CHARS = 1200

# Lower than the note index's 0.6 floor, and measured rather than guessed.
# A file chunk is far longer than a note chunk, and a real question carries
# framing ("search my documents for ...") that dilutes the query embedding,
# so the same correct answer scores lower here than it would for notes:
# against this corpus, related questions land 0.60-0.81 and unrelated ones
# 0.33-0.53, which puts the boundary just under 0.56. At 0.6 the correct
# chunk for "search my documents for login credentials" (0.597) was being
# dropped.
DEFAULT_MIN_SCORE = 0.55

# Text formats this phase can read. Anything else needs an extractor (PDF,
# docx) which is Phase 5's problem -- ingest_file refuses rather than storing
# bytes it cannot chunk into meaningful text.
MIME_BY_SUFFIX = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".log": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".py": "text/x-python",
}
DEFAULT_MIME = "text/plain"

# Placeholder for the NOT NULL vector column before embedding runs.
_UNEMBEDDED = b""


class FileIngestionError(RuntimeError):
    """Raised when a file cannot be read or ingested."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def infer_mime(path: str) -> str:
    """Guess a mime type from the file extension."""
    return MIME_BY_SUFFIX.get(Path(path).suffix.lower(), DEFAULT_MIME)


def read_file_text(path: str) -> str:
    """Read a file as UTF-8, replacing anything undecodable.

    Replacement rather than failure: a log with one stray byte in it is
    still worth indexing, and refusing the whole file would be a worse
    answer than a single lost character.
    """
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise FileIngestionError(f"Could not read {path}: {exc}") from exc


# ======================================================
# Ingestion
# ======================================================
def ingest_file(path: str, mime: str | None = None) -> int:
    """Record a file and return its id.

    Re-ingesting the same path updates the existing row rather than adding a
    second one -- a file is identified by where it is, and two rows for one
    path would both claim to be current.
    """
    resolved = str(Path(path))
    if not Path(resolved).is_file():
        raise FileIngestionError(f"Not a file: {path}")

    text = read_file_text(resolved)
    now = _utc_now()
    record = {
        "path": resolved,
        "name": os.path.basename(resolved),
        "mime": mime or infer_mime(resolved),
        "size_bytes": Path(resolved).stat().st_size,
        "updated_at": now,
    }

    conn = get_connection()
    try:
        existing = conn.execute("SELECT id FROM files WHERE path = ?", (resolved,)).fetchone()
        if existing is not None:
            conn.execute(
                """
                UPDATE files
                SET name = ?, mime = ?, size_bytes = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    record["name"], record["mime"], record["size_bytes"],
                    now, existing["id"],
                ),
            )
            conn.commit()
            file_id = int(existing["id"])
        else:
            cursor = conn.execute(
                """
                INSERT INTO files (path, name, mime, size_bytes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record["path"], record["name"], record["mime"],
                    record["size_bytes"], now, now,
                ),
            )
            conn.commit()
            file_id = int(cursor.lastrowid)
    finally:
        conn.close()

    logger.info("Ingested %s (%d bytes) as file %d", record["name"], record["size_bytes"], file_id)
    _ = text  # read up front so an unreadable file fails before it is recorded
    return file_id


def get_file(file_id: int) -> dict | None:
    """Return one file record, or None."""
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT id, path, name, mime, size_bytes, created_at, updated_at
            FROM files WHERE id = ?
            """,
            (file_id,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def list_files() -> list[dict]:
    """Every ingested file, newest first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, path, name, mime, size_bytes, created_at, updated_at
            FROM files ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ======================================================
# Chunking
# ======================================================
# ATX markdown headings ("## Passwords"). Setext underlining is not matched:
# it needs two-line lookahead for a form that is rare in the plain-text and
# log files this phase reads, and guessing wrong would attach a body line to
# the wrong section.
_HEADING_PATTERN = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(\S.*?)[ \t]*#*[ \t]*$", re.MULTILINE)


def find_headings(text: str) -> list[tuple[int, str]]:
    """Every heading in a document as (character offset, title), in order.

    The title is the heading text without its hashes, so a stored section
    reads as "Passwords" rather than "## Passwords".
    """
    return [(match.start(), match.group(2).strip()) for match in _HEADING_PATTERN.finditer(text)]


def section_at(headings: list[tuple[int, str]], offset: int) -> str | None:
    """The heading in force at a character offset, or None above the first."""
    current = None
    for start, title in headings:
        if start > offset:
            break
        current = title
    return current


def split_text(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[tuple[str, int, int]]:
    """Split text into (chunk, start_offset, end_offset) windows.

    Breaks on line boundaries wherever a line fits, because a chunk that
    ends mid-sentence embeds worse than one that ends at a natural pause. A
    single line longer than max_chars is split hard -- there is no better
    boundary available inside it.

    Offsets are character offsets into the original text, so a chunk can
    always be located back in the file it came from.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if not text.strip():
        return []

    chunks: list[tuple[str, int, int]] = []
    start = 0
    length = len(text)

    while start < length:
        end = min(start + max_chars, length)
        if end < length:
            # Prefer the last newline inside the window; fall back to a hard
            # cut when the window holds no break at all.
            newline = text.rfind("\n", start + 1, end)
            if newline > start:
                end = newline + 1
        piece = text[start:end]
        if piece.strip():
            chunks.append((piece.strip(), start, end))
        start = end

    return chunks


def chunk_file(file_id: int, max_chars: int = DEFAULT_MAX_CHARS) -> list[dict]:
    """Split an ingested file into chunks and store them.

    Re-chunking replaces the file's existing chunks rather than appending,
    so a file that changed on disk does not end up indexed twice.
    """
    record = get_file(file_id)
    if record is None:
        return []

    source = read_file_text(record["path"])
    pieces = split_text(source, max_chars)
    headings = find_headings(source)
    now = _utc_now()
    rows = [
        (
            file_id, index, text, text, _UNEMBEDDED,
            start, end, now, now, section_at(headings, start),
        )
        for index, (text, start, end) in enumerate(pieces)
    ]

    conn = get_connection()
    try:
        conn.execute("DELETE FROM file_chunks WHERE file_id = ?", (file_id,))
        if rows:
            conn.executemany(
                """
                INSERT INTO file_chunks
                    (file_id, chunk_index, text, chunk, vector,
                     start_offset, end_offset, created_at, updated_at, section)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        conn.commit()
    finally:
        conn.close()

    return get_file_chunks(file_id)


def get_file_chunks(file_id: int) -> list[dict]:
    """A file's chunks, in order."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, file_id, chunk_index, text, start_offset, end_offset,
                   created_at, updated_at, model, dim, section
            FROM file_chunks
            WHERE file_id = ?
            ORDER BY chunk_index, id
            """,
            (file_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ======================================================
# Embedding
# ======================================================
def embedding_text(chunk: dict) -> str:
    """What actually gets embedded for a chunk: its section, then its body.

    The heading is skipped when the body already opens with it -- the first
    chunk of a section usually contains its own heading, and repeating it
    would weight those words twice for that chunk but not for the rest.
    """
    text = chunk.get("text") or ""
    section = (chunk.get("section") or "").strip()
    if not section:
        return text
    if section.lower() in text[: len(section) + 16].lower():
        return text
    return f"{section}\n\n{text}"


def embed_file_chunks(file_id: int) -> None:
    """Embed every chunk of a file and store the vectors.

    Embeds in one batched call rather than per chunk, and tags each vector
    with the encoder that produced it so a backend change leaves the old
    vectors visibly foreign instead of silently mis-ranked -- the same rule
    notes follow.

    What is embedded is embedding_text(), not the raw body: the chunk's
    section travels with it into the vector so a fragment stays findable by
    what its section is about.
    """
    chunks = get_file_chunks(file_id)
    if not chunks:
        return

    model = semantic_embeddings.backend_id()
    dim = semantic_embeddings.dimension()
    vectors = semantic_embeddings.embed_texts_semantic([embedding_text(chunk) for chunk in chunks])

    updates = [
        (semantic_embeddings.encode_vector(vector), model, dim, _utc_now(), chunk["id"])
        for chunk, vector in zip(chunks, vectors)
    ]

    conn = get_connection()
    try:
        conn.executemany(
            "UPDATE file_chunks SET vector = ?, model = ?, dim = ?, updated_at = ? WHERE id = ?",
            updates,
        )
        conn.commit()
    finally:
        conn.close()


def ingest_and_index(path: str, mime: str | None = None, max_chars: int = DEFAULT_MAX_CHARS) -> int:
    """Ingest, chunk and embed a file in one call. Returns the file id."""
    file_id = ingest_file(path, mime=mime)
    chunk_file(file_id, max_chars=max_chars)
    embed_file_chunks(file_id)
    return file_id


# ======================================================
# Search
# ======================================================
def _topic_hint(conversation, query: str = "") -> str | None:
    """The current topic of a conversation, as a domain hint.

    Topic labels and expansion domains share their spelling for the four
    that overlap, so the topic can be handed to detect_domain as if it were
    a routing intent. "coding", "travel" and "general" match no domain and
    fall through to the query's own words, which is correct: they say
    something about the conversation and nothing about which synonyms apply.
    """
    if conversation is None:
        return None
    # The goal's topic, not the conversation's. They are the same except
    # when a continuity marker has held a goal on its original topic across
    # a turn that named nothing -- which is precisely the case Phase 7.2
    # exists to get right, so the goal's answer is the one to trust.
    topic = current_topic_of(conversation)
    goal = current_goal_of(conversation)
    if goal is not None and goal.topic:
        return goal.topic

    # No goal yet. Rather than read the whole history a second time, take
    # the topic of the best-scoring selected turn: selection has already
    # weighed topic above everything else, so its top turn is the most
    # on-topic thing the conversation contains.
    selected = select_turns(conversation, topic, goal, query=query)
    if selected and selected[0].topic:
        return selected[0].topic
    return topic or None


def search_files_semantic(
    query: str,
    limit: int = 10,
    min_score: float = DEFAULT_MIN_SCORE,
    routing_intent: str | None = None,
    conversation=None,
) -> list[dict]:
    """Rank file chunks against a query by meaning, best first.

    Only chunks embedded by the active backend take part; anything left from
    a previous encoder is skipped until the file is re-indexed. Chunks
    scoring at or below min_score are dropped.

    Each hit carries {file_id, chunk_id, chunk_index, text, semantic_score,
    created_at, start_offset, end_offset, section, type}.

    conversation is the message history, and is optional. When given, it is
    segmented by topic and the current topic -- the topic of the most recent
    user message -- becomes the domain hint, so a question asked in the
    middle of a Unity conversation searches Unity vocabulary even when the
    question itself names nothing Unity-specific ("why is it still
    failing"). Messages on other topics are never consulted: that scoping is
    the whole point, and it is what stops three turns about the weather from
    colouring a build question.

    An explicit routing_intent still wins. The router saw the message and
    its history; this is an inference from the same history and should not
    override a decision made with more information.
    """
    routing_intent = routing_intent or _topic_hint(conversation, query)
    model = semantic_embeddings.backend_id()
    dim = semantic_embeddings.dimension()
    # "search my documents for X" is a question about X; the framing words
    # only pull the vector toward generic document-management language.
    # Expansion then widens what X can be worded as, at a weight that keeps
    # the vector mostly about what was actually typed.
    query_vector = embed_expanded_query(query, routing_intent)

    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, file_id, chunk_index, text, vector, created_at,
                   start_offset, end_offset, section
            FROM file_chunks
            WHERE model = ? AND dim = ?
            ORDER BY file_id, chunk_index, id
            """,
            (model, dim),
        ).fetchall()
    finally:
        conn.close()

    scored = []
    for row in rows:
        try:
            vector = semantic_embeddings.decode_vector(bytes(row["vector"]), dim)
        except ValueError:
            continue  # not embedded, or embedded at another width
        score = semantic_embeddings.cosine_similarity(query_vector, vector)
        if score > min_score:
            scored.append(
                {
                    "file_id": row["file_id"],
                    "chunk_id": row["id"],
                    "chunk_index": row["chunk_index"],
                    "text": row["text"],
                    "semantic_score": score,
                    "created_at": row["created_at"],
                    # Carried so a prompt can cite where in the file a quote
                    # came from; retrieval itself does not use them.
                    "start_offset": row["start_offset"],
                    "end_offset": row["end_offset"],
                    "section": row["section"],
                    "type": "file_chunk",
                }
            )

    scored.sort(key=lambda entry: entry["semantic_score"], reverse=True)
    return scored[:limit]
