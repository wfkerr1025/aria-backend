"""ARIA Lite - semantic text embeddings.

These embeddings are TRULY SEMANTIC, not lexical. The earlier encoder in
aria_memory_note_embeddings hashed tokens into buckets, so two texts scored
above zero only when they shared literal words: "car" and "automobile" were
orthogonal, and "the cat sat" and "sat the cat" were identical. This module
replaces that with a real embedding model, so similarity tracks meaning --
synonyms and paraphrases land close together, unrelated topics do not, and
word order matters because it changes meaning.

This is the single place model details live. Notes use it today; Phase 4
files and Phase 5 file_chunks should use it too rather than re-deriving any
of this.

Backends, tried in order by _select_backend():

  local-gguf   A dedicated embedding model (BGE / nomic / MiniLM class) run
               through llama_cpp. Local, offline, free, deterministic. This
               is the intended production path for a local-first assistant.
  openai       text-embedding-3-small through the OpenAI SDK, used only when
               a key is configured. Needs the network on every call, costs
               money per call, and OpenAI may revise a model behind a stable
               name -- so vectors are reproducible in practice but not
               guaranteed across time the way a pinned local file is.
  lexical-hash The old bucket-hashing encoder, kept ONLY as a last-resort
               fallback so a machine with no model and no key still runs.
               It is not semantic. is_semantic() returns False when it is
               active, and the semantic test suites skip rather than pretend.

Determinism: the local backend pins n_threads=1. Floating-point reduction
order in a multi-threaded matmul is not fixed, so >1 thread makes a vector
reproducible only to within a small epsilon. Embedding is fast enough here
(tens of milliseconds) that exact reproducibility is the better trade.

Every vector is L2-normalized, which keeps cosine similarity a plain dot
product and keeps the existing cosine tests meaningful.

Vectors are tagged. A vector is only comparable to another from the same
backend and dimension, so backend_id() and dimension() are recorded next to
every stored vector and search filters on them. Switching backends does not
corrupt a store -- it makes the old vectors visibly foreign, and they can be
re-embedded rather than silently mis-ranked.
"""

from __future__ import annotations

import atexit
import math
import os
import struct
import threading
from pathlib import Path

from logger import get_logger

logger = get_logger(__name__)

# ======================================================
# Configuration
# ======================================================
# Explicit override wins over discovery: point this at a .gguf embedding
# model to pin exactly what is used.
ENV_MODEL_PATH = "ARIA_EMBEDDING_MODEL"
# Forces one backend ("local-gguf", "openai", "lexical-hash"). Mostly for
# tests, which need to exercise the fallback without deleting the model.
ENV_BACKEND = "ARIA_EMBEDDING_BACKEND"

# Embedding models live apart from the chat models in ~/.aria-lite/models so
# that model_discovery does not offer an embedding model as a chat model.
DEFAULT_MODEL_DIR = Path.home() / ".aria-lite" / "embeddings"

OPENAI_MODEL = "text-embedding-3-small"
OPENAI_DIM = 1536

LEXICAL_DIM = 256

_VECTOR_ITEM = "<f"  # little-endian float32, matching the original storage


class EmbeddingError(RuntimeError):
    """Raised when the active backend cannot produce a vector."""


# ======================================================
# Backends
# ======================================================
class _Backend:
    """A named, fixed-width text encoder.

    Subclasses set id/dimension and implement _embed_batch. `semantic` says
    whether the vectors actually carry meaning -- the honest answer is False
    for the lexical fallback, and callers branch on it rather than assuming.
    """

    id = "unset"
    dimension = 0
    semantic = False

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        vectors = self._embed_batch(texts)
        return [_normalize(vector, self.dimension) for vector in vectors]

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class LocalGgufBackend(_Backend):
    """A dedicated embedding model run locally through llama_cpp."""

    semantic = True

    def __init__(self, model_path: Path):
        import llama_cpp
        from llama_cpp import Llama

        self.model_path = Path(model_path)
        self.id = f"local-gguf:{self.model_path.stem}"
        # n_threads=1 for bit-exact reproducibility; see module docstring.
        self._llama = Llama(
            model_path=str(self.model_path),
            embedding=True,
            n_ctx=512,
            n_threads=1,
            verbose=False,
            pooling_type=llama_cpp.LLAMA_POOLING_TYPE_MEAN,
        )
        self._lock = threading.Lock()
        atexit.register(self.close)
        self.dimension = len(self._embed_one("dimension probe"))
        logger.info(
            "Semantic embeddings: local model %s (%d dimensions)",
            self.model_path.name,
            self.dimension,
        )

    def close(self) -> None:
        llama, self._llama = getattr(self, "_llama", None), None
        if llama is not None:
            try:
                llama.close()
            except Exception:
                pass

    def _embed_one(self, text: str) -> list[float]:
        # llama_cpp is not safe to call concurrently on one context, and the
        # tool sandbox runs handlers on worker threads.
        with self._lock:
            raw = self._llama.embed(text)
        # Some models pool internally and return one vector; others return
        # one vector per token and leave pooling to the caller.
        if raw and isinstance(raw[0], list):
            raw = [sum(column) / len(column) for column in zip(*raw)]
        return list(raw)

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]


