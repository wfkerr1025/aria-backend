"""ARIA Lite Phase 3 - save_note tool wrapper.

Adapts aria_memory_notes.save_note() to the dispatcher's calling
convention. No logic of its own beyond shaping the result dict.
"""

try:
    from backend.aria_memory_notes import save_note
except ImportError:  # running from inside the backend directory
    from aria_memory_notes import save_note


def tool_save_note(text: str, tags: list[str] | None = None, source: str = "chat") -> dict:
    note_id = save_note(text, tags, source)
    return {
        "note_id": note_id,
        "status": "saved",
    }


TOOL_SPEC = {
    "name": "save_note",
    "description": "Store a note in ARIA Lite's Phase 3 memory system.",
    "args": {
        "text": {"type": "string", "required": True},
        "tags": {"type": "array", "items": {"type": "string"}, "required": False},
        "source": {"type": "string", "required": False},
    },
}
