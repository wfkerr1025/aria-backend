"""ARIA Lite Phase 3 - note embeddings.

Embeds a note's text and stores the vector through aria_memory_embeddings.
This module owns the note-shaped operations only; the encoder itself lives
in backend/llm/semantic_embeddings.py so that Phase 4 files and Phase 5
file_chunks can share exactly one definition of "what a vector is".

Vectors are now SEMANTIC. Until this upgrade the encoder hashed tokens into
buckets, so "car" and "automobile" were orthogonal and similarity only ever
meant shared words. It now runs a real embedding model, so similarity tracks
meaning. Two consequences worth knowing:

  - The vector width follows the active backend (384 for the local BGE
    model, 1536 for OpenAI, 256 for the lexical fallback), so EMBEDDING_DIM
    is a function, not a constant.
  - Vectors written by different backends are not comparable, so each one is
    stored with the backend id that produced it and reads filter on it. A
    note embedded before a backend change keeps its old vector until it is
    re-embedded; it is never silently ranked against new ones.
"""

try:
    from backend.aria_memory_embeddings import get_embeddings_for_note, store_embedding
    from backend.aria_memory_notes import get_note
    from backend.llm import semantic_embeddings
except ImportError:  # running from inside the backend directory
    from aria_memory_embeddings import get_embeddings_for_note, store_embedding
    from aria_memory_notes import get_note
    from llm import semantic_embeddings

# Re-exported so callers have one import for the whole embedding story.
backend_id = semantic_embeddings.backend_id
decode_vector = semantic_embeddings.decode_vector
embed_text = semantic_embeddings.embed_to_blob
embed_vector = semantic_embeddings.embed_text_semantic
encode_vector = semantic_embeddings.encode_vector
is_semantic = semantic_embeddings.is_semantic
tokenize = semantic_embeddings.LexicalHashBackend().tokenize


def embedding_dim() -> int:
    """Vector width of the active backend."""
    return semantic_embeddings.dimension()


def vector_bytes() -> int:
    """Stored size of one vector, in bytes."""
    return embedding_dim() * 4


def embed_note(note_id: int) -> int | None:
    """Embed a note's text and store the vector.

    Returns the new embedding id, or None when the note does not exist.
    """
    note = get_note(note_id)
    if note is None:
        return None
    return store_embedding(
        note_id,
        semantic_embeddings.embed_to_blob(note["text"]),
        model=semantic_embeddings.backend_id(),
        dim=semantic_embeddings.dimension(),
    )


def get_note_vectors(note_id: int) -> list[list[float]]:
    """Return the note's vectors from the active backend, decoded.

    Vectors written by a different backend are skipped rather than decoded
    into the wrong shape -- they belong to another space and comparing them
    would be meaningless.
    """
    model = semantic_embeddings.backend_id()
    dim = semantic_embeddings.dimension()
    return [
        semantic_embeddings.decode_vector(blob, dim)
        for blob in get_embeddings_for_note(note_id, model=model)
    ]
