# Metrics & Telemetry

Metrics + telemetry (local only) — Phase 2, item 6.

Does not introduce a second measurement system: tokens/sec and latency
come from backend.core.perf_profiler's real recorded samples (Phase 1's
streaming_engine.py already records
streaming_engine.stream.{total,time_to_first_token,observed_tokens_per_sec}
per turn); memory/CPU/GPU utilization come from
backend.core.hardware_snapshot_cache + backend.core.resource_monitor.
This module's only job is assembling those already-real numbers into
one snapshot for GET /v1/metrics and a matching IPC packet — nothing
here is telemetry that leaves the machine; "local only" per the spec
means exactly that, there is no network call anywhere in this file.

## Functions

### `get_metrics_snapshot() -> 'Dict[str, Any]'`

The full /v1/metrics payload: tokens/sec + latency (from real
observed streaming turns, when any have happened this run — null
fields until the first one), throughput/perf-path summary (every
perf_profiler-instrumented function, not just streaming), and live
memory/CPU/GPU utilization.

### `get_resource_metrics() -> 'Dict[str, Any]'`

_No docstring provided._

### `get_streaming_metrics() -> 'Dict[str, Any]'`

_No docstring provided._
