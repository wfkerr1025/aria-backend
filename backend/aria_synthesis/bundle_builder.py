"""ARIA Lite Phase 6 - building an evidence bundle from retrieval output.

Pure post-processing. This reads what retrieval returned and reshapes it;
it never searches, never ranks, never embeds and never calls a model. That
is worth protecting rather than merely stating: a builder that could
re-retrieve would make the bundle a second, quieter retrieval path with its
own behaviour to keep in step, and the whole point of the seam is that
there is exactly one.

Three item shapes arrive, told apart by their "type":

    note        a whole note from the keyword half of search_hybrid
    chunk       a passage of a note from the semantic half
    file_chunk  a passage of an ingested document

The first two become EvidenceNote, the third EvidenceFile. Anything else is
dropped rather than guessed at -- a type this does not recognise is a new
retrieval source that nobody has decided how to cite yet, and inventing a
provenance string for it is worse than leaving it out.
"""

from __future__ import annotations

from datetime import datetime, timezone

try:
    from backend.aria_synthesis.evidence_bundle import (
        TITLE_CHARS,
        EvidenceBundle,
        EvidenceFile,
        EvidenceNote,
        shorten,
        snippet,
    )
    from backend.context.current_topic import current_topic_of
    from backend.context.evidence_selection import select_turns
    from backend.context.goal_tracking import current_goal_of
    from backend.context.topic_classifier import classify_topic
    from backend.llm import semantic_embeddings
    from backend.llm.query_deframing import deframe_query
    from backend.llm.query_expansion import ExpansionDomain, detect_domain
except ImportError:  # running from inside the backend directory
    from aria_synthesis.evidence_bundle import (
        TITLE_CHARS,
        EvidenceBundle,
        EvidenceFile,
        EvidenceNote,
        shorten,
        snippet,
    )
    from context.current_topic import current_topic_of
    from context.evidence_selection import select_turns
    from context.goal_tracking import current_goal_of
    from context.topic_classifier import classify_topic
    from llm import semantic_embeddings
    from llm.query_deframing import deframe_query
    from llm.query_expansion import ExpansionDomain, detect_domain

__all__ = ["DEFAULT_FILE_LIMIT", "DEFAULT_NOTE_LIMIT", "build_evidence_bundle"]

# How many of each kind reach the prompt. Five and five is a judgement about
# attention, not about storage: past roughly ten items a model starts
# averaging its sources instead of weighing them, and the eleventh chunk is
# nearly always a worse copy of one of the first five.
DEFAULT_NOTE_LIMIT = 5
DEFAULT_FILE_LIMIT = 5

NOTE_TYPES = ("note", "chunk")
FILE_TYPE = "file_chunk"


def _score_of(item: dict) -> float:
    """The one number this item is ranked and ordered by.

    combined_score when the caller ranked its results, which is the intended
    input and the only score that has weighed recency and type. Otherwise
    the best retrieval signal available, so an unranked search_hybrid list
    still orders sensibly instead of arbitrarily.

    Deliberately not computed here. Ranking needs the query embedded, and an
    embedding call is exactly the kind of thing a "pure post-processing"
    step must not start doing quietly -- a caller who wants combined scores
    passes ranked items.
    """
    for key in ("combined_score", "semantic_score", "keyword_score", "score"):
        value = item.get(key)
        if value is not None:
            return float(value)
    return 0.0


def _text_of(item: dict) -> str:
    """The item's own words, wherever this shape happens to keep them."""
    for key in ("text", "chunk", "value"):
        value = item.get(key)
        if value:
            return str(value)
    return ""


def _section_of(item: dict) -> str:
    """The heading a file chunk sits under, cleaned for display.

    Markdown hashes come off because the heading is being used as a label
    here, not rendered: "#pipeline" reads as a fragment identifier, which is
    exactly what it is being used as.
    """
    return " ".join(str(item.get("section") or "").replace("#", " ").split())


def _file_paths(items: list[dict]) -> dict[int, str]:
    """file_id -> path, read once for the whole bundle.

    File chunks carry file_id, not the path a person would recognise. The
    lookup is a plain database read, not a search: no scoring, no embedding,
    nothing that could change which chunks were chosen. An item that already
    carries its own path skips it, and a file deleted since it was indexed
    falls back to its id rather than dropping the evidence.
    """
    wanted = {
        item.get("file_id")
        for item in items
        if item.get("file_id") is not None and not item.get("path")
    }
    if not wanted:
        return {}

    try:
        from backend.files.file_ingestion import get_file
    except ImportError:
        from files.file_ingestion import get_file

    paths = {}
    for file_id in sorted(wanted):
        try:
            row = get_file(file_id)
        except Exception:
            row = None
        if row and row.get("path"):
            paths[file_id] = str(row["path"])
    return paths


def _basename(path: str) -> str:
    """The part of a path worth putting in a citation.

    Provenance is meant to be read, and "file:build.md#pipeline" is read at
    a glance where an absolute path is not. The bundle keeps the full path
    on the EvidenceFile, so nothing is lost -- but two documents with the
    same basename in different directories do cite identically, which is the
    price of a citation a person will actually read.
    """
    cleaned = str(path or "").replace("\\", "/").rstrip("/")
    return cleaned.rsplit("/", 1)[-1] or cleaned


