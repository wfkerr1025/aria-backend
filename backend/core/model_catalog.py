# backend/core/model_catalog.py

"""
Read-only catalog of local models NOT bundled with ARIA Lite and not
yet downloadable through it -- see backend/config/model_catalog.json
and backend.core.model_manager.install_model()'s own docstring (this
repo has no download/network layer yet). This module's only job is to
load that static list and annotate each entry with whether the CURRENT
machine's backend.core.pc_capability_tier can actually run it, so the
webui's Models page "Available" grid can render a real reason instead
of a bare disabled button.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from .pc_capability_tier import get_pc_tier

from logger import get_logger

logger = get_logger(__name__)

_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "model_catalog.json")


def _load_raw() -> List[Dict[str, Any]]:
    try:
        with open(_CATALOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("entries", [])
    except (OSError, json.JSONDecodeError) as e:
        logger.debug(f"model_catalog._load_raw() → failed to read {_CATALOG_PATH}: {e}")
        return []


def list_catalog_entries() -> List[Dict[str, Any]]:
    """
    Every catalog entry, annotated with:
      - "supported": bool -- whether this machine's current PC tier meets requiredTier
      - "pc_tier": the current machine's own pc_capability_tier.get_pc_tier() result,
        repeated on every entry so the frontend can explain "why disabled"
        without a second round trip.
    """
    pc_tier = get_pc_tier()
    entries = []
    for entry in _load_raw():
        annotated = dict(entry)
        annotated["supported"] = pc_tier["tier"] >= entry.get("requiredTier", 0)
        annotated["pc_tier"] = pc_tier
        entries.append(annotated)
    return entries
