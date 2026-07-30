# backend/llm/provider_health.py

from __future__ import annotations
from typing import Dict, Any
import time


class ProviderHealthMonitor:
    """
    Lightweight, event-driven health monitor for LLM providers.
    Tracks a rolling health score per provider based on successes/failures.
    """

    def __init__(self):
        # health_scores: 0.0 (dead) → 1.0 (perfect)
        self.health_scores: Dict[str, float] = {}
        self.last_status: Dict[str, str] = {}
        self.decay = 0.8  # weight for historical health

    def _init_provider(self, name: str) -> None:
        if name not in self.health_scores:
            self.health_scores[name] = 1.0
            self.last_status[name] = "unknown"

    def update_success(self, name: str, latency: float) -> None:
        """
        Called when a provider successfully returns a response.
        Latency can be used to slightly penalize slow providers.
        """
        self._init_provider(name)

        # Base success health
        current = 1.0

        # Penalize very slow responses
        if latency > 5.0:
            current = 0.7
        elif latency > 10.0:
            current = 0.4

        old = self.health_scores[name]
        new = (old * self.decay) + (current * (1.0 - self.decay))
        self.health_scores[name] = max(0.0, min(new, 1.0))
        self.last_status[name] = "ok"

    def update_failure(self, name: str) -> None:
        """
        Called when a provider fails (exception, HTTP error, etc.).
        """
        self._init_provider(name)

        current = 0.0
        old = self.health_scores[name]
        new = (old * self.decay) + (current * (1.0 - self.decay))
        self.health_scores[name] = max(0.0, min(new, 1.0))
        self.last_status[name] = "error"

    def get_health(self, name: str) -> float:
        self._init_provider(name)
        return self.health_scores[name]

    def snapshot(self) -> Dict[str, Any]:
        return {
            "health_scores": dict(self.health_scores),
            "last_status": dict(self.last_status),
        }


# Global monitor instance
provider_health_monitor = ProviderHealthMonitor()
