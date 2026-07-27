# backend/llm/local_client.py

from __future__ import annotations
from typing import Dict, Any, Optional
import requests
import json
from backend.llm.llm_client_base import LLMClientBase


class LocalLLMClient(LLMClientBase):
    """
    Strong Local LLM client for ARIA Lite.
    Uses LM Studio's local inference server.

    Model: mistral-nemo-12b-instruct-2407
    Endpoint: http://127.0.0.1:1234/v1/chat/completions
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:1234",
        model: str = "mistral-nemo-12b-instruct-2407",
        timeout: int = 60,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

        # LM Studio uses OpenAI-compatible API routes
        self.endpoint = f"{self.base_url}/v1/chat/completions"

    def generate(self, prompt: str, context: Dict[str, Any] | None = None) -> str:
        """
        Sends a chat completion request to LM Studio.
        Returns the generated text or raises an exception.
        """

        messages = [
            {
                "role": "system",
                "content": "You are ARIA Lite's local co-developer. Be helpful, concise, and technically accurate.",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ]

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 512,
            "stream": False,
        }

        try:
            response = requests.post(
                self.endpoint,
                json=payload,
                timeout=self.timeout,
            )
        except Exception as e:
            raise RuntimeError(f"Local LLM request failed: {e}")

        if response.status_code != 200:
            raise RuntimeError(
                f"Local LLM returned HTTP {response.status_code}: {response.text}"
            )

        try:
            data = response.json()
        except Exception:
            raise RuntimeError(f"Local LLM returned invalid JSON: {response.text}")

        try:
            return data["choices"][0]["message"]["content"]
        except Exception:
            raise RuntimeError(f"Local LLM response missing content: {data}")
