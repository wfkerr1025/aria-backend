# backend/core/model_metadata.py

from typing import Optional

from logger import get_logger

logger = get_logger(__name__)


# Bytes per parameter by quantization type (approximate)
QUANT_BYTES = {
    "Q2_K": 0.5,
    "Q3_K": 0.75,
    "Q4_K": 1.0,
    "Q5_K": 1.25,
    "Q6_K": 1.5,
}

# Quantization "difficulty" multiplier — how much heavier a quant format
# is to dequantize/compute per token relative to the Q4_K_M baseline,
# independent of its bytes-per-param memory footprint above. Lower-bit
# formats (Q2_K/Q3_K_M) unpack cheaper but recompute more per weight;
# higher-bit formats (Q5_K_M/Q6_K) cost more compute per token even
# though the memory-bandwidth side is already captured by QUANT_BYTES.
# Applied as a divisor against estimated tokens/sec in
# performance_estimator.py: heavier quant → lower throughput.
QUANT_DIFFICULTY_MULTIPLIER = {
    "Q2_K": 0.6,
    "Q3_K_M": 0.8,
    "Q4_K_M": 1.0,
    "Q5_K_M": 1.3,
    "Q6_K": 1.6,
}

_DEFAULT_QUANT_DIFFICULTY = QUANT_DIFFICULTY_MULTIPLIER["Q4_K_M"]


def get_model_params(model_cfg: dict) -> int:
    """
    Return approximate parameter count for the model.
    Prefer explicit config; fall back to id-based heuristics.
    """
    logger.debug(f"get_model_params() called for model_id={model_cfg.get('id')}")

    if "params" in model_cfg:
        params = int(model_cfg["params"])
        logger.debug(f"Explicit params found → {params}")
        return params

    model_id = model_cfg.get("id", "").lower()
    logger.debug(f"No explicit params; inferring from model_id='{model_id}'")

    if "7b" in model_id:
        logger.debug("Inferred params → 7B")
        return 7_000_000_000
    if "8b" in model_id:
        logger.debug("Inferred params → 8B")
        return 8_000_000_000
    if "12b" in model_id:
        logger.debug("Inferred params → 12B")
        return 12_000_000_000
    if "13b" in model_id:
        logger.debug("Inferred params → 13B")
        return 13_000_000_000
    if "14b" in model_id:
        logger.debug("Inferred params → 14B")
        return 14_000_000_000
    if "30b" in model_id:
        logger.debug("Inferred params → 30B")
        return 30_000_000_000
    if "34b" in model_id:
        logger.debug("Inferred params → 34B")
        return 34_000_000_000
    if "70b" in model_id:
        logger.debug("Inferred params → 70B")
        return 70_000_000_000
    if "72b" in model_id:
        logger.debug("Inferred params → 72B")
        return 72_000_000_000
    if "405b" in model_id:
        logger.debug("Inferred params → 405B")
        return 405_000_000_000

    logger.debug("Fallback params → 7B")
    return 7_000_000_000


def get_quant_bytes(model_cfg: dict) -> float:
    """
    Return bytes per parameter based on quantization.
    """
    logger.debug(f"get_quant_bytes() called for model_id={model_cfg.get('id')}")

    quant = model_cfg.get("quant", "").upper()
    logger.debug(f"Quantization from config → '{quant}'")

    if quant in QUANT_BYTES:
        bytes_per_param = QUANT_BYTES[quant]
        logger.debug(f"Matched quant → {bytes_per_param} bytes/param")
        return bytes_per_param

    filename = model_cfg.get("defaultFilename", "").upper()
    logger.debug(f"Trying filename inference → '{filename}'")

    for key in QUANT_BYTES:
        if key in filename:
            bytes_per_param = QUANT_BYTES[key]
            logger.debug(f"Inferred quant from filename → {bytes_per_param} bytes/param")
            return bytes_per_param

    logger.debug("Fallback quant → Q5_K (1.25 bytes/param)")
    return QUANT_BYTES["Q5_K"]


def get_quant_difficulty(model_cfg: dict) -> float:
    """
    Return the compute-difficulty multiplier for the model's
    quantization (see QUANT_DIFFICULTY_MULTIPLIER above). Tries an exact
    match against the config's "quant" field first (real models.json
    entries use the full "Q5_K_M"-style name, unlike QUANT_BYTES's bare
    "Q5_K" keys above), then falls back to filename substring matching,
    then defaults to the Q4_K_M multiplier (1.0x — a neutral baseline).
    """
    logger.debug(f"get_quant_difficulty() called for model_id={model_cfg.get('id')}")

    quant = (model_cfg.get("quant") or "").upper()
    if quant in QUANT_DIFFICULTY_MULTIPLIER:
        multiplier = QUANT_DIFFICULTY_MULTIPLIER[quant]
        logger.debug(f"Matched quant → {multiplier}x difficulty")
        return multiplier

    filename = (model_cfg.get("defaultFilename") or "").upper()
    for key in sorted(QUANT_DIFFICULTY_MULTIPLIER, key=len, reverse=True):
        if key in filename or key in quant:
            multiplier = QUANT_DIFFICULTY_MULTIPLIER[key]
            logger.debug(f"Inferred quant difficulty from filename/quant → {multiplier}x")
            return multiplier

    logger.debug(f"Fallback quant difficulty → {_DEFAULT_QUANT_DIFFICULTY}x (Q4_K_M baseline)")
    return _DEFAULT_QUANT_DIFFICULTY
