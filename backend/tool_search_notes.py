"""ARIA Lite Phase 3 - search_notes tool wrapper.

Adapts aria_memory_notes.search_notes_keyword() to the dispatcher's
calling convention. Keyword search only -- no embeddings involved.
"""

try:
    from backend.aria_memory_notes import search_notes_keyword
except ImportError:  # running from inside the backend directory
    from aria_memory_notes import search_notes_keyword


def tool_search_notes(query: str, limit: int = 20) -> dict:
    results = search_notes_keyword(query, limit)
    return {
        "results": results,
        "count": len(results),
    }


TOOL_SPEC = {
    "name": "search_notes",
    "description": "Keyword search over ARIA Lite's Phase 3 notes.",
    "args": {
        "query": {"type": "string", "required": True},
        "limit": {"type": "integer", "required": False},
    },
}
