# backend/llm/providers/azure_wrapper.py

from __future__ import annotations
from typing import Dict, Any
import os
import requests

from backend.llm.cloud_llm_base import CloudLLMClientBase

class AzureWrapper(CloudLLMClientBase):
    provider_name = "azure"

    def __init__(self, deployment: str, api_version: str = "2024-02-01"):
        self.deployment = deployment
        self.api_version = api_version
        self.api_key = os.getenv("AZURE_OPENAI_KEY", "")
        self.endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "")

    def is_available(self) -> bool:
        return bool(self.api_key) and bool(self.endpoint)

    def generate(self, prompt: str, context: Dict[str, Any] | None = None) -> Any:
        url = f"{self.endpoint}/openai/deployments/{self.deployment}/chat/completions?api-version={self.api_version}"

        payload = {
            "messages": [
                {"role": "system", "content": "You are ARIA Lite's co-developer."},
                {"role": "user", "content": prompt},
            ]
        }

        headers = {
            "Content-Type": "application/json",
            "api-key": self.api_key,
        }

        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"Azure error: {resp.text}")

        return resp.json()

    def normalize_response(self, raw: Any) -> str:
        return raw["choices"][0]["message"]["content"]

    def handle_error(self, error: Exception) -> Exception:
        return RuntimeError(f"Azure error: {error}")
