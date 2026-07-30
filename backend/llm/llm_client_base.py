# backend/llm/llm_client_base.py

from __future__ import annotations
from typing import Dict, Any

class LLMClientBase:
    def is_available(self) -> bool:
        return True  # override in subclasses

    def generate(self, prompt: str, context: Dict[str, Any] | None = None) -> str:
        raise NotImplementedError
