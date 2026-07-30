from __future__ import annotations
from typing import List, Dict, Any

from backend.llm.intent_scorer import IntentScorer
from backend.llm.provider_registry import PROVIDERS
from backend.llm.provider_health import provider_health_monitor


def select_providers_for_intent(intent: str) -> List[Dict[str, Any]]:
    """
    Capability-based provider ranking.
    Returns strongest → weakest providers for the given intent.
    """

    ranked = []

    for name, meta in PROVIDERS.items():
        if not meta.get("installed", False):
            continue

        score = meta["capabilities"].get(intent, 0.0)
        if score <= 0.0:
            continue

        ranked.append({
            "name": name,
            "score": score,
            "model": meta.get("model", "default"),
            "client": meta.get("client"),
        })

    ranked.sort(key=lambda p: p["score"], reverse=True)
    return ranked


def choose_provider(prompt: str, intent: str, engine) -> Dict[str, Any] | None:
    """
    Complexity-aware + availability-aware + health-aware provider selection.
    """

    complexity = IntentScorer.score(prompt)
    ranked = select_providers_for_intent(intent)

    if not ranked:
        return None

    # Helper: check availability + health
    def usable(p):
        client = engine._get_or_create_client(p["name"])
        if client is None or not client.is_available():
            return False

        health = provider_health_monitor.get_health(p["name"])
        if health < 0.30:  # skip degraded providers
            return False

        return True

    # -------------------------------
    # Tier 1: Local mistral (0–3)
    # -------------------------------
    if complexity <= 3:
        for p in ranked:
            if p["name"] == "local" and usable(p):
                return p

    # -------------------------------
    # Tier 2: Mid-tier cloud (4–7)
    # -------------------------------
    if 4 <= complexity <= 7:
        for p in ranked:
            if p["name"] in ("openai", "azure") and usable(p):
                return p

    # -------------------------------
    # Tier 3: High-tier cloud (8–10)
    # -------------------------------
    if complexity >= 8:
        for p in ranked:
            if p["name"] in ("openai",) and usable(p):
                return p

    # -------------------------------
    # Fallback: strongest healthy provider
    # -------------------------------
    for p in ranked:
        if usable(p):
            return p

    return None