class OpenAIBackend(_Backend):
    """OpenAI's hosted embedding model."""

    id = f"openai:{OPENAI_MODEL}"
    dimension = OPENAI_DIM
    semantic = True

    def __init__(self, api_key: str):
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        # The API rejects an empty string, so it is encoded as a single
        # space -- a caller embedding "" gets a valid, stable vector rather
        # than an exception from three layers down.
        payload = [text if text.strip() else " " for text in texts]
        try:
            response = self._client.embeddings.create(model=OPENAI_MODEL, input=payload)
        except Exception as exc:  # network, auth, rate limit
            raise EmbeddingError(f"OpenAI embedding request failed: {exc}") from exc
        return [item.embedding for item in response.data]


class LexicalHashBackend(_Backend):
    """The pre-semantic encoder, kept so a bare machine still works.

    Hashes tokens into buckets: similarity means "shared words", nothing
    more. Deliberately reports semantic=False.
    """

    id = "lexical-hash-v1"
    dimension = LEXICAL_DIM
    semantic = False

    _TOKEN_PATTERN = r"[a-z0-9]+"

    def tokenize(self, text: str) -> list[str]:
        import re

        return re.findall(self._TOKEN_PATTERN, text.lower())

    def _bucket(self, token: str) -> int:
        import hashlib

        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % self.dimension

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            counts = [0.0] * self.dimension
            for token in self.tokenize(text):
                counts[self._bucket(token)] += 1.0
            vectors.append(counts)
        return vectors


# ======================================================
# Backend selection
# ======================================================
_backend: _Backend | None = None
_backend_lock = threading.Lock()


def find_model_path() -> Path | None:
    """Locate a local embedding model, or None."""
    override = os.environ.get(ENV_MODEL_PATH)
    if override:
        path = Path(override)
        return path if path.is_file() else None
    if DEFAULT_MODEL_DIR.is_dir():
        candidates = sorted(DEFAULT_MODEL_DIR.glob("*.gguf"))
        if candidates:
            return candidates[0]
    return None


def _try_local() -> _Backend | None:
    path = find_model_path()
    if path is None:
        return None
    try:
        return LocalGgufBackend(path)
    except Exception:
        logger.exception("Local embedding model at %s could not be loaded.", path)
        return None


def _try_openai() -> _Backend | None:
    try:
        from backend.core.key_manager import get_provider_key

        api_key = get_provider_key("openai")
    except Exception:
        logger.exception("Could not read the OpenAI key while selecting an embedding backend.")
        return None
    if not api_key:
        return None
    try:
        return OpenAIBackend(api_key)
    except Exception:
        logger.exception("OpenAI embedding backend could not be initialized.")
        return None


_BACKEND_FACTORIES = {
    "local-gguf": _try_local,
    "openai": _try_openai,
    "lexical-hash": lambda: LexicalHashBackend(),
}


def _select_backend() -> _Backend:
    forced = os.environ.get(ENV_BACKEND)
    if forced:
        factory = _BACKEND_FACTORIES.get(forced)
        if factory is None:
            raise EmbeddingError(
                f"{ENV_BACKEND}={forced!r} is not one of {sorted(_BACKEND_FACTORIES)}"
            )
        backend = factory()
        if backend is None:
            raise EmbeddingError(f"{ENV_BACKEND}={forced!r} but that backend is unavailable")
        return backend

    for candidate in (_try_local, _try_openai):
        backend = candidate()
        if backend is not None:
            return backend

    logger.warning(
        "No semantic embedding backend available (no model in %s, no OpenAI key) -- "
        "falling back to the lexical encoder. Similarity will mean shared words, "
        "not meaning.",
        DEFAULT_MODEL_DIR,
    )
    return LexicalHashBackend()


