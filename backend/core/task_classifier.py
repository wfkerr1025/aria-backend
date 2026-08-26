from __future__ import annotations
from typing import Literal

TaskComplexity = Literal["low", "medium", "high"]
TaskType = Literal["code", "reasoning", "creative", "ops", "unknown"]


from backend.logger import log as unified_log

from logger import get_logger

logger = get_logger(__name__)


def classify_task_complexity(prompt: str) -> TaskComplexity:
    logger.debug(f"classify_task_complexity() called → prompt_len={len(prompt)}")

    t = prompt.strip()
    if not t:
        logger.debug("Empty prompt → complexity=low")
        return "low"

    length = len(t)
    lines = t.count("\n")

    if length < 300 and lines < 5:
        complexity = "low"
    elif length < 1200 and lines < 20:
        complexity = "medium"
    else:
        complexity = "high"

    logger.debug(f"Complexity={complexity}")
    unified_log("task_classifier", "DEBUG", f"Task complexity: {complexity}", {
        "prompt_len": length, "complexity": complexity,
    })
    return complexity


# Task-type -> preferred cloud provider, when the caller hasn't manually
# pinned one. Anthropic tends to be the stronger choice for
# reasoning-heavy prompts, OpenAI for code/structured tasks. Shared by
# backend.core.auto_selector.AutoSelector (Automatic Model Routing's cloud branch) and
# backend.core.provider_router.ProviderRouter (pure Cloud Mode's branch,
# when no provider was ever manually chosen) so both apply the exact
# same preference rather than two independently-tuned copies that could
# drift apart.
_TASK_TYPE_CLOUD_PREFERENCE = {"reasoning": "anthropic", "code": "openai"}


def preferred_cloud_provider_for_task(task_type: TaskType) -> str | None:
    return _TASK_TYPE_CLOUD_PREFERENCE.get(task_type)


def classify_task_type(prompt: str) -> TaskType:
    logger.debug("classify_task_type() called")

    p = prompt.lower()

    if any(k in p for k in ["code", "python", "function", "class", "module", "script"]):
        task_type = "code"
    elif any(k in p for k in ["explain", "analyze", "reason", "debug", "trace", "root cause"]):
        task_type = "reasoning"
    elif any(k in p for k in ["story", "poem", "lyrics", "chapter", "character", "worldbuild"]):
        task_type = "creative"
    elif any(k in p for k in ["server", "uptime", "metrics", "health", "status", "runtime"]):
        task_type = "ops"
    else:
        task_type = "unknown"

    logger.debug(f"TaskType={task_type}")
    unified_log("task_classifier", "DEBUG", f"Task type: {task_type}", {"task_type": task_type})
    return task_type
