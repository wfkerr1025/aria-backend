import os
import psutil

from .hardware_detector import detect_gpu

from logger import get_logger

logger = get_logger(__name__)


class ResourceSnapshot:
    def __init__(self, cpu_usage, ram_used_gb, ram_total_gb,
                 vram_used_gb, vram_total_gb, unity_running):
        logger.debug(
            f"ResourceSnapshot created → CPU={cpu_usage}%, "
            f"RAM={ram_used_gb:.2f}/{ram_total_gb:.2f}GB, "
            f"VRAM={vram_used_gb:.2f}/{vram_total_gb:.2f}GB, "
            f"UnityRunning={unity_running}"
        )

        self.cpu_usage = cpu_usage
        self.ram_used_gb = ram_used_gb
        self.ram_total_gb = ram_total_gb
        self.vram_used_gb = vram_used_gb
        self.vram_total_gb = vram_total_gb
        self.unity_running = unity_running

    @property
    def ram_used_pct(self):
        pct = (self.ram_used_gb / self.ram_total_gb) * 100 if self.ram_total_gb else 0
        logger.debug(f"RAM used pct → {pct:.2f}%")
        return pct

    @property
    def vram_used_pct(self):
        pct = (self.vram_used_gb / self.vram_total_gb) * 100 if self.vram_total_gb else 0
        logger.debug(f"VRAM used pct → {pct:.2f}%")
        return pct


def get_resource_snapshot() -> ResourceSnapshot:
    logger.debug("get_resource_snapshot() called")

    # CPU usage (overall)
    cpu_usage = psutil.cpu_percent(interval=0.2)
    logger.debug(f"CPU usage → {cpu_usage}%")

    # RAM usage
    vm = psutil.virtual_memory()
    ram_total_gb = vm.total / (1024 ** 3)
    ram_used_gb = (vm.total - vm.available) / (1024 ** 3)
    logger.debug(f"RAM usage → {ram_used_gb:.2f}/{ram_total_gb:.2f}GB")

    # VRAM usage — sourced from hardware_detector's NVML-based GPU
    # detection (see that module's docstring: the previous GPUtil-based
    # path here silently always returned 0/0 because GPUtil was never
    # actually an installed dependency, so any NVIDIA GPU present was
    # invisible to safety/compatibility checks).
    vram_total_gb = 0.0
    vram_used_gb = 0.0

    gpu = detect_gpu()
    if gpu is not None:
        vram_total_gb = gpu["vram_total_gb"]
        vram_used_gb = gpu["vram_used_gb"]
        logger.debug(f"VRAM usage → {vram_used_gb:.2f}/{vram_total_gb:.2f}GB")

    # Which competing programs are running is domain knowledge, not a
    # hardware reading, so it lives in backend/monitoring/process_watch.py.
    # The snapshot field keeps its name: safety policy, the IPC contract and
    # the REST contract all read `unity_running`.
    from backend.monitoring.process_watch import unity_running as _unity_running

    unity_running = _unity_running()

    logger.debug("Resource snapshot complete")
    return ResourceSnapshot(
        cpu_usage=cpu_usage,
        ram_used_gb=ram_used_gb,
        ram_total_gb=ram_total_gb,
        vram_used_gb=vram_used_gb,
        vram_total_gb=vram_total_gb,
        unity_running=unity_running,
    )
