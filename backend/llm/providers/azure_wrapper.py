from __future__ import annotations
from typing import Any
import os
import requests

from logger import get_logger

logger = get_logger(__name__)


class Provider:
    """
    Azure OpenAI cloud provider — see backend/llm/providers/openai_wrapper.py
    for why this is `class Provider` with a no-arg constructor and
    `.run(request)` rather than the old `AzureWrapper(CloudLLMClientBase)`
    interface (which provider_registry.py's loader could never pick up).
    """

    provider_name = "azure"

    def __init__(self):
        logger.debug("Initializing Azure Provider")
        self.deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")
        self.api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
        self.api_key = os.getenv("AZURE_OPENAI_KEY", "")
        self.endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "")
        logger.debug("Azure Provider initialized")

    def is_available(self) -> bool:
        available = bool(self.api_key) and bool(self.endpoint) and bool(self.deployment)
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
        logger.debug(f"run() called → deployment={self.deployment}")

        if not self.is_available():
            logger.debug("Azure client unavailable → missing API key, endpoint, or deployment")
            raise RuntimeError("Azure OpenAI client not available or misconfigured")

        url = (
            f"{self.endpoint}/openai/deployments/"
            f"{self.deployment}/chat/completions?api-version={self.api_version}"
        )
        payload = {
            "messages": self._build_messages(request),
            "temperature": getattr(request, "temperature", 0.7),
            "max_tokens": getattr(request, "max_tokens", 512),
        }
        headers = {"Content-Type": "application/json", "api-key": self.api_key}

        try:
            logger.debug(f"Sending Azure request → deployment={self.deployment}, key_present={bool(self.api_key)}")
            resp = requests.post(url, json=payload, headers=headers, timeout=30)
        except Exception as e:
            logger.exception(f"Azure HTTP error → {e}")
            raise self.handle_error(e)

        if resp.status_code != 200:
            logger.warning(f"Azure error response → {resp.text}")
            raise self.handle_error(RuntimeError(resp.text))

        logger.info("Azure request succeeded")
        return self.normalize_response(resp.json())

    def normalize_response(self, raw: Any) -> str:
        logger.debug("normalize_response() called")
        try:
            return raw["choices"][0]["message"]["content"]
        except Exception:
            try:
                return raw["choices"][0]["delta"]["content"]
            except Exception:
                logger.debug("normalize_response() failed → returning empty string")
                return ""

    def handle_error(self, error: Exception) -> Exception:
        logger.debug(f"handle_error() → {error}")
        return RuntimeError(f"Azure error: {error}")
