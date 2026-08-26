# backend/core/model_discovery.py
#
# Model Discovery Engine: scans ~/.aria-lite/models for *.gguf files and
# infers metadata from filenames, so new model files dropped into that
# folder show up without hand-editing backend/config/models.json.
#
# Deliberately self-contained — no import of backend.core.model_registry
# (which imports *this* module to merge discovered models in; importing
# it back here would be circular). Nothing in this file talks to the
# registry, llama.cpp, or any other engine — it only reads the
# filesystem and returns plain data.

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from logger import get_logger

logger = get_logger(__name__)


def get_default_model_dir() -> Path:
    return Path.home() / ".aria-lite" / "models"


@dataclass
class DiscoveredModel:
    model_id: str
    filename: str
    path: str
    arch: str
    params_b: float
    quantization: str
    context_tokens: int
    size_bytes: int


# ============================================================
# FILENAME INFERENCE
# ============================================================

# Ordered so more specific patterns are checked before the generic ones
# they'd otherwise be swallowed by (e.g. "mistral-nemo" before "mistral").
_ARCH_PATTERNS: List[tuple] = [
    (r"mistral[-_\s]?nemo", "mistral-nemo"),
    (r"mistral", "mistral"),
    (r"phi[-_\s]?3", "phi3"),
    (r"phi[-_\s]?2", "phi2"),
    (r"qwen2\.5", "qwen2.5"),
    (r"qwen2", "qwen2"),
    (r"qwen", "qwen"),
    (r"llama[-_\s]?3", "llama3"),
    (r"llama[-_\s]?2", "llama2"),
    (r"gemma2", "gemma2"),
    (r"gemma", "gemma"),
]

# Filenames don't always spell out a param count ("Phi-3-mini-4k-instruct-
# q4.gguf" has no "3.8b" in it anywhere) — this is the fallback once
# regex extraction below comes up empty, keyed by the arch just detected.
_KNOWN_ARCH_PARAMS_B: Dict[str, float] = {
    "phi3": 3.8,
    "phi2": 2.7,
    "qwen2.5": 0.5,   # only used if the filename itself has no explicit size
    "gemma2": 9.0,
    "gemma": 7.0,
}

_PARAMS_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*[bB](?:illion)?(?:[-_.]|$)")
_QUANT_PATTERN = re.compile(r"(Q\d(?:_K)?(?:_[MSL])?|Q\d_\d|F16|F32)", re.IGNORECASE)
_CONTEXT_PATTERN = re.compile(r"(\d+)\s*[kK](?:[-_.]|$)")

DEFAULT_CONTEXT_TOKENS = 4096


def infer_arch(name: str) -> str:
    lower = name.lower()
    for pattern, arch in _ARCH_PATTERNS:
        if re.search(pattern, lower):
            return arch
    return "unknown"


def infer_params_b(name: str, arch: str) -> float:
    match = _PARAMS_PATTERN.search(name)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return _KNOWN_ARCH_PARAMS_B.get(arch, 7.0)


def infer_quantization(name: str) -> str:
    match = _QUANT_PATTERN.search(name)
    if match:
        return match.group(1).upper()
    return "unknown"


def infer_context_tokens(name: str) -> int:
    match = _CONTEXT_PATTERN.search(name)
    if match:
        try:
            return int(match.group(1)) * 1024
        except ValueError:
            pass
    return DEFAULT_CONTEXT_TOKENS


def _model_id_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    return stem.lower()


# ============================================================
# DISCOVERY
# ============================================================

