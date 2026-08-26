# backend/core/provider_interface.py

"""
Unified provider interface — Phase 2, item 1.

backend.llm.providers.provider_registry already has 15+ real, working
providers (local GGUF via local_provider.py, OpenAI, Anthropic, Cohere,
DeepSeek, Gemini, Grok, HuggingFace, Mistral, OpenRouter, Perplexity,
Replicate, Together, Azure, custom HTTP), loaded by pure duck-typing:
any `*_provider.py`/`*_wrapper.py` file exposing a no-arg-constructible
class literally named `Provider` gets registered, and the only methods
ever actually invoked on it are `.run(request) -> str` (mandatory) and
optionally `.stream(request, callback) -> None` (backend.core.
streaming_engine.StreamingEngine synthesizes a single fake chunk when
absent).

Rewriting all 15 wrappers to a new ABC would touch a lot of live,
working code for no functional gain — the adapter below instead wraps
whatever object provider_registry already produces and exposes the six
requested methods (load/infer/stream/metadata/diagnostics/requirements)
on top of it, so every existing and future provider_registry entry gets
the unified interface for free without changing a single wrapper file.

get_unified_provider()/list_unified_providers() are the new entry
points; nothing here changes provider_registry.py's own behavior or
what backend.core.streaming_engine.StreamingEngine does today.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from backend.llm.providers import provider_registry
from .model_registry import get_model
from .model_info import build_model_info, ModelInfo
from .resource_monitor import get_resource_snapshot
from .compatibility_checker import check_requirements
from . import perf_profiler

from logger import get_logger

logger = get_logger(__name__)


class ProviderInterface(ABC):
    """The six methods every provider must expose, per this feature's spec."""

    @abstractmethod
    def load(self) -> Dict[str, Any]:
        """Prepare the provider to serve requests. For local models this
        actually loads GGUF weights; for a stateless remote API this is
        just an availability/credential check. Returns {"ok": bool, ...}."""

    @abstractmethod
    def infer(self, request: Any) -> str:
        """Non-streaming completion — returns the full response text."""

    @abstractmethod
    def stream(self, request: Any, callback: Callable[[dict], None]) -> None:
        """Streaming completion — calls callback({"content": <token>}) per chunk."""

    @abstractmethod
    def metadata(self) -> Dict[str, Any]:
        """Static identity: provider name, location (local/cloud), and — for
        a local model — the Phase-1 ModelInfo (params/quant/context/...)."""

    @abstractmethod
    def diagnostics(self) -> Dict[str, Any]:
        """Live health: is_available, and — for local models — a compat
        check against current hardware."""

    @abstractmethod
    def requirements(self) -> Dict[str, Any]:
        """What this provider needs to run: hardware requirements for a
        local model, or {"requires_api_key": true} for a remote one."""


@dataclass
class ProviderAdapter(ProviderInterface):
    """
    Wraps one provider_registry entry (whatever duck-typed object
    load_providers() produced) to expose ProviderInterface. `model_id`
    is only meaningful for the "local" provider (provider_registry has
    exactly one local entry serving whichever model is currently
    active/requested; every other registered name is a stateless remote
    API with no single "model" of its own).
    """

    provider_name: str
    _provider: Any
    model_id: Optional[str] = None

    @property
    def is_local(self) -> bool:
        return self.provider_name == "local"

    def load(self) -> Dict[str, Any]:
        with perf_profiler.timed(f"provider.{self.provider_name}.load"):
            if self.is_local:
                model_id = self.model_id or getattr(self._provider, "get_active_model_id", lambda: None)()
                if not model_id:
                    return {"ok": False, "reason": "No active local model configured."}
                try:
                    loader = getattr(self._provider, "loader", None)
                    if loader is not None:
                        loader.load_model(model_id)
                    return {"ok": True, "model_id": model_id}
                except Exception as e:
                    logger.debug(f"ProviderAdapter.load() → local load failed: {e}")
                    return {"ok": False, "reason": str(e)}

            available = self._safe_is_available()
            return {"ok": available, "reason": None if available else "Provider not configured/available."}

    def infer(self, request: Any) -> str:
        with perf_profiler.timed(f"provider.{self.provider_name}.infer"):
            return self._provider.run(request)

    def stream(self, request: Any, callback: Callable[[dict], None]) -> None:
        with perf_profiler.timed(f"provider.{self.provider_name}.stream"):
            if hasattr(self._provider, "stream"):
                self._provider.stream(request, callback)
            else:
                # Same fallback backend.core.streaming_engine.StreamingEngine
                # already uses for a run()-only provider — one synthesized
                # chunk containing the whole response, so callers of this
                # adapter never need to special-case non-streaming providers.
                result = self._provider.run(request)
                callback({"content": result})

    def metadata(self) -> Dict[str, Any]:
        base = {
            "provider_name": self.provider_name,
            "location": "local" if self.is_local else "cloud",
            "display_name": provider_registry.get_provider_display_name(self.provider_name),
        }
        if self.is_local and self.model_id:
            model_cfg = get_model(self.model_id)
            if model_cfg:
                base["model_info"] = build_model_info(model_cfg, get_resource_snapshot()).to_dict()
        return base

    def diagnostics(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "provider_name": self.provider_name,
            "available": self._safe_is_available(),
        }
        if self.is_local and self.model_id:
            model_cfg = get_model(self.model_id)
            if model_cfg:
                result["compat"] = check_requirements(get_resource_snapshot(), model_cfg)
        return result

    def requirements(self) -> Dict[str, Any]:
        if self.is_local and self.model_id:
            model_cfg = get_model(self.model_id)
            if model_cfg:
                info = build_model_info(model_cfg)
                return info.requirements
        return {"requires_api_key": True, "location": "cloud"}

    def _safe_is_available(self) -> bool:
        try:
            if hasattr(self._provider, "is_available"):
                return bool(self._provider.is_available())
            return True
        except Exception as e:
            logger.debug(f"ProviderAdapter._safe_is_available() → {self.provider_name} check failed: {e}")
            return False


def get_unified_provider(provider_name: str, model_id: Optional[str] = None) -> Optional[ProviderAdapter]:
    """The one new entry point — wraps whatever provider_registry.get_provider()
    already returns. Returns None for an unknown provider name, exactly
    matching get_provider()'s own None-on-miss contract."""
    raw = provider_registry.get_provider(provider_name)
    if raw is None:
        return None
    return ProviderAdapter(provider_name=provider_name, _provider=raw, model_id=model_id)


def list_unified_providers() -> List[str]:
    return provider_registry.list_providers()
