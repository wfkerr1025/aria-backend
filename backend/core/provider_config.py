# backend/core/provider_config.py

"""
Batch 1 stability fix — single source of truth for the cloud-provider
REGISTRY (identity, enablement, metadata), backed by one on-disk file:
backend/config/provider_config.json, schema:

    {"providers": [{"name": str, "enabled": bool, "metadata": dict}, ...]}

This is deliberately separate from:
  - backend.core.key_manager — owns SECRET storage (has-a-key yes/no,
    via OS keyring / env var). The key value itself never touches disk
    in plaintext there, and provider_config.json does not change that:
    this file never stores a key or key-shaped value, only identity/
    enablement/metadata. "apiKey" in the Batch 1 spec's schema is
    represented here as the derived boolean `hasApiKey` (from
    key_manager), never the raw secret — writing real credentials into
    a plain JSON file would be a straight regression against
    key_manager.py's documented, already-audited security model.
  - backend.llm.providers.provider_registry — owns the actual wrapper
    CLASS instances used to make API calls (auto-discovered from
    backend/llm/providers/*_wrapper.py).

Before this module, "is X a valid cloud provider" was answered
three different, not-quite-consistent ways depending which of the
above you asked, and nothing actually gated a Cloud Mode switch on
the answer — see backend.core.routing_guard, the ONLY caller allowed
to accept a cloud-provider switch, and backend/config/mode_state.json,
found on disk as {"mode": "cloud", "cloud_provider": null} before this
fix: exactly the contradiction a real registry+gate prevents.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from backend.core import key_manager
from backend.logger import log as unified_log

from logger import get_logger

logger = get_logger(__name__)

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "provider_config.json"
)

_registry: Dict[str, Dict[str, Any]] = {}

# Preference order for auto-picking a provider when Cloud Mode is
# requested without naming one — mirrors backend.core.auto_selector.
# AutoSelector.cloud_rank's relative ordering, kept as a plain literal
# here (rather than importing AutoSelector) to avoid pulling in
# complexity_router/task_classifier/model-registry machinery this
# module has no other reason to depend on.
_PREFERENCE_ORDER = (
    "anthropic", "openai", "gemini", "grok", "mistral", "deepseek",
    "cohere", "together", "openrouter", "perplexity", "huggingface",
    "replicate", "azure", "custom_http",
)


def _default_entries() -> List[Dict[str, Any]]:
    return [{"name": name, "enabled": True, "metadata": {}} for name in key_manager.KNOWN_PROVIDERS]


def _load_from_disk() -> List[Dict[str, Any]]:
    if not os.path.exists(_CONFIG_PATH):
        return _default_entries()

    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"provider_config: failed to read {_CONFIG_PATH}: {e} — using defaults")
        unified_log("provider_config", "ERROR", f"Failed to read provider_config.json: {e}")
        return _default_entries()

    entries = raw.get("providers") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        logger.error("provider_config: malformed provider_config.json (expected a 'providers' list) — using defaults")
        unified_log("provider_config", "ERROR", "Malformed provider_config.json — using defaults")
        return _default_entries()

    valid: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name"):
            logger.warning(f"provider_config: skipping malformed entry: {entry!r}")
            continue
        valid.append({
            "name": entry["name"],
            "enabled": bool(entry.get("enabled", True)),
            "metadata": entry.get("metadata") or {},
        })

    return valid or _default_entries()


def _save_to_disk(entries: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"providers": entries}, f, indent=2)


def load() -> None:
    """
    Load provider_config.json into the in-memory registry — creating the
    file (populated with every provider key_manager already knows about,
    all enabled) the first time this runs on a machine that doesn't have
    one yet, so there is always exactly one real file backing this
    registry rather than an implicit "defaults nobody wrote down".
    """
    global _registry
    entries = _load_from_disk()
    if not os.path.exists(_CONFIG_PATH):
        _save_to_disk(entries)
    _registry = {e["name"]: e for e in entries}
    logger.debug(f"provider_config: loaded {len(_registry)} provider(s)")


load()


def reload() -> None:
    """Re-read provider_config.json from disk — call after externally editing it."""
    load()
    unified_log("provider_config", "INFO", "Provider config reloaded", {"providers": list(_registry.keys())})


def get_provider_registry() -> List[Dict[str, Any]]:
    """
    Every provider object this backend knows about:
        {name, enabled, hasApiKey, metadata}
    hasApiKey is sourced live from key_manager (OS keyring / env var) —
    never a value stored in provider_config.json itself.
    """
    configured = key_manager.list_configured_providers()
    return [
        {
            "name": name,
            "enabled": entry.get("enabled", True),
            "hasApiKey": configured.get(name, False),
            "metadata": entry.get("metadata") or {},
        }
        for name, entry in sorted(_registry.items())
    ]


def is_valid_cloud_provider(name: Optional[str]) -> bool:
    """
    True if `name` is a known, enabled provider with a configured API
    key — the ONLY definition of "a valid provider is loaded" for
    entering/staying in Cloud Mode (backend.core.routing_guard).
    """
    if not name:
        return False
    entry = _registry.get(name)
    if entry is None or not entry.get("enabled", True):
        return False
    return key_manager.list_configured_providers().get(name, False)


def resolve_cloud_provider(requested: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Resolve a concrete, valid cloud provider for entering/staying in
    Cloud Mode.

    `requested` (an explicit "use X", or the mode's already-persisted
    choice) is tried first; if it's unknown, disabled, or has no
    configured key, falls through to the first valid provider in
    _PREFERENCE_ORDER instead — so "switch to Cloud Mode" with no
    provider named still lands on a real, working provider rather than
    cloud_provider=None.

    Returns (provider_name, None) on success, or (None, error_message)
    if NO valid cloud provider exists at all — the caller
    (routing_guard.attempt_mode_switch) must reject the whole switch in
    that case, never enter Cloud Mode with a null/invalid provider.
    """
    if requested and is_valid_cloud_provider(requested):
        return requested, None

    for name in _PREFERENCE_ORDER:
        if is_valid_cloud_provider(name):
            if requested:
                logger.warning(f"resolve_cloud_provider: requested '{requested}' invalid — falling back to '{name}'")
                unified_log("provider_config", "WARNING", "Cloud provider fallback", {
                    "requested": requested, "resolved": name,
                })
            return name, None

    return None, "No cloud provider is configured. Add an API key in Settings before switching to Cloud Mode."
