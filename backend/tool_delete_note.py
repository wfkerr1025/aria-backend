"""ARIA Lite Phase 3 - delete_note tool wrapper.

Adapts aria_memory_notes.delete_note() to the dispatcher's calling
convention. Deleting a note takes its embeddings and indexed chunks with
it, so the reported counts say what actually went.
"""

try:
    from backend.aria_memory_notes import delete_note
except ImportError:  # running from inside the backend directory
    from aria_memory_notes import delete_note


def tool_delete_note(note_id: int) -> dict:
    result = delete_note(note_id)
    return {
        "note_id": note_id,
        "status": "deleted" if result["deleted"] else "not_found",
        "embeddings_deleted": result["embeddings_deleted"],
        "chunks_deleted": result["chunks_deleted"],
    }


TOOL_SPEC = {
    "name": "delete_note",
    "description": "Delete a note from ARIA Lite's memory, with its embeddings and indexed chunks.",
    "args": {
        "note_id": {"type": "integer", "required": True},
    },
}
