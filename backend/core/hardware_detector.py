# backend/core/hardware_detector.py

"""
Comprehensive hardware detection for ARIA Lite's model-compatibility and
performance-estimation pipeline.

This replaces the ad-hoc CPU detection that used to be duplicated across
compatibility_checker.py and performance_estimator.py, both of which:
  - used os.cpu_count() as "cores", which actually returns LOGICAL
    threads, not physical cores (wrong on every CPU with SMT/Hyper-
    Threading — a 6-core/12-thread CPU was reported as "12 cores");
  - only ever read real CPU feature flags on Linux (via /proc/cpuinfo)
    and just assumed AVX2 on every other OS, so a Windows machine
    without AVX2 (rare, but real on some older/low-power parts) was
    silently reported as AVX2-capable.

Every detection function here is best-effort: it degrades to a sensible
"unknown" default rather than raising, because this feeds UI badges and
install gating, not anything safety-critical.

Each "detect_*" function does live OS/driver probing and is intended to
be called at most once per request (results should be cached by the
caller if used repeatedly). Each has a matching pure "classify_*"
function that takes already-known hardware facts and does no probing at
all — this is what backend/tests/hardware_detection_tests.py exercises
directly with literal, named hardware specs so the classification logic
itself is fully deterministic and unit-testable without needing that
exact physical machine.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import platform
import tempfile

import psutil

try:
    import cpuinfo as _py_cpuinfo
except ImportError:
    _py_cpuinfo = None

try:
    import pynvml as _pynvml
except ImportError:
    _pynvml = None

from logger import get_logger

logger = get_logger(__name__)


# ============================================================
# SECTION 1.1 — CPU
# ============================================================

# FLOPs performed per CPU cycle per core for a vectorized fused-multiply-
# add workload, by best available instruction set. AVX-512 processes
# 512-bit (16x fp32) vectors and can dual-issue FMA on most
# implementations (~32 FLOPs/cycle); AVX2 processes 256-bit (8x fp32)
# vectors with FMA (~16 FLOPs/cycle); a machine with neither is limited
# to SSE-width vectors (~4 FLOPs/cycle). These are standard, widely-cited
# per-cycle throughput figures for FMA3-capable cores, not measured on
# any specific chip — see avx2_tier()'s docstring for why an actual
# per-machine FLOPS benchmark is out of scope here.
_FLOPS_PER_CYCLE_AVX512 = 32.0
_FLOPS_PER_CYCLE_AVX2 = 16.0
_FLOPS_PER_CYCLE_BASELINE = 4.0

_INTEL_GEN_PATTERN = re.compile(r"i[3579][- ](\d{4,5})", re.IGNORECASE)
_AMD_RYZEN_PATTERN = re.compile(r"ryzen\s+\d+\s+(\d{3,4})", re.IGNORECASE)

_SUPPORTED_INTEL_GENERATIONS = {12, 13, 14}
_SUPPORTED_AMD_SERIES = {5000, 6000, 7000}


def _raw_cpu_info() -> dict:
    """
    Best-effort raw CPU info from py-cpuinfo, which (unlike
    /proc/cpuinfo) works cross-platform including Windows. Falls back to
    an empty dict — every caller treats missing keys as "unknown".
    """
    if _py_cpuinfo is None:
        logger.debug("_raw_cpu_info() → py-cpuinfo not installed")
        return {}
    try:
        return _py_cpuinfo.get_cpu_info() or {}
    except Exception as e:
        logger.debug(f"_raw_cpu_info() → detection failed: {e}")
        return {}


def _detect_intel_generation(brand: str) -> str | None:
    match = _INTEL_GEN_PATTERN.search(brand)
    if not match:
        return None
    digits = match.group(1)
    generation = int(digits[:2])
    if generation in _SUPPORTED_INTEL_GENERATIONS:
        return f"Intel {generation}th Gen"
    return None


def _detect_amd_generation(brand: str) -> str | None:
    match = _AMD_RYZEN_PATTERN.search(brand)
    if not match:
        return None
    digits = match.group(1)
    series = int(digits[0]) * 1000
    if series in _SUPPORTED_AMD_SERIES:
        return f"AMD Ryzen {series} Series"
    return None


def detect_cpu_generation(brand: str) -> str | None:
    """
    Best-effort CPU generation label from a brand string, e.g.
    "Intel(R) Core(TM) i9-14900KF" → "Intel 14th Gen", or
    "AMD Ryzen 5 5600X 6-Core Processor" → "AMD Ryzen 5000 Series".

    Only recognizes Intel 12th-14th gen and AMD 5000-7000 series per
    this feature's spec — anything else (older/newer parts, non-Intel/
    AMD CPUs) returns None rather than a guess.
    """
    if not brand:
        return None
    return _detect_intel_generation(brand) or _detect_amd_generation(brand)


def estimate_avx_gflops(physical_cores: int, base_clock_ghz: float, avx2: bool, avx512: bool) -> float:
    """
    Rough estimated peak AVX throughput in GFLOPS: physical_cores x
    clock x FLOPs-per-cycle-per-core for the best available instruction
    set. This is a theoretical-peak estimate, not a measured benchmark —
    there is no dependency-free way to actually run a SIMD microbenchmark
    from pure Python without a compiled extension (numpy is not a
    project dependency). It's good enough to bucket into the Low/Medium/
    High tiers this feature asks for, which is the only thing it's used
    for.
    """
    if physical_cores <= 0 or base_clock_ghz <= 0:
        return 0.0

    if avx512:
        flops_per_cycle = _FLOPS_PER_CYCLE_AVX512
    elif avx2:
        flops_per_cycle = _FLOPS_PER_CYCLE_AVX2
    else:
        flops_per_cycle = _FLOPS_PER_CYCLE_BASELINE

    return physical_cores * base_clock_ghz * flops_per_cycle


def avx2_tier(gflops: float) -> str:
    """Low < 150 GFLOPS <= Medium <= 300 GFLOPS < High."""
    if gflops < 150:
        return "Low"
    if gflops <= 300:
        return "Medium"
    return "High"


AVX2_TIER_ORDER = {"None": 0, "Low": 1, "Medium": 2, "High": 3}


def classify_cpu(physical_cores: int, logical_threads: int, base_clock_ghz: float,
                  avx2: bool, avx512: bool, brand: str = "") -> dict:
    """
    Pure classification — takes already-known hardware facts, does no
    probing. This is what backend/tests/hardware_detection_tests.py
    drives directly with literal, named CPU specs.
    """
    gflops = estimate_avx_gflops(physical_cores, base_clock_ghz, avx2, avx512)
    return {
        "physical_cores": physical_cores,
        "logical_threads": logical_threads,
        "base_clock_ghz": base_clock_ghz,
        "avx2": avx2,
        "avx512": avx512,
        "brand": brand,
        "generation": detect_cpu_generation(brand),
        "estimated_gflops": round(gflops, 1),
        "avx2_tier": avx2_tier(gflops) if avx2 or avx512 else "None",
    }


def detect_cpu() -> dict:
    """Live CPU detection → classify_cpu()."""
    logger.debug("detect_cpu() called")

    physical_cores = psutil.cpu_count(logical=False) or os.cpu_count() or 1
    logical_threads = psutil.cpu_count(logical=True) or os.cpu_count() or physical_cores

    raw = _raw_cpu_info()
    brand = raw.get("brand_raw", "") or platform.processor() or ""
    flags = set(f.lower() for f in raw.get("flags", []) or [])
    avx2 = "avx2" in flags
    avx512 = any(f.startswith("avx512") for f in flags)

    base_clock_ghz = 0.0
    hz_advertised = raw.get("hz_advertised", None)
    if hz_advertised and isinstance(hz_advertised, (list, tuple)) and hz_advertised[0]:
        # py-cpuinfo's own convention: actual Hz = hz_advertised[0] * 10 **
        # hz_advertised[1] (e.g. [3187000000, 0] → 3,187,000,000 Hz).
        # Previously divided by 10**exponent instead of multiplying,
        # which for the common exponent=0 case left the raw Hz count
        # (3.187 BILLION) sitting in what every caller — avx2 GFLOPS
        # estimation, CPU generation display — treats as already-GHz.
        hz = hz_advertised[0] * (10 ** hz_advertised[1])
        base_clock_ghz = hz / 1e9
    if not base_clock_ghz:
        try:
            freq = psutil.cpu_freq()
            if freq and freq.max:
                base_clock_ghz = freq.max / 1000.0
            elif freq and freq.current:
                base_clock_ghz = freq.current / 1000.0
        except Exception as e:
            logger.debug(f"detect_cpu() → psutil.cpu_freq() failed: {e}")

    result = classify_cpu(physical_cores, logical_threads, base_clock_ghz, avx2, avx512, brand)
    logger.debug(f"detect_cpu() → {result}")
    return result


# ============================================================
# SECTION 1.2 — RAM
# ============================================================

def ram_tier(total_gb: float) -> str:
    """Low < 16 <= Medium < 32 <= High <= 64 < Ultra."""
    if total_gb < 16:
        return "Low"
    if total_gb < 32:
        return "Medium"
    if total_gb <= 64:
        return "High"
    return "Ultra"


RAM_PRESSURE_THRESHOLD_GB = 20.0


def classify_ram(total_gb: float, available_gb: float) -> dict:
    used_gb = max(0.0, total_gb - available_gb)
    return {
        "total_gb": round(total_gb, 2),
        "available_gb": round(available_gb, 2),
        "used_gb": round(used_gb, 2),
        "pressure": used_gb > RAM_PRESSURE_THRESHOLD_GB,
        "tier": ram_tier(total_gb),
    }


# Fallback used only when a live per-DIMM speed/channel query isn't
# available (non-Windows, or the WMI call failed) — a conservative
# dual-channel DDR4-3200 estimate, roughly the low end of what any
# machine built in the last several years actually has.
_DEFAULT_RAM_BANDWIDTH_GBPS = 51.2


def _detect_ram_bandwidth_gbps() -> float:
    """
    Best-effort real RAM bandwidth estimate: effective transfer rate
    (MT/s) x 8 bytes-per-channel x channel count / 1000. On Windows this
    reads real per-DIMM ConfiguredClockSpeed + channel population via
    WMI; everywhere else (no reliable dependency-free equivalent without
    root) it falls back to a conservative dual-channel DDR4-3200 default.
    """
    if platform.system() != "Windows":
        logger.debug("_detect_ram_bandwidth_gbps() → non-Windows, using default estimate")
        return _DEFAULT_RAM_BANDWIDTH_GBPS

    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_PhysicalMemory | "
             "Select-Object ConfiguredClockSpeed,DeviceLocator | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            logger.debug(f"_detect_ram_bandwidth_gbps() → WMI query failed: {proc.stderr}")
            return _DEFAULT_RAM_BANDWIDTH_GBPS

        import json
        data = json.loads(proc.stdout)
        if isinstance(data, dict):
            data = [data]

        clock_speeds = [d.get("ConfiguredClockSpeed") for d in data if d.get("ConfiguredClockSpeed")]
        channels = set(d.get("DeviceLocator", "").rsplit("-DIMM", 1)[0] for d in data if d.get("DeviceLocator"))

        if not clock_speeds or not channels:
            return _DEFAULT_RAM_BANDWIDTH_GBPS

        mt_per_sec = max(clock_speeds)
        channel_count = max(1, len(channels))
        bandwidth_gbps = (mt_per_sec * 8 * channel_count) / 1000.0

        logger.debug(
            f"_detect_ram_bandwidth_gbps() → {mt_per_sec}MT/s x {channel_count} channel(s) = {bandwidth_gbps:.1f}GB/s"
        )
        return bandwidth_gbps

    except Exception as e:
        logger.debug(f"_detect_ram_bandwidth_gbps() → detection failed: {e}")
        return _DEFAULT_RAM_BANDWIDTH_GBPS


def detect_ram() -> dict:
    logger.debug("detect_ram() called")
    vm = psutil.virtual_memory()
    total_gb = vm.total / (1024 ** 3)
    available_gb = vm.available / (1024 ** 3)

    result = classify_ram(total_gb, available_gb)
    result["bandwidth_gbps"] = round(_detect_ram_bandwidth_gbps(), 1)

    logger.debug(f"detect_ram() → {result}")
    return result


# ============================================================
# SECTION 1.3 — GPU
# ============================================================

def vram_tier(total_gb: float) -> str:
    """Low < 6 <= Medium < 12 <= High <= 16 < Ultra."""
    if total_gb < 6:
        return "Low"
    if total_gb < 12:
        return "Medium"
    if total_gb <= 16:
        return "High"
    return "Ultra"


# Peak memory bandwidth (GB/s) for popular consumer GPUs, from vendor
# spec sheets. NVML doesn't expose a driver-queryable "effective GDDR6/
# GDDR6X data rate" (the PAM4 signaling multiplier isn't a standard NVML
# field), so known cards use their real published spec here; unknown
# cards fall back to a bus-width/clock-based estimate in
# _estimate_bandwidth_from_clock() below. Longest key wins so "RTX 4070
# TI SUPER" isn't shadowed by the "RTX 4070" substring.
_GPU_BANDWIDTH_LOOKUP_GBPS = {
    "RTX 4090": 1008.0,
    "RTX 4080 SUPER": 736.3,
    "RTX 4080": 716.8,
    "RTX 4070 TI SUPER": 672.3,
    "RTX 4070 SUPER": 504.2,
    "RTX 4070 TI": 504.2,
    "RTX 4070": 504.2,
    "RTX 4060 TI": 288.0,
    "RTX 4060": 272.0,
    "RTX 3090 TI": 1008.0,
    "RTX 3090": 936.2,
    "RTX 3080 TI": 912.4,
    "RTX 3080": 760.3,
    "RTX 3070 TI": 608.3,
    "RTX 3070": 448.0,
    "RTX 3060 TI": 448.0,
    "RTX 3060": 360.0,
    "RTX 3050": 224.0,
}


def _lookup_gpu_bandwidth(name: str) -> float | None:
    upper = (name or "").upper()
    for key in sorted(_GPU_BANDWIDTH_LOOKUP_GBPS, key=len, reverse=True):
        if key in upper:
            return _GPU_BANDWIDTH_LOOKUP_GBPS[key]
    return None


def _estimate_bandwidth_from_clock(mem_clock_mhz: float, bus_width_bits: float) -> float:
    """
    Fallback for GPUs not in the lookup table: standard GDDR double-
    data-rate formula (clock x 2 x bus width / 8). This underestimates
    real GDDR6X bandwidth (which uses PAM4 quad-level signaling) by
    roughly 2x, but a driver-sourced clock+bus-width combo with a
    labeled-conservative formula is more honest than fabricating a
    number for a card we don't recognize.
    """
    if mem_clock_mhz <= 0 or bus_width_bits <= 0:
        return 0.0
    return (mem_clock_mhz * 2 * bus_width_bits) / 8 / 1000.0


def classify_gpu(name: str, vram_total_gb: float, vram_used_gb: float,
                  cuda_cores: int | None, compute_capability: str | None,
                  memory_bandwidth_gbps: float | None) -> dict:
    """Pure classification — no probing."""
    return {
        "name": name,
        "vram_total_gb": round(vram_total_gb, 2),
        "vram_used_gb": round(vram_used_gb, 2),
        "vram_free_gb": round(max(0.0, vram_total_gb - vram_used_gb), 2),
        "cuda_cores": cuda_cores,
        "compute_capability": compute_capability,
        "memory_bandwidth_gbps": round(memory_bandwidth_gbps, 1) if memory_bandwidth_gbps else None,
        "vram_tier": vram_tier(vram_total_gb),
    }


def detect_gpu() -> dict | None:
    """
    Live GPU detection via NVML (NVIDIA Management Library). Returns
    None on any non-NVIDIA/driverless system rather than raising — a
    CPU-only machine is a completely normal, supported configuration for
    this app.
    """
    logger.debug("detect_gpu() called")

    if _pynvml is None:
        logger.debug("detect_gpu() → pynvml not installed, treating as no GPU")
        return None

    try:
        _pynvml.nvmlInit()
    except Exception as e:
        logger.debug(f"detect_gpu() → nvmlInit failed (no NVIDIA GPU/driver): {e}")
        return None

    try:
        if _pynvml.nvmlDeviceGetCount() < 1:
            return None

        handle = _pynvml.nvmlDeviceGetHandleByIndex(0)
        name = _pynvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="ignore")

        mem = _pynvml.nvmlDeviceGetMemoryInfo(handle)
        vram_total_gb = mem.total / (1024 ** 3)
        vram_used_gb = mem.used / (1024 ** 3)

        try:
            major, minor = _pynvml.nvmlDeviceGetCudaComputeCapability(handle)
            compute_capability = f"{major}.{minor}"
        except Exception as e:
            logger.debug(f"detect_gpu() → compute capability query failed: {e}")
            compute_capability = None

        try:
            cuda_cores = int(_pynvml.nvmlDeviceGetNumGpuCores(handle))
        except Exception as e:
            logger.debug(f"detect_gpu() → CUDA core count query failed: {e}")
            cuda_cores = None

        bandwidth_gbps = _lookup_gpu_bandwidth(name)
        if bandwidth_gbps is None:
            try:
                mem_clock_mhz = _pynvml.nvmlDeviceGetClockInfo(handle, _pynvml.NVML_CLOCK_MEM)
                bus_width_bits = _pynvml.nvmlDeviceGetMemoryBusWidth(handle)
                bandwidth_gbps = _estimate_bandwidth_from_clock(mem_clock_mhz, bus_width_bits)
            except Exception as e:
                logger.debug(f"detect_gpu() → bandwidth clock fallback failed: {e}")
                bandwidth_gbps = None

        result = classify_gpu(name, vram_total_gb, vram_used_gb, cuda_cores, compute_capability, bandwidth_gbps)
        logger.debug(f"detect_gpu() → {result}")
        return result

    except Exception as e:
        logger.debug(f"detect_gpu() → detection failed: {e}")
        return None

    finally:
        try:
            _pynvml.nvmlShutdown()
        except Exception:
            pass


# ============================================================
# SECTION 1.4 — STORAGE
# ============================================================

def ssd_tier(speed_gbps: float) -> str:
    """Low < 1 <= Medium < 3 <= High <= 5 < Ultra."""
    if speed_gbps < 1:
        return "Low"
    if speed_gbps < 3:
        return "Medium"
    if speed_gbps <= 5:
        return "High"
    return "Ultra"


def classify_storage(storage_type: str, read_speed_gbps: float, write_speed_gbps: float) -> dict:
    return {
        "type": storage_type,
        "read_speed_gbps": round(read_speed_gbps, 2),
        "write_speed_gbps": round(write_speed_gbps, 2),
        "tier": ssd_tier(read_speed_gbps),
    }


def _detect_storage_type(path: str) -> str:
    """
    Best-effort NVMe / SATA SSD / HDD detection for the physical disk
    backing `path`. Windows: resolves the drive letter to its physical
    disk via Get-Partition/Get-Disk/Get-PhysicalDisk. Linux: reads
    /sys/block/<dev>/queue/rotational and .../device/transport via
    lsblk. Returns "Unknown" rather than raising if any step fails.
    """
    try:
        if platform.system() == "Windows":
            drive_letter = os.path.splitdrive(os.path.abspath(path))[0].rstrip(":\\")
            script = (
                f"$p = Get-Partition -DriveLetter {drive_letter} -ErrorAction SilentlyContinue; "
                "if ($p) { $d = Get-Disk -Number $p.DiskNumber; "
                "$pd = Get-PhysicalDisk -DeviceNumber $d.Number; "
                "($pd | Select-Object -First 1).BusType.ToString() + '|' + ($pd | Select-Object -First 1).MediaType.ToString() }"
            )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True, text=True, timeout=5,
            )
            output = (proc.stdout or "").strip()
            if not output or "|" not in output:
                return "Unknown"
            bus_type, media_type = output.split("|", 1)
            if bus_type.upper() == "NVME":
                return "NVMe"
            if media_type.upper() == "HDD":
                return "HDD"
            if media_type.upper() == "SSD":
                return "SATA SSD"
            return "Unknown"

        if platform.system() == "Linux":
            proc = subprocess.run(
                ["lsblk", "-d", "-o", "NAME,ROTA,TRAN", "-J"],
                capture_output=True, text=True, timeout=5,
            )
            if proc.returncode != 0:
                return "Unknown"
            import json
            devices = json.loads(proc.stdout).get("blockdevices", [])
            if not devices:
                return "Unknown"
            dev = devices[0]
            tran = (dev.get("tran") or "").lower()
            rota = dev.get("rota")
            if tran == "nvme":
                return "NVMe"
            if rota in ("0", 0, False):
                return "SATA SSD"
            return "HDD"

        return "Unknown"

    except Exception as e:
        logger.debug(f"_detect_storage_type() → detection failed: {e}")
        return "Unknown"


_BENCHMARK_FILE_SIZE_BYTES = 64 * 1024 * 1024  # 64MB — big enough to mostly defeat small write caches


def _benchmark_read_write_gbps(path: str) -> tuple[float, float]:
    """
    Lightweight local read/write micro-benchmark against `path` (a
    directory). Best-effort: on any failure (permissions, read-only
    filesystem) returns (0.0, 0.0) rather than raising.
    """
    try:
        os.makedirs(path, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=path, prefix=".aria_storage_bench_")
        try:
            payload = os.urandom(1024 * 1024)  # 1MB chunk, written repeatedly
            chunks = _BENCHMARK_FILE_SIZE_BYTES // len(payload)

            start = time.perf_counter()
            with os.fdopen(fd, "wb") as f:
                for _ in range(chunks):
                    f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            write_elapsed = time.perf_counter() - start
            write_gbps = (_BENCHMARK_FILE_SIZE_BYTES / (1024 ** 3)) / write_elapsed if write_elapsed > 0 else 0.0

            start = time.perf_counter()
            with open(tmp_path, "rb") as f:
                while f.read(4 * 1024 * 1024):
                    pass
            read_elapsed = time.perf_counter() - start
            read_gbps = (_BENCHMARK_FILE_SIZE_BYTES / (1024 ** 3)) / read_elapsed if read_elapsed > 0 else 0.0

            return read_gbps, write_gbps
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    except Exception as e:
        logger.debug(f"_benchmark_read_write_gbps() → benchmark failed: {e}")
        return 0.0, 0.0


def detect_storage(path: str | None = None) -> dict:
    """
    Live storage detection for the disk backing `path` (defaults to the
    user's home directory, a reasonable stand-in for wherever models end
    up on disk when no specific path is known yet).
    """
    logger.debug(f"detect_storage() called for path={path}")

    target_path = path or os.path.expanduser("~")
    storage_type = _detect_storage_type(target_path)
    read_gbps, write_gbps = _benchmark_read_write_gbps(target_path)

    result = classify_storage(storage_type, read_gbps, write_gbps)
    logger.debug(f"detect_storage() → {result}")
    return result


# ============================================================
# SECTION 1.5 — THERMAL
# ============================================================

# Conservative "likely throttling" line shared by both detection paths
# below -- not chip-specific (a real per-model junction-temp threshold
# would need a lookup table this project has no data source for), so
# this is deliberately a coarse signal used only to decide whether
# warning_manager.py should surface a critical warning, not a precise
# measurement.
THROTTLE_TEMP_C = 95.0


def thermal_tier(supported: bool, throttling_detected: bool) -> str:
    if not supported:
        return "Unknown"
    return "Throttling" if throttling_detected else "Normal"


def classify_thermal(supported: bool, throttling_detected: bool, max_temp_c: float | None) -> dict:
    return {
        "supported": supported,
        "throttling_detected": throttling_detected,
        "max_temp_c": max_temp_c,
        "tier": thermal_tier(supported, throttling_detected),
    }


def _detect_thermal_psutil() -> tuple[bool, float | None] | None:
    """
    psutil.sensors_temperatures() -- Linux (and some BSD) only; the
    attribute doesn't exist at all on Windows/macOS builds of psutil.
    Returns None (not a supported=False result) when this path can't
    even be attempted, so detect_thermal() knows to try the Windows WMI
    fallback next instead of giving up.
    """
    sensors_fn = getattr(psutil, "sensors_temperatures", None)
    if sensors_fn is None:
        return None
    try:
        temps = sensors_fn() or {}
    except Exception as e:
        logger.debug(f"_detect_thermal_psutil() → failed: {e}")
        return None
    if not temps:
        return None

    max_temp: float | None = None
    for entries in temps.values():
        for entry in entries:
            current = getattr(entry, "current", None)
            if current is not None and (max_temp is None or current > max_temp):
                max_temp = current
    if max_temp is None:
        return None
    return (True, max_temp)


def _detect_thermal_windows_wmi() -> tuple[bool, float | None] | None:
    """
    Best-effort Windows ACPI thermal-zone read via the stock `wmic` CLI
    (MSAcpi_ThermalZoneTemperature, reported in tenths of Kelvin) --
    deliberately shelling out to `wmic` rather than adding a `wmi`/
    `pywin32` dependency this project doesn't otherwise need. Many
    consumer boards don't populate this WMI namespace at all; that's a
    clean, common "unsupported" result here, not a failure to fix.
    """
    if platform.system() != "Windows":
        return None
    try:
        completed = subprocess.run(
            ["wmic", "/namespace:\\\\root\\wmi", "PATH", "MSAcpi_ThermalZoneTemperature", "get", "CurrentTemperature"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception as e:
        logger.debug(f"_detect_thermal_windows_wmi() → wmic invocation failed: {e}")
        return None
    if completed.returncode != 0:
        return None

    values = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            values.append((int(line) / 10.0) - 273.15)
    if not values:
        return None
    return (True, max(values))


def detect_thermal() -> dict:
    """
    Best-effort thermal-throttling detection for warning_manager.py's
    critical-warning conditions. There is no single reliable
    cross-platform temperature API (psutil.sensors_temperatures() is
    Linux-only; Windows needs a WMI ACPI query most consumer boards
    never populate) -- rather than fabricate a throttling signal from an
    unrelated proxy (CPU frequency, load average, ...), this reports
    supported=False whenever no real reading is available. Callers MUST
    treat supported=False as "unknown", never as "confirmed not
    throttling" -- see classify_thermal()'s "Unknown" tier, which exists
    specifically so a caller can't collapse that distinction by accident.
    """
    logger.debug("detect_thermal() called")

    reading = _detect_thermal_psutil()
    if reading is None:
        reading = _detect_thermal_windows_wmi()

    if reading is None:
        result = classify_thermal(supported=False, throttling_detected=False, max_temp_c=None)
        logger.debug("detect_thermal() → unsupported on this platform/hardware")
        return result

    supported, max_temp_c = reading
    throttling = max_temp_c is not None and max_temp_c >= THROTTLE_TEMP_C
    result = classify_thermal(supported=supported, throttling_detected=throttling, max_temp_c=max_temp_c)
    logger.debug(f"detect_thermal() → {result}")
    return result


# ============================================================
# COMBINED SNAPSHOT
# ============================================================

def detect_all(storage_path: str | None = None) -> dict:
    """One-shot combined hardware snapshot for the compat/performance pipeline."""
    logger.debug("detect_all() called")
    return {
        "cpu": detect_cpu(),
        "ram": detect_ram(),
        "gpu": detect_gpu(),
        "storage": detect_storage(storage_path),
        "thermal": detect_thermal(),
    }