def build_evidence_bundle(
    query: str,
    retrieval_result,
    note_limit: int = DEFAULT_NOTE_LIMIT,
    file_limit: int = DEFAULT_FILE_LIMIT,
    routing_intent: str | None = None,
    conversation=None,
    now: str | None = None,
) -> EvidenceBundle:
    """Reshape retrieval output into the bundle the prompt is built from.

    retrieval_result is whatever search produced: search_hybrid's items,
    search_ranked's, search_files_ranked's, or several concatenated. Notes
    and files are separated by type, sorted best-first, and capped.

    The domain is detected exactly as Phase 5 detects it, from the raw query
    and the same optional routing intent, so the vocabulary the prompt says
    the evidence is in is the vocabulary retrieval actually searched with.

    now fixes the timestamp recorded in meta. It is injectable because a
    bundle is otherwise a pure function of its inputs, and a clock reading
    is the one thing that would stop two runs of the same query producing
    equal bundles. It never reaches the prompt.

    Sorting is stable and the key is the score alone, so items that score
    equally keep the order retrieval gave them rather than an arbitrary one.

    conversation is optional and is used for two things: recording which
    topic the conversation is on, and which goal the user is pursuing, so
    synthesis can tell evidence about the work in hand from evidence that
    merely scored well. It changes nothing about which items are kept or how
    they are ordered -- both are recorded, never filtered on, because a
    bundle that silently dropped evidence from outside the current goal
    would hide exactly the material a reader needs when an answer looks
    wrong.
    """
    items = [item for item in (retrieval_result or []) if isinstance(item, dict)]
    domain = detect_domain(query, routing_intent)
    current = current_topic_of(conversation) if conversation is not None else None
    goal = current_goal_of(conversation) if conversation is not None else None
    turns = (
        select_turns(conversation, current, goal, query=deframe_query(query))
        if conversation is not None
        else []
    )

    note_items = [item for item in items if item.get("type") in NOTE_TYPES]
    file_items = [item for item in items if item.get("type") == FILE_TYPE]

    note_items.sort(key=_score_of, reverse=True)
    file_items.sort(key=_score_of, reverse=True)

    kept_notes = note_items[:note_limit]
    kept_files = file_items[:file_limit]
    paths = _file_paths(kept_files)

    notes = []
    for item in kept_notes:
        text = _text_of(item)
        note_id = item.get("id") if item.get("id") is not None else item.get("note_id")
        notes.append(
            EvidenceNote(
                id=note_id,
                title=shorten(text, TITLE_CHARS),
                snippet=snippet(text),
                score=_score_of(item),
                domain=domain,
                provenance=f"note:{note_id}",
                topic=classify_topic(text),
            )
        )

    files = []
    for item in kept_files:
        path = str(item.get("path") or paths.get(item.get("file_id")) or "")
        name = _basename(path) or f"file {item.get('file_id')}"
        section = _section_of(item)
        files.append(
            EvidenceFile(
                path=path,
                section_heading=section,
                snippet=snippet(_text_of(item)),
                score=_score_of(item),
                domain=domain,
                provenance=f"file:{name}#{section}" if section else f"file:{name}",
                topic=classify_topic(_text_of(item)),
            )
        )

    return EvidenceBundle(
        turns=turns,
        query=str(query or ""),
        cleaned_query=deframe_query(query),
        domain=domain,
        notes=notes,
        files=files,
        meta={
            "embedding_backend": _backend_id(),
            "note_floor": _note_floor(),
            "file_floor": _file_floor(),
            "candidates": {"notes": len(note_items), "files": len(file_items)},
            "dropped": {
                "notes": max(0, len(note_items) - len(kept_notes)),
                "files": max(0, len(file_items) - len(kept_files)),
            },
            "limits": {"notes": note_limit, "files": file_limit},
            "current_topic": current,
            # The goal is recorded, never filtered on -- same reasoning as
            # the topic. Evidence that scored well while the user was
            # working on something else is worth showing and worth
            # labelling; dropping it would hide the material a reader needs
            # when an answer looks wrong.
            "current_goal": goal.goal if goal else None,
            "goal_topic": goal.topic if goal else None,
            "goal_explicit": goal.explicit if goal else None,
            # Ids rather than the turns themselves: meta is the record of
            # how this bundle was built, and the turns are on the bundle.
            "selected_turns": [turn.message_id for turn in turns],
            "built_at": now if now is not None else datetime.now(timezone.utc).isoformat(),
        },
    )


def _backend_id():
    """Which encoder produced the vectors behind this evidence.

    Recorded for meta only, and never allowed to fail the build: a bundle is
    a reshaping of text the caller already has, and a machine with no
    embedding backend can still make one.
    """
    try:
        return semantic_embeddings.backend_id()
    except Exception:
        return None


def _note_floor() -> float:
    try:
        from backend.aria_memory_semantic_index import DEFAULT_MIN_SCORE
    except ImportError:
        from aria_memory_semantic_index import DEFAULT_MIN_SCORE
    return DEFAULT_MIN_SCORE


def _file_floor() -> float:
    try:
        from backend.files.file_ingestion import DEFAULT_MIN_SCORE
    except ImportError:
        from files.file_ingestion import DEFAULT_MIN_SCORE
    return DEFAULT_MIN_SCORE
