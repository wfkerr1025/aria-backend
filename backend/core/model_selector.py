# backend/core/model_selector.py

"""
Batch 2 — single source of truth for "given a mode + provider, which
model is actually active".

Before this module, cloud requests never carried a real model id at
all: backend.core.provider_router.ProviderRouter.resolve()'s cloud
branch returned (provider, None), which every cloud provider wrapper
(backend/llm/providers/*_wrapper.py) then sent to the real API as
`"model": None` — a request that would fail against any real provider,
not just a display-layer inconsistency. Local model selection was
already centralized in backend.core.model_registry /
backend.core.complexity_router, but scattered call sites each re-derived
"is this id valid for this mode" slightly differently (see Batch 1's
routing_invariants.py for the same observation about mode/state
validation).

select_cloud_model() / select_local_model() are the ONLY functions
allowed to decide "which concrete model backs this mode+provider" —
backend.core.provider_router.ProviderRouter.resolve(),
backend.core.routing_guard (switch-time validation), and
backend.core.streaming_engine (the per-turn active_model_changed
broadcast) all call through here instead of re-deriving it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

from backend.core import model_registry
from backend.core import provider_config
from backend.llm.providers.provider_registry import get_provider_display_name


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    display_name: str
    provider: str
    provider_display_name: Optional[str]
    location: str  # "local" | "cloud"


@dataclass(frozen=True)
class SelectionError:
    code: str
    message: str


SelectionResult = Union[ModelInfo, SelectionError]


# ---------------------------------------------------------------------------
# Provider -> (default cloud model id, human display name). This is the
# ONE place a cloud provider's default model is named. Overridable per
# provider via provider_config.json's metadata.preferred_cloud_model
# (see backend.core.provider_config) — e.g.
# {"name": "openai", "metadata": {"preferred_cloud_model": "gpt-4-turbo"}}.
# These are deliberately plain, stable model-family names rather than
# dated snapshot ids (which drift and go stale) — a deployment that
# needs a specific dated snapshot should set the override instead.
#
# THIS TABLE ROTS, AND NOTHING TELLS YOU
# It sat at gpt-4 / claude-3-opus / gemini-1.5-pro long after all three
# were superseded, and nothing failed loudly: the ids only reach a real
# API when a cloud turn actually runs, and until the double resolution
# in generation.py was fixed, an Automatic cloud decision never got that
# far. So a row here is checked, or it is a guess, and the two are
# marked apart below rather than left to look alike.
#
# An alias id (mistral-large-latest, openrouter/auto, deepseek-chat)
# tracks its provider's current model and is the right shape for this
# table. A named id is a snapshot of a check on a date.
#
# CHECKED 2026-09-03 against each provider's own documentation:
#   anthropic, openai, gemini, grok, cohere
# NOT CHECKED — still whatever they were, and some are certainly dead
# (perplexity's llama-3.1-sonar id in particular). Left rather than
# guessed at: a wrong id here is the failure this comment is about. Set
# metadata.preferred_cloud_model for any of these before relying on it:
#   together, perplexity, huggingface, replicate, azure
# ---------------------------------------------------------------------------
CLOUD_DEFAULT_MODELS: dict[str, tuple[str, str]] = {
    "anthropic": ("claude-opus-5", "Claude Opus 5"),
    "openai": ("gpt-6-astra", "GPT-6 Astra"),
    # 3.1 Pro is the Pro-tier flagship but ships as a preview id, and a
    # preview id is exactly what rots. This is the current stable one.
    "gemini": ("gemini-3.8-flash", "Gemini 3.8 Flash"),
    "grok": ("grok-4.6", "Grok 4.6"),
    "mistral": ("mistral-large-latest", "Mistral Large"),
    "deepseek": ("deepseek-chat", "DeepSeek Chat"),
    "cohere": ("command-a-plus-05-2026", "Command A+"),
    "together": ("meta-llama/Llama-3-70b-chat-hf", "Llama 3 70B"),
    "openrouter": ("openrouter/auto", "OpenRouter Auto"),
    "perplexity": ("llama-3.1-sonar-large-128k-online", "Sonar Large"),
    "huggingface": ("meta-llama/Meta-Llama-3-70B-Instruct", "Llama 3 70B"),
    "replicate": ("meta/meta-llama-3-70b-instruct", "Llama 3 70B"),
    "azure": ("gpt-4", "GPT-4 (Azure)"),
    "custom_http": ("custom", "Custom Model"),
}


def select_cloud_model(provider_name: Optional[str]) -> SelectionResult:
    """
    Resolve the concrete cloud model that backs `provider_name`.

    Fails (CLOUD_MODEL_RESOLUTION_FAILED) if no provider is given, the
    provider isn't a known/enabled/configured entry in
    backend.core.provider_config's registry, or there's no default (or
    override) model mapping for it — never returns a ModelInfo with a
    guessed or null model_id.
    """
    if not provider_name:
        return SelectionError("CLOUD_MODEL_RESOLUTION_FAILED", "No cloud provider given.")

    if not provider_config.is_valid_cloud_provider(provider_name):
        return SelectionError(
            "CLOUD_MODEL_RESOLUTION_FAILED",
            f"Provider '{provider_name}' is not configured or enabled.",
        )

    entry = next((e for e in provider_config.get_provider_registry() if e["name"] == provider_name), None)
    override = (entry.get("metadata") or {}).get("preferred_cloud_model") if entry else None

    if override:
        model_id, display_name = override, override
    else:
        default = CLOUD_DEFAULT_MODELS.get(provider_name)
        if not default:
            return SelectionError(
                "CLOUD_MODEL_RESOLUTION_FAILED",
                f"No default model mapping for provider '{provider_name}'.",
            )
        model_id, display_name = default

    return ModelInfo(
        model_id=model_id,
        display_name=display_name,
        provider=provider_name,
        provider_display_name=get_provider_display_name(provider_name),
        location="cloud",
    )


def select_local_model(model_id: Optional[str]) -> SelectionResult:
    """
    Resolve a local model. `model_id=None` picks the persisted default
    (backend.core.model_registry.get_default_model_id()). A given
    model_id is validated against the registry — an unknown id, or one
    belonging to a non-local provider, fails (LOCAL_MODEL_INVALID)
    rather than silently substituting something else; callers that want
    a "fall back to default" behavior pass None instead of guessing.
    """
    if model_id is None:
        model_id = model_registry.get_default_model_id()
        if not model_id:
            return SelectionError("LOCAL_MODEL_INVALID", "No local model is available.")

    cfg = model_registry.get_model(model_id)
    if cfg is None:
        return SelectionError("LOCAL_MODEL_INVALID", f"Unknown local model_id: {model_id!r}")
    if cfg.get("provider") != "local":
        return SelectionError("LOCAL_MODEL_INVALID", f"Model '{model_id}' is not a local model.")

    return ModelInfo(
        model_id=model_id,
        display_name=cfg.get("name") or model_id,
        provider="local",
        provider_display_name="Local",
        location="local",
    )


def is_cloud_model_id(model_id: Optional[str]) -> bool:
    """
    True if `model_id` matches a known cloud default (or override) model
    id — used by routing_invariants.py to catch a cloud model id
    surviving into routing_mode="local" state, the symmetric case to a
    local model id surviving into routing_mode="cloud".
    """
    if not model_id:
        return False
    if any(model_id == default_id for default_id, _ in CLOUD_DEFAULT_MODELS.values()):
        return True
    for entry in provider_config.get_provider_registry():
        if (entry.get("metadata") or {}).get("preferred_cloud_model") == model_id:
            return True
    return False
