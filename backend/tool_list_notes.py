"""ARIA Lite Phase 3 - list_notes tool wrapper.

Adapts aria_memory_notes.list_notes() to the dispatcher's calling
convention. Browsing, not searching: search_notes answers "which notes
mention X", this answers "what is in memory".
"""

try:
    from backend.aria_memory_notes import list_notes
except ImportError:  # running from inside the backend directory
    from aria_memory_notes import list_notes


def tool_list_notes(limit: int = 20, offset: int = 0) -> dict:
    results = list_notes(limit, offset)
    return {
        "results": results,
        "count": len(results),
    }


TOOL_SPEC = {
    "name": "list_notes",
    "description": "List ARIA Lite's stored notes, newest first.",
    "args": {
        "limit": {"type": "integer", "required": False},
        "offset": {"type": "integer", "required": False},
    },
}
