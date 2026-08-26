# backend/core/pc_capability_tier.py

"""
Universal Tier 0-6 PC-capability rating for the model-installation
safety system.

backend.core.hardware_detector already classifies CPU/RAM/VRAM/storage
INDIVIDUALLY into their own tiers (Low/Medium/High/Ultra, or a GFLOPS-
based AVX2 tier) -- none of those collapse into a single "what size
model can this machine actually run" number a model-install screen can
gate on directly. This module is that collapse, and it deliberately
does not invent its own threshold numbers where an approved one already
exists: Tier 2 upward reuses backend.core.model_size_requirements'
per-size-class minRamGB/minCpuCores/minCpuFeatures/recVramGB exactly
(via get_requirements_for_size_tier()), so a PC tier and the
compatibility-checker verdict for the same model can never quietly
disagree about what "enough RAM for a 12B model" means.

Tiers 0 and 1 (0.5B-only, then +Phi-3-mini) are the one place with no
existing table to borrow from: model_size_requirements.py's smallest
bucket ("7B", params<=8B) is shared by Qwen-0.5B and Phi-3-mini too
(both land under that same param-count bucket), but both are usable
well below that bucket's 8GB/4-core minimum in practice. Those two
tiers use their own small, clearly-labeled ad hoc thresholds instead of
borrowing numbers that would misrepresent what those specific models
actually need.

Tiers 4-6 additionally require a real GPU (vram_gb > 0) even though
model_size_requirements.py's own 20B/30B/70B/140B minVramGB is 0 (i.e.
technically CPU-viable, just impractically slow) -- this matches the
spec's own tier table, which parenthesizes "(GPU)" starting at Tier 4.
A machine that meets those size classes' RAM/CPU minimums but has no
GPU stays capped at Tier 3, and pc_capability_tier's own "why" is
carried in each tier's `enabled_size_classes` rather than silently
recommending an impractically slow local run.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import hardware_detector
from .model_size_requirements import get_requirements_for_size_tier

TIER_LABELS = {
    0: "Minimal",
    1: "Basic",
    2: "Standard",
    3: "Capable",
    4: "Advanced (GPU)",
    5: "High-End (GPU)",
    6: "Extreme (GPU)",
}

TIER1_MIN_RAM_GB = 4.0
TIER1_MIN_CORES = 2


def _meets(ram_gb: float, cores: int, has_avx2: bool, req: dict) -> bool:
    min_ram = req.get("minRamGB", 0)
    min_cores = req.get("minCpuCores", 1)
    needs_avx2 = "AVX2" in (req.get("minCpuFeatures") or [])
    if ram_gb < min_ram:
        return False
    if cores < min_cores:
        return False
    if needs_avx2 and not has_avx2:
        return False
    return True


def get_pc_tier(hw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Collapse hardware_detector.py's separate component tiers into ONE
    overall Tier 0-6 PC-capability rating, per the model-install-safety
    spec's tier table.

    `hw` accepts hardware_detector.detect_all()'s exact shape (for
    testability with literal, named hardware specs -- see
    backend/tests/pc_capability_tier_tests.py); omit it to detect the
    live machine.

    Tiers are climbed in strict order (each requires the previous one),
    matching how the spec's ladder is written ("Tier N: + <size
    class>") -- a machine never skips a tier even if it would otherwise
    qualify for a higher one on paper (e.g. huge RAM but only 1 core
    stays capped where the core-count minimum actually caps it).
    """
    hw = hw or hardware_detector.detect_all()
    cpu = hw["cpu"]
    ram = hw["ram"]
    gpu = hw.get("gpu")

    ram_gb = ram["total_gb"]
    cores = cpu["physical_cores"]
    has_avx2 = bool(cpu["avx2"] or cpu["avx512"])
    vram_gb = gpu["vram_total_gb"] if gpu else 0.0
    has_gpu = vram_gb > 0

    req_7b = get_requirements_for_size_tier("7B") or {}
    req_12b = get_requirements_for_size_tier("12B") or {}
    req_20b = get_requirements_for_size_tier("20B") or {}
    req_30b = get_requirements_for_size_tier("30B") or {}
    req_70b = get_requirements_for_size_tier("70B") or {}
    req_140b = get_requirements_for_size_tier("140B") or {}

    tier = 0
    enabled: List[str] = ["0.5B"]

    if ram_gb >= TIER1_MIN_RAM_GB and cores >= TIER1_MIN_CORES:
        tier = 1
        enabled.append("Phi-3")

    if tier >= 1 and _meets(ram_gb, cores, has_avx2, req_7b):
        tier = 2
        enabled.append("7B")

    if tier >= 2 and _meets(ram_gb, cores, has_avx2, req_12b):
        tier = 3
        enabled.append("12B")

    if (
        tier >= 3 and has_gpu
        and _meets(ram_gb, cores, has_avx2, req_20b)
        and _meets(ram_gb, cores, has_avx2, req_30b)
    ):
        tier = 4
        enabled += ["20B", "30B"]

    if (
        tier >= 4 and has_gpu
        and vram_gb >= req_70b.get("recVramGB", 0)
        and _meets(ram_gb, cores, has_avx2, req_70b)
    ):
        tier = 5
        enabled.append("70B")

    if (
        tier >= 5 and has_gpu
        and vram_gb >= req_140b.get("recVramGB", 0)
        and _meets(ram_gb, cores, has_avx2, req_140b)
    ):
        tier = 6
        enabled += ["120B", "140B"]

    return {
        "tier": tier,
        "label": TIER_LABELS[tier],
        "enabled_size_classes": enabled,
        "ram_gb": round(ram_gb, 1),
        "cores": cores,
        "avx2": has_avx2,
        "vram_gb": round(vram_gb, 1),
    }
