from backend.llm.provider_registry import PROVIDERS

def select_providers_for_intent(intent: str) -> list[dict]:
    """
    Dynamic provider selection based on capability scoring.
    Returns a sorted list of provider metadata dicts.
    """

    ranked = []

    for name, meta in PROVIDERS.items():
        # Skip providers not installed
        if not meta.get("installed", False):
            continue

        # Capability score for this intent
        score = meta["capabilities"].get(intent, 0.0)

        # Skip providers with no capability for this intent
        if score <= 0.0:
            continue

        ranked.append({
            "name": name,
            "score": score,
            "model": meta.get("model", "default"),
            "client": meta.get("client"),
        })

    # Sort strongest → weakest
    ranked.sort(key=lambda p: p["score"], reverse=True)
    return ranked


def select_best_provider(intent: str) -> dict | None:
    """
    Returns the strongest available provider for the given intent.
    """
    ranked = select_providers_for_intent(intent)
    return ranked[0] if ranked else None


def select_provider_with_fallback(intent: str) -> list[dict]:
    """
    Returns a full fallback chain:
    strongest → weakest.
    """
    return select_providers_for_intent(intent)
