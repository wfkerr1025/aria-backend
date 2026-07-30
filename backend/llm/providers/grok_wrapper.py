# backend/llm/providers/grok_wrapper.py

from __future__ import annotations
from typing import Dict, Any
import os
import requests

from backend.llm.cloud_llm_base import CloudLLMClientBase

class GrokWrapper(CloudLLMClientBase):
    provider_name = "grok"

    def __init__(self, model: str = "grok-latest"):
        self.model = model
        self.api_key = os.getenv("XAI_API_KEY", "")

    def is_available(self) -> bool:
        return bool(self.api_key)

    def generate(self, prompt: str, context: Dict[str, Any] | None = None) -> Any:
        url = "https://api.x.ai/v1/chat/completions"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are ARIA Lite's co-developer."},
                {"role": "user", "content": prompt},
            ]
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"Grok error: {resp.text}")

        return resp.json()

    def normalize_response(self, raw: Any) -> str:
        return raw["choices"][0]["message"]["content"]

    def handle_error(self, error: Exception) -> Exception:
        return RuntimeError(f"Grok error: {error}")
