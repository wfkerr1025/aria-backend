from __future__ import annotations
from backend.core.task_classifier import classify_task_complexity, classify_task_type

from backend.logger import log as unified_log

from logger import get_logger

logger = get_logger(__name__)


DEFAULT_SUCCESS_THRESHOLD = 75  # percent


def predict_success(prompt: str, local_capabilities: dict | None = None) -> int:
    """
    Predict how likely the local model is to succeed.
    Lightweight heuristic tuned for ARIA Lite.
    """

    logger.debug(
        f"predict_success() called → prompt_len={len(prompt)}, "
        f"local_caps={bool(local_capabilities)}"
    )

    complexity = classify_task_complexity(prompt)
    task_type = classify_task_type(prompt)

    logger.debug(f"Complexity={complexity}, TaskType={task_type}")

    success = 100

    # ---------------------------------------------------------
    # Complexity penalties
    # ---------------------------------------------------------
    if complexity == "medium":
        success -= 15
        logger.debug("Applied complexity penalty → medium (-15)")
    elif complexity == "high":
        success -= 35
        logger.debug("Applied complexity penalty → high (-35)")

    # ---------------------------------------------------------
    # Task-type penalties
    # ---------------------------------------------------------
    if task_type == "reasoning":
        success -= 20
        logger.debug("Applied task penalty → reasoning (-20)")
    elif task_type == "code":
        success -= 10
        logger.debug("Applied task penalty → code (-10)")
    elif task_type == "ops":
        success -= 15
        logger.debug("Applied task penalty → ops (-15)")
    elif task_type == "creative":
        success -= 5
        logger.debug("Applied task penalty → creative (-5)")

    # ---------------------------------------------------------
    # Length penalties
    # ---------------------------------------------------------
    length = len(prompt)
    if length > 500:
        success -= 10
        logger.debug("Applied length penalty → >500 chars (-10)")
    if length > 1500:
        success -= 15
        logger.debug("Applied length penalty → >1500 chars (-15)")

    # ---------------------------------------------------------
    # Local capability bonuses
    # ---------------------------------------------------------
    if local_capabilities:
        rb = int(local_capabilities.get("reasoning_bonus", 0))
        cb = int(local_capabilities.get("coding_bonus", 0))
        db = int(local_capabilities.get("debugging_bonus", 0))

        success += rb + cb + db
        logger.debug(f"Applied capability bonuses → +{rb}+{cb}+{db}")

    final_score = max(0, min(success, 100))
    logger.debug(f"Final success score → {final_score}")
    unified_log("success_predictor", "DEBUG", f"Predicted success score: {final_score}", {
        "score": final_score, "complexity": complexity, "task_type": task_type,
    })

    return final_score


def should_use_local(prompt: str, local_capabilities: dict | None = None,
                     threshold: int = DEFAULT_SUCCESS_THRESHOLD) -> bool:
    score = predict_success(prompt, local_capabilities)
    decision = score >= threshold

    logger.debug(
        f"should_use_local() → score={score}, threshold={threshold}, decision={decision}"
    )
    unified_log("success_predictor", "INFO", f"should_use_local: {decision}", {
        "score": score, "threshold": threshold, "decision": decision,
    })

    return decision
