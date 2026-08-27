# Streaming Engine v2

Streaming Engine v2 — Phase 2, item 9.

backend.core.streaming_engine.StreamingEngine is live today, used by
all three real chat entry points (backend/websocket/handlers.py,
backend/server.py's /chat, backend/rest/router.py's /v1/chat/stream) —
this does NOT replace or rewire any of that. Ripping out and replacing
a tested, safety-critical streaming path used by every real chat
request, unattended, is exactly the kind of high-blast-radius change
this pass avoids (same reasoning Phase 1's REST router documented for
NOT sharing a pipeline with handlers.py).

StreamingEngineV2 is new, adds real capabilities v1 doesn't have —
adaptive chunk batching, bounded-queue backpressure, and cooperative
interruption — and is what backend.core.execution_pipeline (Phase 2,
item 4) uses for any NEW call path. Existing call sites keep using v1
unchanged; adopting v2 for them is a deliberate future decision, not
something this pass makes silently.

Honest scope on "interruption + resume": a StreamHandle's cancel() stops
the CONSUMER — no further stream_token packets are sent, and it flushes
what's already buffered — within one poll interval (~50ms), regardless
of what the producer is doing. It does NOT forcibly stop the underlying
provider call itself: none of the 15 real providers in
backend.llm.providers accept an external cancellation signal today, so
a cloud API request already in flight, or a local llama.cpp generation
loop already running, keeps running to completion in the background
(its remaining output is simply never enqueued/sent — on_chunk() checks
is_cancelled before queuing). Real process-level interruption of the
provider itself would need backend.core.sandbox.run_in_subprocess()
(which CAN be killed) instead of this thread-based design — a
deliberate tradeoff for streaming's low per-chunk overhead requirement,
where process-per-turn isolation would be far too slow to be viable.

"Resume" does NOT mean
continuing generation from the exact interrupted token at the provider
level (no provider in backend.llm.providers supports that); it means
StreamHandle retains get_partial_text() so a caller can decide to
re-submit a continuation as a NEW request. Claiming true token-level
resume would be dishonest given what the underlying providers actually
support.

## Classes

### `StreamHandle`

Returned immediately by stream_v2() (the actual streaming happens
on a background thread) — the caller's remote control for the
in-flight turn.

- `__init__(self, request_id: 'int', _cancel_event: 'threading.Event' = <factory>, _partial_text: 'List[str]' = <factory>, _lock: 'threading.Lock' = <factory>, _done_event: 'threading.Event' = <factory>) -> None`
  Initialize self.  See help(type(self)) for accurate signature.
- `cancel(self) -> 'None'`
- `get_partial_text(self) -> 'str'`
- `wait(self, timeout: 'Optional[float]' = None) -> 'bool'`

### `StreamingEngineV2`

_No docstring provided._

- `stream(self, request: 'Any', provider: 'ProviderAdapter', send_packet: 'Callable[[dict], None]', *, flush_interval: 'float' = 0.03, flush_max_chars: 'int' = 64, queue_maxsize: 'int' = 256) -> 'StreamHandle'`
  Starts streaming on a background thread and returns a StreamHandle
