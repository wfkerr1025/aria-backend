from __future__ import annotations
from typing import Dict, Any, Optional
import os
import time
import json

from backend.llm.intent_detector import IntentDetector
from backend.llm.provider_registry import PROVIDERS
from backend.llm.provider_selector import choose_provider
from backend.llm.local_client import LocalLLMClient
from backend.llm.constants import ARIA_TOOL_USE_SYSTEM_PROMPT
from backend.llm.intent_scorer import IntentScorer
from backend.llm.provider_health import provider_health_monitor
from backend.llm.tool_router import tool_router


class LLMClientBase:
    def is_available(self) -> bool:
        return True

    def generate(self, prompt: str, context: Dict[str, Any] | None = None) -> str:
        raise NotImplementedError


class OpenAIClient(LLMClientBase):
    def __init__(self, model: str):
        self.model = model
        try:
            import openai
            self._openai = openai
            self._openai.api_key = os.getenv("OPENAI_API_KEY", "")
        except Exception:
            self._openai = None

    def is_available(self) -> bool:
        return self._openai is not None and bool(self._openai.api_key)

    def generate(self, prompt: str, context: Dict[str, Any] | None = None) -> str:
        if not self.is_available():
            raise RuntimeError("OpenAI client not available or API key missing")

        resp = self._openai.ChatCompletion.create(
            model=self.model,
            messages=[
                {"role": "system", "content": ARIA_TOOL_USE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        return resp["choices"][0]["message"]["content"]


class LLMEngine:
    def __init__(self):
        self.intent_detector = IntentDetector()
        self.provider_clients: Dict[str, LLMClientBase] = {}

        for name, meta in PROVIDERS.items():
            if not meta.get("installed", False):
                continue

            if name == "local":
                self.provider_clients[name] = LocalLLMClient()
            elif name == "openai":
                self.provider_clients[name] = OpenAIClient(
                    model=meta.get("model", "gpt-4o")
                )

    def _get_or_create_client(self, provider_name: str) -> Optional[LLMClientBase]:
        client = self.provider_clients.get(provider_name)
        if client:
            return client

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
        print("LLM ENGINE RECEIVED:", prompt, context)

        context = context or {}

        intent = context.get("intent") or self.intent_detector.detect(prompt)
        complexity = IntentScorer.score(prompt)
        context["complexity_score"] = complexity

        provider_meta = choose_provider(prompt, intent, self)
        if provider_meta is None:
            return {
                "status": "error",
                "success": False,
                "provider": None,
                "model": None,
                "intent": intent,
                "content": "",
                "detail": "No available providers",
                "complexity": complexity,
            }

        name = provider_meta["name"]
        model = provider_meta.get("model", "default")

        client = self._get_or_create_client(name)
        if client is None or not client.is_available():
            provider_health_monitor.update_failure(name)
            return {
                "status": "error",
                "success": False,
                "provider": name,
                "model": model,
                "intent": intent,
                "content": "",
                "detail": f"Provider '{name}' unavailable",
                "complexity": complexity,
            }

        start = time.time()
        try:
            text = client.generate(prompt=prompt, context=context)
            print("RAW LLM OUTPUT:", repr(text))
            latency = time.time() - start
            provider_health_monitor.update_success(name, latency)

            # Try to parse JSON; if it’s a dict, let tool_router decide
            parsed: Any = None
            try:
                candidate = json.loads(text)
                if isinstance(candidate, dict):
                    parsed = candidate
            except Exception:
                parsed = None

            routed = tool_router.route(parsed if parsed is not None else text)

            return {
                "status": "ok",
                "success": True,
                "provider": name,
                "model": model,
                "intent": intent,
                "content": routed,
                "detail": None,
                "complexity": complexity,
                "health": provider_health_monitor.get_health(name),
            }

        except Exception as e:
            provider_health_monitor.update_failure(name)
            return {
                "status": "error",
                "success": False,
                "provider": name,
                "model": model,
                "intent": intent,
                "content": "",
                "detail": str(e),
                "complexity": complexity,
                "health": provider_health_monitor.get_health(name),
            }


llm_engine = LLMEngine()
