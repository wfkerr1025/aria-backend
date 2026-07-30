# backend/llm/providers/openai_wrapper.py

from __future__ import annotations
from typing import Dict, Any
import os
import openai

from backend.llm.cloud_llm_base import CloudLLMClientBase

class OpenAIWrapper(CloudLLMClientBase):
    provider_name = "openai"

    def __init__(self, model: str):
        self.model = model
        self.api_key = os.getenv("OPENAI_API_KEY", "")
        self.client = openai
        self.client.api_key = self.api_key

    def is_available(self) -> bool:
        return bool(self.api_key)

    def generate(self, prompt: str, context: Dict[str, Any] | None = None) -> Any:
        return self.client.ChatCompletion.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are ARIA Lite's co-developer."},
                {"role": "user", "content": prompt},
            ],
        )

    def normalize_response(self, raw: Any) -> str:
        return raw["choices"][0]["message"]["content"]

    def handle_error(self, error: Exception) -> Exception:
        return RuntimeError(f"OpenAI error: {error}")
