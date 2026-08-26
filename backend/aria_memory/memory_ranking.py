"""ARIA Lite - memory ranking.

Scores retrieved memory items against a query using four signals and sorts
by the combination. This sits above retrieval: hybrid search decides WHICH
items are candidates, ranking decides which of them ARIA should read first.

The signals answer different questions, which is why one alone ranks badly:

    semantic_score   does this mean the same thing as the query?
    keyword_score    does this contain the words of the query?
    recency_score    how likely is this still true?
    type_score       how much does this kind of memory deserve priority?

Semantic and keyword scores are produced upstream by the searches that found
the item and are used as-is; this module never re-runs retrieval. It will
compute a missing semantic score from the query embedding when an item
arrives without one, so a keyword-only hit is still ranked on meaning rather
than being treated as meaningless.

Ranking is pure and deterministic: it copies items rather than mutating
them, sorts stably so equal scores keep their input order, and depends on
nothing but the inputs and the clock value passed in.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from logger import get_logger

try:
    from backend.llm import semantic_embeddings
    from backend.llm.query_deframing import deframe_query
except ImportError:  # running from inside the backend directory
    from llm import semantic_embeddings
    from llm.query_deframing import deframe_query

logger = get_logger(__name__)

# ======================================================
# Weights
# ======================================================
# Meaning dominates, exact wording backs it up, freshness breaks near-ties,
# and the kind of memory is a nudge rather than a decision.
WEIGHT_SEMANTIC = 0.45
WEIGHT_KEYWORD = 0.25
WEIGHT_RECENCY = 0.20
WEIGHT_TYPE = 0.10

# Half-life constant in hours, applied as exp(-age_hours / RECENCY_DECAY_HOURS).
# At 720 hours an item retains exp(-1) = 0.368 of its recency weight.
RECENCY_DECAY_HOURS = 720.0

# Recency for an item with no usable timestamp: deliberately mid-scale, so an
# undated item is neither promoted nor buried by a signal we cannot measure.
DEFAULT_RECENCY_SCORE = 0.5

# Per-kind priority. Self-knowledge outranks a note because it describes what
# ARIA is rather than something it was told; a chunk is a fragment of a note
# and slightly less useful than the whole; context keys are small facts that
# rarely answer a question on their own. Extend by adding an entry.
TYPE_WEIGHTS = {
    "note": 1.0,
    "chunk": 0.9,
    # A file chunk is a fragment of a document the user did not write
    # deliberately, so it sits with note chunks rather than whole notes.
    "file_chunk": 0.9,
    "self": 1.1,
    "self_knowledge": 1.1,
    "context": 0.8,
}
DEFAULT_TYPE_WEIGHT = 1.0

# The weighted sum can exceed 1.0 because the highest type weight is above
# 1.0, so combined scores are divided by their own ceiling to land in [0, 1].
MAX_COMBINED = (
    WEIGHT_SEMANTIC
    + WEIGHT_KEYWORD
    + WEIGHT_RECENCY
    + WEIGHT_TYPE * max(TYPE_WEIGHTS.values())
)


# ======================================================
# Helpers
# ======================================================
def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else float(value)


def _as_datetime(value) -> datetime | None:
    """Coerce a stored timestamp into an aware datetime, or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def recency_score(item: dict, now_ts=None) -> float:
    """Exponential decay on the item's age.

    Uses updated_at when present, else created_at: an edited note is as
    current as its edit. An item with no usable timestamp scores
    DEFAULT_RECENCY_SCORE rather than 0.0 -- missing data is not evidence of
    staleness. A timestamp in the future is treated as now.
    """
    stamp = _as_datetime(item.get("updated_at") or item.get("created_at"))
    if stamp is None:
        return DEFAULT_RECENCY_SCORE

    now = _as_datetime(now_ts) or datetime.now(timezone.utc)
    age_hours = (now - stamp).total_seconds() / 3600.0
    if age_hours <= 0.0:
        return 1.0
    return math.exp(-age_hours / RECENCY_DECAY_HOURS)


