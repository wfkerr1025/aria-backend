"""ARIA Lite Phase 4 - ranked file search.

Thin layer over file_ingestion.search_files_semantic: retrieval finds the
candidate chunks, memory_ranking orders them. Kept separate from ingestion
for the same reason search_ranked is separate from search_hybrid -- ranking
can be reweighted or replaced without touching how files are read.

File chunks carry type="file_chunk", which memory_ranking weights slightly
below a whole note: a fragment of a document is usually less directly useful
than a note the user wrote deliberately.
"""

from __future__ import annotations

try:
    from backend.aria_memory import memory_ranking
    from backend.files.file_ingestion import DEFAULT_MIN_SCORE, search_files_semantic
except ImportError:  # running from inside the backend directory
    from aria_memory import memory_ranking
    from files.file_ingestion import DEFAULT_MIN_SCORE, search_files_semantic

__all__ = ["search_files_ranked"]


def search_files_ranked(
    query: str,
    limit: int = 10,
    min_score: float = DEFAULT_MIN_SCORE,
    routing_intent: str | None = None,
    conversation=None,
) -> list[dict]:
    """Retrieve file chunks and return them best-first.

    Ranking sees the semantic score from retrieval, the chunk's created_at
    for recency, and type="file_chunk" for the type weight. There is no
    keyword signal here yet -- file search is semantic only -- so those items
    rank on three of the four signals rather than a fabricated fourth.
    """
    items = search_files_semantic(
        query,
        limit=limit,
        min_score=min_score,
        routing_intent=routing_intent,
        conversation=conversation,
    )
    return memory_ranking.rank_items(items, query, limit=limit)
