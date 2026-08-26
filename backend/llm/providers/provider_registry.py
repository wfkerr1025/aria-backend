import os
import importlib

from backend.logger import log as unified_log

from logger import get_logger

logger = get_logger(__name__)


# Dictionary of provider_name → Provider() instance
PROVIDERS = {}


def load_providers():
    """
    Auto-load all provider modules inside backend/llm/providers/.
    Any file ending in *_provider.py or *_wrapper.py is treated as a provider.
    """
    logger.debug("Loading providers")
    root = os.path.dirname(__file__)

    for file in os.listdir(root):
        # Only load provider modules
        if not (file.endswith("_provider.py") or file.endswith("_wrapper.py")):
            continue

        module_name = file[:-3]  # strip .py
        provider_name = module_name.replace("_provider", "").replace("_wrapper", "")

        try:
            logger.debug(f"Importing module: {module_name}")
            module = importlib.import_module(f"backend.llm.providers.{module_name}")

            # Every provider module must expose a class named Provider
            provider_class = getattr(module, "Provider", None)
            if provider_class is None:
                logger.warning(f"{module_name} has no Provider class — skipping")
                unified_log("provider_registry", "WARNING", f"Provider load skipped: {module_name} has no Provider class", {
                    "module": module_name,
                })
                continue

            PROVIDERS[provider_name] = provider_class()
            logger.debug(f"Loaded provider: {provider_name}")
            unified_log("provider_registry", "INFO", f"Provider loaded: {provider_name}", {
                "module": module_name,
            })

        except Exception as e:
            logger.exception(f"ERROR loading provider '{module_name}': {e}")
            unified_log("provider_registry", "ERROR", f"Provider load failed: {module_name}: {e}", {
                "module": module_name,
            })


# Load providers immediately on import
load_providers()


def reload_providers():
    """
    Re-import and re-instantiate every provider. Each Provider.__init__
    reads its API key via os.getenv(...) exactly once, so a provider
    instance created before a key existed keeps using its original
    (empty) key forever otherwise. Call this after
    backend.core.key_manager changes a provider's key (set or delete)
    so the change takes effect immediately, without a process restart —
    see key_manager.set_provider_key()/delete_provider_key().
    """
    logger.debug("Reloading providers")
    PROVIDERS.clear()
    load_providers()
    unified_log("provider_registry", "INFO", "Providers reloaded", {"providers": list(PROVIDERS.keys())})


def get_provider(provider_name: str):
    """
    Return the provider instance for the given provider name.
    Example: "openai", "anthropic", "local", "groq", etc.
    """
    logger.debug(f"get_provider() → {provider_name}")
    return PROVIDERS.get(provider_name)


def list_providers():
    """
    Return a list of all loaded provider names.
    """
    logger.debug("list_providers() called")
    return list(PROVIDERS.keys())


# ---------------------------------------------------------------------------
# Human-readable display names — "OpenAI"/"Anthropic", not the raw
# lowercase provider id these irregular capitalizations can't be derived
# from mechanically (str.title() would give "Openai"). Single source of
# truth for anywhere a provider name is shown to a user (the bottom-left
# active-model status bar in particular — see
# backend/core/streaming_engine.py's active_model_changed packet and
# backend/ipc_router.py's mode_status_result) so it's never spelled two
# different ways in two different UI surfaces.
# ---------------------------------------------------------------------------
PROVIDER_DISPLAY_NAMES = {
    "local": "Local",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "gemini": "Gemini",
    "grok": "Grok",
    "mistral": "Mistral",
    "cohere": "Cohere",
    "together": "Together AI",
    "openrouter": "OpenRouter",
    "huggingface": "Hugging Face",
    "replicate": "Replicate",
    "perplexity": "Perplexity",
    "azure": "Azure OpenAI",
    "custom_http": "Custom",
    "deepseek": "DeepSeek",
}


def get_provider_display_name(provider_name):
    """
    "openai" -> "OpenAI", "anthropic" -> "Anthropic", etc. Falls back to
    title-casing an unrecognized provider id (still better than showing
    the raw lowercase string) rather than raising — this is presentation
    only, never a validity check.
    """
    if not provider_name:
        return None
    return PROVIDER_DISPLAY_NAMES.get(provider_name, provider_name.title())