def type_score(item: dict) -> float:
    """Weight for the kind of memory this item is."""
    return TYPE_WEIGHTS.get(item.get("type"), DEFAULT_TYPE_WEIGHT)


def _semantic_score(item: dict, query_embedding) -> float:
    """The item's semantic relevance.

    Prefers a score the search already computed. Falls back to comparing the
    query embedding against the item's stored vector, then to embedding the
    item's own text -- so an item that arrived from keyword search alone is
    still scored on meaning.
    """
    for key in ("semantic_score", "score"):
        if item.get(key) is not None:
            return float(item[key])

    if query_embedding is None:
        return 0.0

    vector = item.get("vector")
    if isinstance(vector, (bytes, bytearray)):
        try:
            vector = semantic_embeddings.decode_vector(bytes(vector), len(query_embedding))
        except ValueError:
            vector = None  # a vector from another encoder is not comparable
    if isinstance(vector, list) and len(vector) == len(query_embedding):
        return semantic_embeddings.cosine_similarity(query_embedding, vector)

    text = item.get("text") or item.get("chunk") or item.get("value")
    if not text:
        return 0.0
    return semantic_embeddings.cosine_similarity(
        query_embedding, semantic_embeddings.embed_text_semantic(str(text))
    )


# ======================================================
# Public API
# ======================================================
def normalize_scores(
    keyword_score: float,
    semantic_score: float,
    recency_score: float,
    type_score: float,
) -> float:
    """Combine the four signals into one score in [0, 1].

    Each signal except the type weight is clamped to [0, 1] first: a cosine
    similarity can legitimately be negative, and "less alike than unrelated"
    and "unrelated" should rank the same rather than letting one signal push
    a combined score below zero.
    """
    combined = (
        WEIGHT_SEMANTIC * _clamp01(semantic_score)
        + WEIGHT_KEYWORD * _clamp01(keyword_score)
        + WEIGHT_RECENCY * _clamp01(recency_score)
        + WEIGHT_TYPE * float(type_score)
    )
    return _clamp01(combined / MAX_COMBINED)


def compute_scores(item: dict, query_embedding, now_ts=None) -> dict:
    """Score one item, returning every signal alongside the combination.

    The individual scores are returned, not just the total, so a caller can
    see why something ranked where it did.
    """
    semantic = _semantic_score(item, query_embedding)
    keyword = float(item.get("keyword_score") or 0.0)
    recency = recency_score(item, now_ts)
    type_weight = type_score(item)

    return {
        "semantic_score": semantic,
        "keyword_score": keyword,
        "recency_score": recency,
        "type_score": type_weight,
        "combined_score": normalize_scores(keyword, semantic, recency, type_weight),
    }


def rank_items(
    items: list[dict],
    query: str,
    limit: int | None = None,
    now_ts=None,
) -> list[dict]:
    """Score every item against the query and sort best first.

    The query is embedded once for the whole batch rather than per item.
    Returns copies with the scores attached; the input dicts are untouched,
    so ranking the same list twice gives the same answer.

    Ties keep their input order -- the sort is stable and the key is the
    combined score alone, so a tie is resolved by whatever order retrieval
    produced rather than by anything arbitrary.

    now_ts fixes the clock that recency is measured against; it defaults to
    the current time and exists so a caller (or a test) can rank a batch
    against one consistent instant.
    """
    if not items:
        return []

    query_embedding = None
    if query:
        try:
            query_embedding = semantic_embeddings.embed_text_semantic(deframe_query(query))
        except Exception:
            # Ranking must not fail because an embedding backend is down; the
            # other three signals still order the list.
            logger.exception("Query could not be embedded; ranking without the semantic signal.")
            query_embedding = None

    now = _as_datetime(now_ts) or datetime.now(timezone.utc)

    ranked = []
    for item in items:
        scored = dict(item)
        scored.update(compute_scores(item, query_embedding, now))
        ranked.append(scored)

    ranked.sort(key=lambda entry: entry["combined_score"], reverse=True)
    return ranked[:limit] if limit is not None else ranked
