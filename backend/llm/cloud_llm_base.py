# backend/llm/cloud_llm_base.py

from __future__ import annotations
from typing import Dict, Any

class CloudLLMClientBase:
    """
    Unified interface for all cloud LLM providers.
    """

    provider_name: str = "unknown"

    def is_available(self) -> bool:
        raise NotImplementedError

    def generate(self, prompt: str, context: Dict[str, Any] | None = None) -> str:
        raise NotImplementedError

    def normalize_response(self, raw: Any) -> str:
        raise NotImplementedError

    def handle_error(self, error: Exception) -> Exception:
        raise NotImplementedError

    def generate_with_retry(self, prompt: str, context: Dict[str, Any] | None = None) -> str:
        """
        Unified retry strategy for all cloud providers.
        """
        try:
            return self.generate(prompt, context)
        except Exception as e:
            err = self.handle_error(e)
            raise err