def discover_models(models_dir: Optional[Path] = None) -> List[DiscoveredModel]:
    """
    Scan `models_dir` (default ~/.aria-lite/models) for *.gguf files and
    return a DiscoveredModel per file. Never raises on a missing/unreadable
    directory — an empty result plus a clear log line is the correct
    "fail gracefully" behavior here (backend/core/model_registry.py's
    caller decides what an empty discovery means for the running app).
    """
    target_dir = models_dir or get_default_model_dir()
    logger.info(f"discover_models() scanning {target_dir}")

    if not target_dir.exists() or not target_dir.is_dir():
        logger.warning(f"discover_models() — models directory not found: {target_dir}")
        return []

    discovered: List[DiscoveredModel] = []

    try:
        entries = sorted(target_dir.glob("*.gguf"))
    except OSError as e:
        logger.error(f"discover_models() — failed to list {target_dir}: {e}")
        return []

    for path in entries:
        try:
            filename = path.name
            arch = infer_arch(filename)
            model = DiscoveredModel(
                model_id=_model_id_from_filename(filename),
                filename=filename,
                path=str(path),
                arch=arch,
                params_b=infer_params_b(filename, arch),
                quantization=infer_quantization(filename),
                context_tokens=infer_context_tokens(filename),
                size_bytes=path.stat().st_size,
            )
            discovered.append(model)
            logger.debug(f"discover_models() found → {model}")
        except OSError as e:
            logger.warning(f"discover_models() — skipping unreadable file {path}: {e}")
            continue

    logger.info(f"discover_models() → {len(discovered)} model(s) found in {target_dir}")
    return discovered


def build_model_registry(models_dir: Optional[Path] = None) -> Dict[str, DiscoveredModel]:
    """Same scan as discover_models(), keyed by model_id for O(1) lookup."""
    return {m.model_id: m for m in discover_models(models_dir)}


# ============================================================
# REQUIREMENTS ESTIMATION
#
# backend/config/models.json's two hand-curated entries are the
# calibration points: 7B/Q4_K_M -> 8GB min / 16GB rec, 12B/Q5_K_M ->
# 16GB min / 32GB rec. This is a heuristic, not a measured figure per
# model — documented as such wherever it's surfaced (Models page,
# self-knowledge) rather than presented as precise.
# ============================================================
_QUANT_SCALE = {
    "Q2_K": 0.55, "Q3_K": 0.7, "Q3_K_S": 0.65, "Q3_K_M": 0.7, "Q3_K_L": 0.75,
    "Q4_0": 0.85, "Q4_K": 0.9, "Q4_K_S": 0.85, "Q4_K_M": 0.9,
    "Q5_0": 1.0, "Q5_K": 1.05, "Q5_K_S": 1.0, "Q5_K_M": 1.05,
    "Q6_K": 1.2, "Q8_0": 1.5, "F16": 2.5, "F32": 5.0,
}


def estimate_requirements(params_b: float, quantization: str) -> dict:
    scale = _QUANT_SCALE.get(quantization.upper(), 0.9)
    min_ram_gb = max(1, round(params_b * 1.15 * scale, 1))
    rec_ram_gb = max(2, round(params_b * 2.3 * scale, 1))

    if params_b <= 1:
        difficulty = "Light"
    elif params_b <= 5:
        difficulty = "Medium"
    elif params_b <= 10:
        difficulty = "Medium"
    else:
        difficulty = "Heavy"

    return {
        "minRamGB": min_ram_gb,
        "recRamGB": rec_ram_gb,
        "minCpuCores": 2 if params_b <= 1 else 4,
        "recCpuCores": 4 if params_b <= 1 else 8,
        "minCpuFeatures": ["AVX2"],
        "recCpuFeatures": ["AVX2"],
        "minVramGB": 0,
        "recVramGB": max(0, round(params_b * 0.8, 1)),
        "minSpeedTokSec": 2,
        "recSpeedTokSec": 6,
        "minContext": 2048,
        "recContext": 4096,
        "difficulty": difficulty,
        "notes": (
            f"Auto-discovered model — requirements are a heuristic estimate "
            f"based on {params_b}B parameters at {quantization}, not a "
            f"measured figure."
        ),
    }


def to_registry_entry(model: DiscoveredModel) -> dict:
    """
    Build a full model_cfg dict in the exact shape
    backend/core/model_registry.py's other entries use, so every existing
    consumer (compatibility_checker, safety_manager, ipc_router, the
    Models page) handles a discovered model identically to a hand-curated
    one — no special-casing needed anywhere downstream of the registry.
    """
    params = int(model.params_b * 1_000_000_000)
    return {
        "id": model.model_id,
        "name": f"{model.filename} (auto-discovered)",
        "provider": "local",
        "type": "gguf",
        "defaultFilename": model.filename,
        "path": model.path,
        "quant": model.quantization,
        "params": params,
        "maxContext": model.context_tokens,
        "defaultGpuLayers": 0,
        "arch": model.arch,
        "discovered": True,
        "requirements": estimate_requirements(model.params_b, model.quantization),
    }
