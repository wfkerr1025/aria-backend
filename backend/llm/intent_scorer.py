# backend/llm/intent_scorer.py

from __future__ import annotations
import re
from typing import Any


class IntentScorer:
    """
    Heuristic task-complexity scorer for ARIA Lite.

    Returns an integer score from 0–10 based on:
    - prompt length
    - number of instructions
    - multi-step language
    - presence of code blocks
    - high-complexity keywords (architecture, refactor, etc.)
    """

    @staticmethod
    def score(prompt: str) -> int:
        if not prompt:
            return 0

        score = 0
        p = prompt.lower()

        # Length scoring
        length = len(prompt)
        if length > 200:
            score += 2
        if length > 500:
            score += 3
        if length > 1000:
            score += 4

        # Instruction count
        instructions = re.findall(
            r"\b(do|make|build|fix|refactor|rewrite|optimize|generate|create|implement)\b",
            p,
        )
        score += min(len(instructions), 4)

        # Multi-step detection
        if re.search(r"\b(step|first|second|third|finally|then)\b", p):
            score += 2

        # Code block detection
        if "```" in prompt:
            score += 3

        # High-complexity keywords
        high_complexity_keywords = [
            "architecture",
            "refactor",
            "redesign",
            "optimize",
            "multi-file",
            "project-wide",
            "dependency",
            "module",
            "subsystem",
            "routing",
            "orchestration",
        ]
        if any(k in p for k in high_complexity_keywords):
            score += 3

        return min(score, 10)
