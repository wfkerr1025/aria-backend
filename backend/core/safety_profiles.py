# backend/core/safety_profiles.py

import os
from .resource_monitor import ResourceSnapshot
from .model_metadata import get_model_params, get_quant_bytes

from logger import get_logger

logger = get_logger(__name__)


class PerformanceProfile:
    def __init__(self, name, n_threads, n_gpu_layers, max_ctx):
        logger.debug(
            f"PerformanceProfile created → name={name}, "
            f"threads={n_threads}, gpu_layers={n_gpu_layers}, ctx={max_ctx}"
        )
        self.name = name
        self.n_threads = n_threads
        self.n_gpu_layers = n_gpu_layers
        self.max_ctx = max_ctx


# -----------------------------------------------------------
# INTERNAL HELPERS
# -----------------------------------------------------------

def _adaptive_threads(snapshot: ResourceSnapshot) -> int:
    logger.debug("Calculating adaptive threads")

    cores = os.cpu_count() or 4
    logger.debug(f"Detected CPU cores → {cores}")

    # -------------------------------------------------------
    # CPU-only systems (no VRAM)
    # -------------------------------------------------------
    if snapshot.vram_total_gb <= 0:
        if snapshot.unity_running:
            # Unity + ARIA → keep extremely light
            threads = max(2, min(cores // 4, 4))
            logger.debug(f"CPU-only + Unity → threads={threads}")
            return threads

        # Solo ARIA on CPU-only → realistic threading
        threads = max(2, min(cores // 2, 8))
        logger.debug(f"CPU-only Solo ARIA → threads={threads}")
        return threads

    # -------------------------------------------------------
    # GPU available (normal behavior)
    # -------------------------------------------------------
    if snapshot.unity_running:
        threads = max(2, min(cores // 4, 6))
        logger.debug(f"Unity running → threads={threads}")
        return threads

    half_cores = cores // 2
    leave_two_free = cores - 2
    threads = max(2, min(half_cores, leave_two_free))

    logger.debug(f"Solo ARIA → threads={threads}")
    return threads


def _adaptive_gpu_layers(snapshot: ResourceSnapshot, model_cfg: dict) -> int:
    logger.debug("Calculating adaptive GPU layers")

    total_vram = snapshot.vram_total_gb
    used_vram = snapshot.vram_used_gb
    logger.debug(f"VRAM → used={used_vram:.2f}GB, total={total_vram:.2f}GB")

    if total_vram <= 0:
        logger.debug("No GPU detected → 0 layers")
        return 0

    default_layers = int(model_cfg.get("defaultGpuLayers", 24))
    logger.debug(f"Default GPU layers → {default_layers}")

    headroom_gb = 2.0 if snapshot.unity_running else 1.0
    available_vram_gb = max(0.0, total_vram - used_vram - headroom_gb)
    logger.debug(f"Available VRAM after headroom → {available_vram_gb:.2f}GB")

    params = get_model_params(model_cfg)
    bytes_per_param = get_quant_bytes(model_cfg)
    total_layers = 80

    model_bytes = params * bytes_per_param
    model_vram_gb = model_bytes / (1024 ** 3)
    vram_per_layer_gb = model_vram_gb / total_layers if total_layers > 0 else 0.05

    logger.debug(f"VRAM per layer → {vram_per_layer_gb:.4f}GB")

    max_layers_by_vram = (
        int(available_vram_gb / vram_per_layer_gb)
        if vram_per_layer_gb > 0 else default_layers
    )

    if snapshot.unity_running:
        layers = max(0, min(default_layers, max_layers_by_vram, 16))
        logger.debug(f"Unity running → GPU layers={layers}")
        return layers

    layers = max(0, min(default_layers, max_layers_by_vram, 48))
    logger.debug(f"Solo ARIA → GPU layers={layers}")
    return layers


def _adaptive_context(snapshot: ResourceSnapshot, model_cfg: dict) -> int:
    logger.debug("Calculating adaptive context size")

    ram_total = snapshot.ram_total_gb
    base_ctx = int(model_cfg.get("maxContext", 8192))

    logger.debug(f"RAM total → {ram_total:.2f}GB, base_ctx={base_ctx}")

    if ram_total <= 8:
        ctx = min(base_ctx, 4096)
        logger.debug(f"Low RAM → ctx={ctx}")
        return ctx

    if ram_total <= 16:
        ctx = min(base_ctx, 8192)
        logger.debug(f"Mid RAM → ctx={ctx}")
        return ctx

    logger.debug(f"High RAM → ctx={base_ctx}")
    return base_ctx


# -----------------------------------------------------------
# PUBLIC PROFILE SELECTOR
# -----------------------------------------------------------

def select_profile(snapshot: ResourceSnapshot, model_cfg: dict) -> PerformanceProfile:
    logger.debug(
        f"select_profile() called → model_id={model_cfg.get('id')}, "
        f"RAM={snapshot.ram_total_gb:.2f}GB, VRAM={snapshot.vram_total_gb:.2f}GB"
    )

    n_threads = _adaptive_threads(snapshot)
    n_gpu_layers = _adaptive_gpu_layers(snapshot, model_cfg)
    max_ctx = _adaptive_context(snapshot, model_cfg)

    if snapshot.unity_running or snapshot.ram_total_gb < 12 or snapshot.vram_total_gb < 6:
        profile_name = "eco"
    elif snapshot.ram_total_gb < 24 or snapshot.vram_total_gb < 10:
        profile_name = "balanced"
    else:
        profile_name = "performance"

    logger.debug(
        f"Profile selected → {profile_name} "
        f"(threads={n_threads}, gpu_layers={n_gpu_layers}, ctx={max_ctx})"
    )

    return PerformanceProfile(
        name=profile_name,
        n_threads=n_threads,
        n_gpu_layers=n_gpu_layers,
        max_ctx=max_ctx,
    )