def active_backend() -> _Backend:
    """Return the backend in use, selecting one on first call."""
    global _backend
    if _backend is None:
        with _backend_lock:
            if _backend is None:
                _backend = _select_backend()
    return _backend


def reset_backend() -> None:
    """Drop the cached backend so the next call re-selects.

    Exists for tests that flip the environment; production selects once.
    """
    global _backend
    with _backend_lock:
        _backend = None
    _embedding_cache.clear()


def backend_id() -> str:
    """Identifier recorded next to every vector this backend produces."""
    return active_backend().id


def dimension() -> int:
    """Vector width of the active backend."""
    return active_backend().dimension


def is_semantic() -> bool:
    """True when the active backend encodes meaning rather than words."""
    return active_backend().semantic


# ======================================================
# Public encoding API
# ======================================================
# Embedding the same text twice is common (re-indexing an unchanged note,
# a query repeated across pages) and the result is deterministic, so a small
# cache is pure win. Bounded because chunk text can be long.
_CACHE_LIMIT = 512
_embedding_cache: dict[tuple[str, str], list[float]] = {}


def _to_float32(vector: list[float]) -> list[float]:
    """Round a vector to the precision it will be stored at.

    Vectors are persisted as float32. Returning float64 here would mean a
    vector never equals the one read back from the database, which makes
    round-trip comparisons fail for no reason a caller can act on. Rounding
    at the boundary keeps "what you embed" and "what you stored" the same
    numbers.
    """
    packed = struct.pack(f"<{len(vector)}f", *vector)
    return list(struct.unpack(f"<{len(vector)}f", packed))


def _normalize(vector: list[float], expected_dim: int) -> list[float]:
    if len(vector) != expected_dim:
        raise EmbeddingError(
            f"Backend returned {len(vector)} values, expected {expected_dim}"
        )
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return [0.0] * expected_dim
    return _to_float32([value / norm for value in vector])


def embed_texts_semantic(texts: list[str]) -> list[list[float]]:
    """Embed several texts, returning one unit-length vector each."""
    if not texts:
        return []
    backend = active_backend()
    key_of = lambda text: (backend.id, text)  # noqa: E731

    missing = [text for text in texts if key_of(text) not in _embedding_cache]
    if missing:
        unique = list(dict.fromkeys(missing))
        for text, vector in zip(unique, backend.embed_batch(unique)):
            if len(_embedding_cache) >= _CACHE_LIMIT:
                _embedding_cache.clear()
            _embedding_cache[key_of(text)] = vector

    return [list(_embedding_cache[key_of(text)]) for text in texts]


def embed_text_semantic(text: str) -> list[float]:
    """Embed one text as a unit-length semantic vector."""
    return embed_texts_semantic([text])[0]


# ======================================================
# Storage format
# ======================================================
# Unchanged from the lexical encoder: little-endian float32, packed end to
# end. Only the width and the meaning of the numbers changed, which is why
# vectors carry their backend id and dimension in the database.
def encode_vector(vector: list[float]) -> bytes:
    """Pack a vector into the stored BLOB format."""
    return struct.pack(f"<{len(vector)}f", *vector)


def decode_vector(vector_bytes: bytes, expected_dim: int | None = None) -> list[float]:
    """Unpack a stored BLOB.

    expected_dim defaults to the active backend's width; pass it explicitly
    to read a vector written by a different backend.
    """
    width = struct.calcsize(_VECTOR_ITEM)
    if len(vector_bytes) % width:
        raise ValueError(
            f"Vector blob of {len(vector_bytes)} bytes is not a whole number of float32 values"
        )
    count = len(vector_bytes) // width
    if expected_dim is None:
        expected_dim = dimension()
    if count != expected_dim:
        raise ValueError(
            f"Expected {expected_dim * width} bytes for a {expected_dim}-dimension vector, "
            f"got {len(vector_bytes)}"
        )
    return list(struct.unpack(f"<{count}f", vector_bytes))


def embed_to_blob(text: str) -> bytes:
    """Embed text straight into the stored BLOB format."""
    return encode_vector(embed_text_semantic(text))


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Cosine similarity of two equal-length vectors.

    Vectors from this module are already normalized, so this is a dot
    product, but the norms are recomputed rather than assumed.
    """
    if len(left) != len(right):
        raise ValueError("Vectors must have the same length")
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)
