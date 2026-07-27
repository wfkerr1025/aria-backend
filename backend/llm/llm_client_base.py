# backend/llm/llm_client_base.py

from __future__ import annotations
from typing import Dict, Any

class LLMClientBase:
    """
    Base interface for all LLM provider clients.
    """

    def generate(self, prompt: str, context: Dict[str, Any] | None = None) -> str:
        raise NotImplementedError
