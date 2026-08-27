# Hardware Snapshot Cache (Phase 1)

TTL-cached wrapper around backend.core.hardware_detector.

Root cause of the "15-second model card stall": backend.core.model_manager
.list_models() (called by every models_list_request / GET /v1/models) and
backend.core.compatibility_checker.check_requirements() /
backend.core.performance_estimator.estimate_speed() (called once per
model, per request) each independently called hardware_detector.detect_cpu()
/ detect_ram() / detect_gpu() / detect_storage() fresh, every single call —
including detect_storage()'s real local read/write benchmark. None of
that data changes between one request and the next half-second later; the
CPU doesn't change generation, the GPU doesn't change VRAM, and a spinning
disk's sequential throughput doesn't change from one Models-page click to
the next.

This module is the single place those four detectors get called from now.
Static-ish facts (CPU/GPU/storage) are cached with a long TTL; RAM gets a
much shorter one since "available"/"pressure" genuinely changes as other
processes run. Callers ask for a snapshot; a cache hit is a dict lookup, a
miss re-probes (mirroring, and instrumented by, backend.core.perf_profiler
so a systemic slowdown here shows up in /v1/diagnostics/performance instead
of silently degrading every model card load again).

## Functions

### `cache_state() -> 'dict'`

For /v1/diagnostics/hardware — what's cached and how fresh it is,
without forcing a re-probe.

### `get_all(storage_path: 'Optional[str]' = None) -> 'dict'`

_No docstring provided._

### `get_cpu() -> 'dict'`

_No docstring provided._

### `get_gpu() -> 'Optional[dict]'`

_No docstring provided._

### `get_ram() -> 'dict'`

_No docstring provided._

### `get_storage(path: 'Optional[str]' = None) -> 'dict'`

_No docstring provided._

### `refresh() -> 'dict'`

Force-invalidate everything and re-probe immediately. Used by the
startup pipeline (so the very first request gets a warm cache instead
of paying the first-probe cost) and by /v1/diagnostics/hardware?refresh=1.
