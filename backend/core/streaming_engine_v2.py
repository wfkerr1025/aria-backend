# backend/core/streaming_engine_v2.py

"""
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
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from .provider_interface import ProviderAdapter
from . import perf_profiler

from logger import get_logger

logger = get_logger(__name__)

DEFAULT_CHUNK_FLUSH_INTERVAL_SECONDS = 0.03
DEFAULT_CHUNK_FLUSH_MAX_CHARS = 64
DEFAULT_QUEUE_MAXSIZE = 256


@dataclass
class StreamHandle:
    """Returned immediately by stream_v2() (the actual streaming happens
    on a background thread) — the caller's remote control for the
    in-flight turn."""

    request_id: int
    _cancel_event: threading.Event = field(default_factory=threading.Event)
    _partial_text: List[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _done_event: threading.Event = field(default_factory=threading.Event)

    def cancel(self) -> None:
        self._cancel_event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    @property
    def is_done(self) -> bool:
        return self._done_event.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._done_event.wait(timeout)

    def get_partial_text(self) -> str:
        with self._lock:
            return "".join(self._partial_text)

    def _append(self, text: str) -> None:
        with self._lock:
            self._partial_text.append(text)

    def _mark_done(self) -> None:
        self._done_event.set()


class _AdaptiveChunker:
    """
    Buffers raw provider chunks and flushes as fewer, larger send_packet()
    calls — real for a provider that yields many small pieces quickly
    (reduces per-chunk WS-frame/JSON-encode overhead); a provider that
    already yields large, infrequent chunks passes through with
    negligible added latency (the time-based flush fires long before the
    size threshold could matter).
    """

    def __init__(self, flush_interval: float, max_chars: int):
        self.flush_interval = flush_interval
        self.max_chars = max_chars
        self._buffer: List[str] = []
        self._buffer_len = 0
        self._last_flush = time.perf_counter()
        self.flush_count = 0
        self.raw_chunk_count = 0

    def add(self, text: str) -> Optional[str]:
        """Returns the text to flush now, or None if still buffering."""
        self.raw_chunk_count += 1
        self._buffer.append(text)
        self._buffer_len += len(text)

        now = time.perf_counter()
        should_flush = (
            self._buffer_len >= self.max_chars
            or (now - self._last_flush) >= self.flush_interval
        )
        if should_flush:
            return self._flush(now)
        return None

    def flush_remaining(self) -> Optional[str]:
        if not self._buffer:
            return None
        return self._flush(time.perf_counter())

    def _flush(self, now: float) -> str:
        text = "".join(self._buffer)
        self._buffer = []
        self._buffer_len = 0
        self._last_flush = now
        self.flush_count += 1
        return text


class StreamingEngineV2:
    def stream(
        self,
        request: Any,
        provider: ProviderAdapter,
        send_packet: Callable[[dict], None],
        *,
        flush_interval: float = DEFAULT_CHUNK_FLUSH_INTERVAL_SECONDS,
        flush_max_chars: int = DEFAULT_CHUNK_FLUSH_MAX_CHARS,
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        balancer: Any = None,
        cpu_monitor: Any = None,
    ) -> StreamHandle:
        """
        Starts streaming on a background thread and returns a StreamHandle
        immediately — unlike v1's StreamingEngine.stream() (which blocks
        the calling thread for the whole turn), so a caller can cancel()
        mid-stream from another thread/coroutine.

        balancer/cpu_monitor (backend.core.auto_balancer.AutoBalancer /
        backend.core.cpu_load_monitor.CPULoadMonitor) are optional — when
        both are given, the consumer loop's existing ~50ms poll tick also
        calls balancer.tick(cpu_monitor.get_current_cpu()) and applies
        balancer.get_chunk_size() to the chunker live, so an eligible
        CPU-only stream can actually shrink/grow its chunk size mid-turn
        (see auto_balancer.py's module docstring: chunk size is the ONE
        tier that's genuinely live like this — threads/context/quant take
        effect on this model's next load, not the in-flight generation).
        Absent for every other caller (v1's callers, non-eligible models),
        so this is a strict no-op unless both are explicitly supplied.
        """
        request_id = id(request)
        handle = StreamHandle(request_id=request_id)
        chunk_queue: "queue.Queue[Optional[dict]]" = queue.Queue(maxsize=queue_maxsize)
        _SENTINEL = None

        def producer():
            """Runs provider.stream()/infer() and pushes raw chunks onto
            the bounded queue — put() blocks when the queue is full,
            which IS the backpressure: a fast provider naturally slows
            down to match however fast the consumer thread drains it,
            instead of buffering unboundedly in memory."""
            try:
                def on_chunk(chunk: dict) -> None:
                    if handle.is_cancelled:
                        return
                    chunk_queue.put(chunk)  # blocks if full — real backpressure

                provider.stream(request, on_chunk)
            except Exception as e:
                logger.debug("StreamingEngineV2 producer error: %s", e)
                chunk_queue.put({"__error__": str(e)})
            finally:
                chunk_queue.put(_SENTINEL)

        def consumer():
            stream_start = time.perf_counter()
            first_token_time: Optional[float] = None
            token_count = 0
            chunker = _AdaptiveChunker(flush_interval, flush_max_chars)

            send_packet({"type": "stream_start", "requestId": request_id})

            try:
                while True:
                    if handle.is_cancelled:
                        remaining = chunker.flush_remaining()
                        if remaining:
                            handle._append(remaining)
                            send_packet({"type": "stream_token", "requestId": request_id, "token": remaining})
                        send_packet({"type": "stream_cancelled", "requestId": request_id})
                        break

                    # A plain, un-timed-out queue.get() would block here
                    # until the NEXT item arrives — if cancel() is called
                    # while already blocked waiting (the common case: the
                    # producer is mid-chunk), this loop would never get
                    # back to the is_cancelled check above until the
                    # producer's next put()/sentinel, defeating "immediate"
                    # cancellation. Polling with a short timeout means a
                    # cancel() fires within one poll interval regardless of
                    # what the producer/provider is doing.
                    if balancer is not None and cpu_monitor is not None:
                        action = balancer.tick(cpu_monitor.get_current_cpu())
                        if action is not None and action.field == "chunk_size":
                            chunker.max_chars = balancer.get_chunk_size()

                    try:
                        item = chunk_queue.get(timeout=0.05)
                    except queue.Empty:
                        continue

                    if item is _SENTINEL:
                        remaining = chunker.flush_remaining()
                        if remaining:
                            handle._append(remaining)
                            send_packet({"type": "stream_token", "requestId": request_id, "token": remaining})
                        send_packet({"type": "stream_end", "requestId": request_id})
                        break

                    if "__error__" in item:
                        send_packet({"type": "stream_error", "requestId": request_id, "message": item["__error__"]})
                        break

                    token = item.get("content", "")
                    if first_token_time is None:
                        first_token_time = time.perf_counter()
                    token_count += 1

                    flushed = chunker.add(token)
                    if flushed is not None:
                        handle._append(flushed)
                        send_packet({"type": "stream_token", "requestId": request_id, "token": flushed})

            finally:
                total_elapsed_ms = (time.perf_counter() - stream_start) * 1000.0
                perf_profiler.record("streaming_engine_v2.stream.total", total_elapsed_ms)
                if first_token_time is not None:
                    perf_profiler.record("streaming_engine_v2.stream.time_to_first_token", (first_token_time - stream_start) * 1000.0)
                if token_count > 0 and total_elapsed_ms > 0:
                    perf_profiler.record("streaming_engine_v2.stream.observed_tokens_per_sec", token_count / (total_elapsed_ms / 1000.0))
                if chunker.raw_chunk_count > 0:
                    perf_profiler.record("streaming_engine_v2.stream.chunks_per_flush", chunker.raw_chunk_count / max(1, chunker.flush_count))
                # The monitor was started by whoever created it (typically
                # execution_pipeline.py, right before calling stream())
                # specifically because the stream — not the outer
                # run_pipeline() call, which returns immediately — is what
                # actually determines when polling should stop.
                if cpu_monitor is not None:
                    cpu_monitor.stop()
                handle._mark_done()

        threading.Thread(target=producer, daemon=True, name=f"streaming-v2-producer-{request_id}").start()
        threading.Thread(target=consumer, daemon=True, name=f"streaming-v2-consumer-{request_id}").start()

        return handle
