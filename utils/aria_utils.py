import sys
import time
import os
import json
import math
from pathlib import Path
from openai import OpenAI

from logger import get_logger

logger = get_logger(__name__)


# ============================================================
# ADAPTIVE SCANNING (Unified)
# ============================================================
def adaptive_scan_line(path, last_update, enabled, tag, interval=0.05):
    """
    Updates a single-line scanner output if adaptive scanning is enabled.
    Unified behavior:
      - throttled updates
      - deterministic output
      - safe for large scans
    """
    now = time.time()
    if enabled and (now - last_update) > interval:
        sys.stdout.write(f"\r{tag} Scanning: {path}      ")
        sys.stdout.flush()
        return now
    return last_update


def clear_scan_line():
    """Clears the adaptive scan line."""
    sys.stdout.write("\r" + " " * 140 + "\r")
    sys.stdout.flush()


def should_enable_scanner(start_time, file_count, byte_count, depth):
    """
    Determines whether the adaptive scanner should activate.
    Unified heuristics:
      - time threshold
      - file count threshold
      - byte threshold
      - directory depth threshold
    """
    if time.time() - start_time > 0.5:
        return True
    if file_count > 200:
        return True
    if byte_count > 50 * 1024 * 1024:  # 50 MB
        return True
    if depth > 5:
        return True
    return False


# ============================================================
# EMBEDDING CACHE (Unified)
# ============================================================
EMBED_CACHE_FILE = ".aria_cache/embeddings.json"


def load_embedding_cache():
    """Load embedding cache safely."""
    path = Path(EMBED_CACHE_FILE)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_embedding_cache(cache):
    """Save embedding cache safely."""
    path = Path(EMBED_CACHE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception as e:
        logger.exception(f"[ERROR] Failed to save embedding cache: {e}")


# ============================================================
# EMBEDDING ENGINE (Unified)
# ============================================================
def embed_text(client: OpenAI, text: str):
    """
    Generate an embedding vector using OpenAI.
    Unified model + deterministic return.
    """
    resp = client.embeddings.create(
        model="text-embedding-3-small",
        input=text
    )
    return resp.data[0].embedding


def get_cached_embedding(client: OpenAI, key: str, text: str, cache: dict):
    """
    Return cached embedding or compute and store it.
    Unified caching behavior.
    """
    if key in cache:
        return cache[key]

    emb = embed_text(client, text)
    cache[key] = emb
    return emb


# ============================================================
# VECTOR MATH (Unified)
# ============================================================
def cosine_similarity(a, b):
    """Compute cosine similarity between two embedding vectors."""
    if not a or not b:
        return 0.0

    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return dot / (norm_a * norm_b)


# ============================================================
# TEXT CHUNKING (Unified)
# ============================================================
def chunk_text(text: str, max_chars=1200, overlap=200):
    """
    Split text into overlapping chunks.
    Unified chunking behavior:
      - deterministic overlap
      - safe for large files
    """
    chunks = []
    start = 0
    length = len(text)

    while start < length:
        end = min(start + max_chars, length)
        chunk = text[start:end]
        chunks.append(chunk)
        start = end - overlap
        if start < 0:
            start = 0

    return chunks


# ============================================================
# FILE LOADING (Unified)
# ============================================================
def load_file_content(path: str):
    """
    Safe file loader used by search + patch engine.
    Unified return format:
      { "exists": bool, "content": str?, "error": str? }
    """
    try:
        p = Path(path)
        if not p.exists():
            return {"exists": False}

        content = p.read_text(encoding="utf-8", errors="ignore")
        return {"exists": True, "content": content}

    except Exception as e:
        return {"exists": False, "error": str(e)}


# ============================================================
# PROJECT INDEX (Unified)
# ============================================================
PROJECT_INDEX_FILE = ".aria_cache/project_index.json"


def load_project_index_raw():
    """
    Load the raw project index JSON.
    Unified behavior:
      - safe load
      - deterministic fallback
    """
    path = Path(PROJECT_INDEX_FILE)
    if not path.exists():
        return {}

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_project_index_raw(index_data):
    """
    Save the raw project index JSON.
    Unified behavior:
      - safe write
      - deterministic error reporting
    """
    path = Path(PROJECT_INDEX_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        path.write_text(json.dumps(index_data, indent=2), encoding="utf-8")
    except Exception as e:
        logger.exception(f"[ERROR] Failed to save project index: {e}")
