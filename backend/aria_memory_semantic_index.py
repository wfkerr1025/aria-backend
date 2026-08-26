"""ARIA Lite Phase 3 - semantic index.

Chunk a note's text, embed each chunk, and store the pairs in the
semantic_index table so a query can be matched against parts of a long
note rather than the note as a whole.

Search is an exact cosine scan over every stored chunk. That is O(n) in
the number of chunks, which is the right trade at Phase 3 scale (a
personal note store, not a corpus) and keeps the module dependency-free.
If the index ever grows past tens of thousands of chunks, this is the
function to replace, not the storage layout.

Search is now genuinely semantic. Chunks and queries both go through
backend/llm/semantic_embeddings.py, so a query matches a chunk that means
the same thing whether or not they share any words -- "how do I reset my
password" finds a chunk about forgotten login credentials. Before this
upgrade the encoder was lexical and only shared words could score.

Because scores are now similarities in a dense space rather than sparse
word overlap, almost nothing scores exactly zero: an unrelated chunk lands
low rather than absent. min_score is what separates "related" from "merely
present", and DEFAULT_MIN_SCORE is a calibrated floor rather than 0.0.

Only chunks embedded by the active backend are searched. Vectors from a
different encoder are a different space, so they are filtered out rather
than mis-ranked; re-index a note to bring it into the current space.
"""

from datetime import datetime, timezone

try:
    from backend.aria_memory_db import get_connection
    from backend.aria_memory_notes import get_note
    from backend.llm import semantic_embeddings
    from backend.context.current_topic import current_topic_of
    from backend.context.evidence_selection import select_turns
    from backend.context.goal_tracking import current_goal_of
    from backend.llm.query_expansion import embed_expanded_query
except ImportError:  # running from inside the backend directory
    from aria_memory_db import get_connection
    from aria_memory_notes import get_note
    from llm import semantic_embeddings
    from context.current_topic import current_topic_of
    from context.evidence_selection import select_turns
    from context.goal_tracking import current_goal_of
    from llm.query_expansion import embed_expanded_query

decode_vector = semantic_embeddings.decode_vector
embed_text = semantic_embeddings.embed_to_blob

# Calibrated against the local BGE model: related pairs sit around 0.6-0.9
# and unrelated ones around 0.3-0.55, so this floor keeps the obvious
# non-matches out without discarding a loose paraphrase. It is a default,
# not a rule -- callers that want everything ranked pass min_score=0.0.
DEFAULT_MIN_SCORE = 0.6

# Chunks are measured in words rather than characters so a chunk always
# holds whole tokens -- the encoder works on tokens, and splitting one in
# half would put a word in the index that appears in neither the note nor
# any query.
DEFAULT_CHUNK_WORDS = 40
DEFAULT_CHUNK_OVERLAP = 10


