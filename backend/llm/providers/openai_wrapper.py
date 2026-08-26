from __future__ import annotations
from typing import Any
import os
from openai import OpenAI

from logger import get_logger

logger = get_logger(__name__)


class Provider:
    """
    OpenAI cloud provider.

    Standardized onto the same interface every other provider in this
    package uses (backend.llm.providers.provider_registry auto-loads any
    module exposing a no-arg-constructible `Provider` class implementing
    `.run(request)` / optionally `.stream(request, callback)`, where
    `request` is a backend.core.local_inference_engine.InferenceRequest).
    This used to be `class OpenAIWrapper(CloudLLMClientBase)` with a
    required `model` constructor arg and a `generate(prompt, context)`
    method — a different interface that provider_registry.py's loader
    silently skipped (no `Provider` class found) and that ProviderRouter/
    StreamingEngine could never have called anyway (they build an
    InferenceRequest, not a bare prompt string).

    No `.stream()` method is defined here on purpose: StreamingEngine
    already falls back to `.run()` + a single callback chunk for any
    provider without native streaming support, so there's nothing to
    duplicate — true SSE streaming can be added later as a `.stream()`
    override without touching that fallback path.
    """

    provider_name = "openai"

    def __init__(self):
        logger.debug("Initializing OpenAI Provider")
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.api_key = os.getenv("OPENAI_API_KEY", "")
        self.client = OpenAI(api_key=self.api_key)
        logger.debug("OpenAI Provider initialized")

    def is_available(self) -> bool:
        available = bool(self.api_key)
        logger.debug(f"is_available() → {available}")
        return available

    def _build_messages(self, request) -> list[dict]:
        messages = [
            {"role": getattr(m, "role", None) or m.get("role", "user"),
             "content": getattr(m, "content", None) or m.get("content", "")}
            for m in (request.messages or [])
        ]
        return messages or [{"role": "user", "content": ""}]

    def run(self, request) -> str:
        logger.debug(f"run() called → model={self.model}")

        if not self.is_available():
            logger.debug("OpenAI client unavailable → missing API key")
            raise RuntimeError("OpenAI client not available or API key missing")

        try:
            logger.debug(f"Sending OpenAI request → model={self.model}, key_present={bool(self.api_key)}")
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self._build_messages(request),
                temperature=getattr(request, "temperature", 0.7),
                max_tokens=getattr(request, "max_tokens", 512),
            )
            logger.info("OpenAI request succeeded")
            return self.normalize_response(response)
        except Exception as e:
            logger.exception(f"OpenAI request failed → {e}")
            raise self.handle_error(e)

    def normalize_response(self, raw: Any) -> str:
        logger.debug("normalize_response() called")
        try:
            return raw.choices[0].message.content
        except Exception:
            try:
                return raw.choices[0].delta.content
            except Exception:
                logger.debug("normalize_response() failed → returning empty string")
                return ""

    def handle_error(self, error: Exception) -> Exception:
        logger.debug(f"handle_error() → {error}")
        return RuntimeError(f"OpenAI error: {error}")
