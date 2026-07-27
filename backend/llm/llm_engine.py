# backend/llm/llm_engine.py

from __future__ import annotations
from typing import Dict, Any, Optional, List

import os

from backend.llm.intent_detector import IntentDetector
from backend.llm.provider_registry import PROVIDERS
from backend.llm.provider_selector import select_providers_for_intent
from backend.llm.local_client import LocalLLMClient

class LLMClientBase:
    """
    Base interface for all LLM provider clients.
    """

    def generate(self, prompt: str, context: Dict[str, Any] | None = None) -> str:
        raise NotImplementedError


class OpenAIClient(LLMClientBase):
    """
    OpenAI provider client.
    Assumes OPENAI_API_KEY is set in environment.
    """

    def __init__(self, model: str):
        self.model = model
        try:
            import openai  # type: ignore
            self._openai = openai
            self._openai.api_key = os.getenv("OPENAI_API_KEY", "")
        except Exception:
            self._openai = None

    def generate(self, prompt: str, context: Dict[str, Any] | None = None) -> str:
        if self._openai is None or not self._openai.api_key:
            raise RuntimeError("OpenAI client not available or API key missing")

        # Simple Chat Completions call; you can refine this later.
        resp = self._openai.ChatCompletion.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are ARIA Lite's co-developer."},
                {"role": "user", "content": prompt},
            ],
        )
        return resp["choices"][0]["message"]["content"]


class LLMEngine:
    """
    Unified LLM orchestration engine for ARIA Lite.
    - Detects intent
    - Scores providers
    - Selects strongest provider
    - Falls back dynamically
    - Returns structured results obeying backend contract
    """

    def __init__(self):
        self.intent_detector = IntentDetector()
        self.provider_clients: Dict[str, LLMClientBase] = {}

        # Initialize known clients based on PROVIDERS
        for name, meta in PROVIDERS.items():
            if not meta.get("installed", False):
                continue

            if name == "local":
                self.provider_clients[name] = LocalLLMClient()
            elif name == "openai":
                self.provider_clients[name] = OpenAIClient(model=meta.get("model", "gpt-4o"))
            # Other providers (grok, azure, atherial) can be wired later.

    def _get_or_create_client(self, provider_name: str) -> Optional[LLMClientBase]:
        client = self.provider_clients.get(provider_name)
        if client:
            return client

        # Lazy creation if needed later
        meta = PROVIDERS.get(provider_name)
        if not meta or not meta.get("installed", False):
            return None

        if provider_name == "local":
            client = LocalLLMClient()
        elif provider_name == "openai":
            client = OpenAIClient(model=meta.get("model", "gpt-4o"))
        else:
            return None

        self.provider_clients[provider_name] = client
        return client

    def generate(self, prompt: str, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
        print("LLM ENGINE RECEIVED:", prompt, context)   # <‑‑ ADD THIS

        context = context or {}
        intent = context.get("intent") or self.intent_detector.detect(prompt)

        try:
            ranked: List[Dict[str, Any]] = select_providers_for_intent(intent or "")
        except Exception as e:
            return {
                "status": "error",
                "success": False,
                "provider": None,
                "model": None,
                "intent": intent,
                "content": "",
                "detail": f"Provider selection failed: {e}",
            }

        if not ranked:
            return {
                "status": "error",
                "success": False,
                "provider": None,
                "model": None,
                "intent": intent,
                "content": "",
                "detail": "No available providers for this intent",
            }

        last_error: Optional[str] = None

        for provider_meta in ranked:
            name = provider_meta["name"]
            model = provider_meta.get("model", "default")

            client = self._get_or_create_client(name)
            if client is None:
                last_error = f"Provider '{name}' not available or not wired"
                continue

            try:
                text = client.generate(prompt=prompt, context=context)
                return {
                    "status": "ok",
                    "success": True,
                    "provider": name,
                    "model": model,
                    "intent": intent,
                    "content": text,
                    "detail": None,
                }
            except Exception as e:
                last_error = f"{name}: {e}"
                continue

        return {
            "status": "error",
            "success": False,
            "provider": None,
            "model": None,
            "intent": intent,
            "content": "",
            "detail": last_error or "All providers failed",
        }

# Global engine instance used by backend.router
llm_engine = LLMEngine()
