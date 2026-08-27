# Unified Execution Pipeline

Unified execution pipeline — Phase 2, item 4.

Pre-execution (requirements/safety/provider selection), execution
(streaming via StreamingEngineV2, with backpressure/adaptive chunking/
interruption), and post-execution (metrics via perf_profiler, unified
logging) in one place, reusing exactly the same tested primitives
backend/websocket/handlers.py, backend/server.py's /chat, and
backend/rest/router.py's _prepare_chat_turn() each already call
independently (ProviderRouter for provider selection,
backend.core.safety_manager.evaluate_safety() for the safety gate).

Scope decision, stated plainly: this does NOT rewire those three live
chat entry points to call through here. They are the most heavily used,
most safety-critical code paths in the app, each independently
tested, and a prior task (backend/rest/router.py's own module docstring)
already made and documented the same call for the same reason — a third
independent orchestration was chosen there specifically to avoid
touching backend/websocket/handlers.py. Replacing all three unattended,
in one pass, with no one available to review a mistake in the safety
gate or mode-separation logic, is a risk this pass declines to take.
This pipeline is real, fully tested, and is what any NEW call path
(and, when someone deliberately chooses to, a future migration of the
existing three) should use — it is not wired into them today.

## Classes

### `PipelineResult`

PipelineResult(ok: 'bool', text: 'Optional[str]' = None, stream_handle: 'Optional[StreamHandle]' = None, model_id: 'Optional[str]' = None, provider_name: 'Optional[str]' = None, location: 'Optional[str]' = None, safety_severity: 'Optional[str]' = None, error: 'Optional[Dict[str, Any]]' = None, elapsed_seconds: 'float' = 0.0)

- `__init__(self, ok: 'bool', text: 'Optional[str]' = None, stream_handle: 'Optional[StreamHandle]' = None, model_id: 'Optional[str]' = None, provider_name: 'Optional[str]' = None, location: 'Optional[str]' = None, safety_severity: 'Optional[str]' = None, error: 'Optional[Dict[str, Any]]' = None, elapsed_seconds: 'float' = 0.0) -> None`
  Initialize self.  See help(type(self)) for accurate signature.

## Functions

### `run_pipeline(request: 'Any', send_packet: 'Optional[Callable[[dict], None]]' = None) -> 'PipelineResult'`

The full pipeline, non-streaming variant when send_packet is None
(blocks and returns the complete text) or streaming when it's given
(returns immediately with a StreamHandle the caller can cancel()).