def chunk_spans(
    text: str,
    chunk_words: int = DEFAULT_CHUNK_WORDS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[tuple[str, int, int]]:
    """Split text into overlapping word windows, each with its span.

    Yields (chunk, start_word, end_word) with end_word exclusive, so a
    stored chunk can be located back in the note it came from -- which is
    what the chunk_index/start_word/end_word columns record. The overlap
    keeps a phrase that straddles a boundary intact in at least one
    chunk. An empty or whitespace-only text yields no chunks.
    """
    if chunk_words <= 0:
        raise ValueError("chunk_words must be positive")
    if not 0 <= overlap < chunk_words:
        raise ValueError("overlap must be >= 0 and smaller than chunk_words")

    words = text.split()
    if not words:
        return []

    step = chunk_words - overlap
    spans = []
    for start in range(0, len(words), step):
        window = words[start : start + chunk_words]
        if window:
            spans.append((" ".join(window), start, start + len(window)))
        if start + chunk_words >= len(words):
            break
    return spans


def chunk_text(
    text: str,
    chunk_words: int = DEFAULT_CHUNK_WORDS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    """Split text into overlapping word windows, text only."""
    return [chunk for chunk, _start, _end in chunk_spans(text, chunk_words, overlap)]


def index_note(
    note_id: int,
    chunk_words: int = DEFAULT_CHUNK_WORDS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> int:
    """Chunk, embed and store a note. Returns the number of chunks stored,
    or 0 when the note does not exist or holds no words.

    Re-indexing a note replaces its existing chunks rather than adding a
    second copy, so this is safe to call after a note is edited.
    """
    note = get_note(note_id)
    if note is None:
        return 0

    spans = chunk_spans(note["text"], chunk_words, overlap)
    if not spans:
        clear_note_index(note_id)
        return 0

    indexed_at = datetime.now(timezone.utc).isoformat()
    model = semantic_embeddings.backend_id()
    dim = semantic_embeddings.dimension()
    # One batched call rather than one per chunk: a hosted backend charges
    # per request, and a local one still pays setup cost each time.
    vectors = semantic_embeddings.embed_texts_semantic([chunk for chunk, _s, _e in spans])
    rows = [
        (
            note_id,
            chunk,
            semantic_embeddings.encode_vector(vector),
            position,
            start,
            end,
            indexed_at,
            model,
            dim,
        )
        for position, ((chunk, start, end), vector) in enumerate(zip(spans, vectors))
    ]
    conn = get_connection()
    try:
        conn.execute("DELETE FROM semantic_index WHERE note_id = ?", (note_id,))
        conn.executemany(
            """
            INSERT INTO semantic_index
                (note_id, chunk, vector, chunk_index, start_word, end_word,
                 created_at, model, dim)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def clear_note_index(note_id: int) -> int:
    """Remove every indexed chunk for a note. Returns the number deleted."""
    conn = get_connection()
    try:
        cursor = conn.execute("DELETE FROM semantic_index WHERE note_id = ?", (note_id,))
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def get_note_chunks(note_id: int) -> list[dict]:
    """Return a note's indexed chunks, in chunk_index order."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, note_id, chunk, chunk_index, start_word, end_word,
                   created_at, model, dim
            FROM semantic_index
            WHERE note_id = ?
            ORDER BY chunk_index, id
            """,
            (note_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# Re-exported from the embedding module so there is one implementation of
# similarity, whatever produced the vectors.
cosine_similarity = semantic_embeddings.cosine_similarity


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


def search_semantic(
    query: str,
    limit: int = 10,
    min_score: float = DEFAULT_MIN_SCORE,
    routing_intent: str | None = None,
    conversation=None,
) -> list[dict]:
    """Rank indexed chunks against a query by meaning, best match first.

    Each result carries the chunk's own metadata alongside its score:
    {"note_id", "chunk", "chunk_index", "start_word", "end_word",
    "created_at", "score"}.
    Chunks scoring at or below min_score are dropped.

    Only chunks embedded by the active backend take part; anything left
    over from a previous encoder is skipped until the note is re-indexed.

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
    # Retrieval sees the topic, not the way it was asked, and sees it in
    # more than one wording: embed_expanded_query de-frames the question and
    # folds in weighted synonyms for the words it recognises, so a note
    # written as "password reset" is reachable from "login". Which synonyms
    # those are depends on the domain, which routing_intent settles when the
    # caller knows it. The caller's original string is what gets displayed;
    # only the vector changes.
    query_vector = embed_expanded_query(query, routing_intent)

    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT note_id, chunk, vector, chunk_index, start_word, end_word,
                   created_at
            FROM semantic_index
            WHERE model = ?
            ORDER BY id
            """,
            (model,),
        ).fetchall()
    finally:
        conn.close()

    scored = []
    for row in rows:
        score = cosine_similarity(query_vector, decode_vector(bytes(row["vector"]), dim))
        if score > min_score:
            scored.append(
                {
                    "note_id": row["note_id"],
                    "chunk": row["chunk"],
                    "chunk_index": row["chunk_index"],
                    "start_word": row["start_word"],
                    "end_word": row["end_word"],
                    # Carried so a ranking layer can score a chunk on age;
                    # without it every chunk looks equally fresh.
                    "created_at": row["created_at"],
                    "score": score,
                }
            )

    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:limit]
