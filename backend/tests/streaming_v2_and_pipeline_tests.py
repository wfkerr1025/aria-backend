# backend/tests/streaming_v2_and_pipeline_tests.py
#
# Regression tests for Phase 2:
#   - backend/core/streaming_engine_v2.py: adaptive chunking (fewer,
#     larger send_packet calls for a fast-yielding provider),
#     backpressure (a bounded queue naturally throttles a fast producer
#     against a slow consumer, no deadlock), cooperative interruption
#     (cancel() takes effect within ~one poll interval, not only after
#     the underlying provider call finishes), token-timing
#     instrumentation feeding perf_profiler.
#   - backend/core/execution_pipeline.py: pre-execution (provider
#     resolution + safety gate) / execution / post-execution, using the
#     REAL ProviderRouter + safety_manager (not reimplementations of
#     them) — including one real end-to-end run against the actual
#     local model on this machine.
#
# Self-contained plain-assert tests. Run directly:
#
#   python backend/tests/streaming_v2_and_pipeline_tests.py

from __future__ import annotations

import os
import sys
import time
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ============================================================
# PART 1 — streaming_engine_v2.py
# ============================================================
class _FastProvider:
    def __init__(self, n=50, delay=0.0, content="x"):
        self.n = n
        self.delay = delay
        self.content = content

    def stream(self, request, callback):
        for _ in range(self.n):
            callback({"content": self.content})
            if self.delay:
                time.sleep(self.delay)


def test_adaptive_chunking_reduces_packet_count_for_many_fast_tokens():
    from backend.core.streaming_engine_v2 import StreamingEngineV2

    packets = []
    engine = StreamingEngineV2()
    handle = engine.stream(object(), _FastProvider(n=50), packets.append, flush_interval=0.02, flush_max_chars=1000)
    assert handle.wait(timeout=5)

    token_packets = [p for p in packets if p["type"] == "stream_token"]
    assert 0 < len(token_packets) < 50, f"expected batching to produce far fewer than 50 packets, got {len(token_packets)}"
    assert "".join(p["token"] for p in token_packets) == "x" * 50, "no token content may be lost to batching"


def test_stream_start_and_end_always_bracket_a_successful_stream():
    from backend.core.streaming_engine_v2 import StreamingEngineV2

    packets = []
    engine = StreamingEngineV2()
    handle = engine.stream(object(), _FastProvider(n=5), packets.append)
    assert handle.wait(timeout=5)

    assert packets[0]["type"] == "stream_start"
    assert packets[-1]["type"] == "stream_end"


def test_get_partial_text_matches_what_was_actually_sent():
    from backend.core.streaming_engine_v2 import StreamingEngineV2

    packets = []
    engine = StreamingEngineV2()
    handle = engine.stream(object(), _FastProvider(n=20, content="ab"), packets.append)
    assert handle.wait(timeout=5)

    sent_text = "".join(p["token"] for p in packets if p["type"] == "stream_token")
    assert handle.get_partial_text() == sent_text
    assert sent_text == "ab" * 20


def test_cancel_stops_the_stream_within_roughly_one_poll_interval():
    from backend.core.streaming_engine_v2 import StreamingEngineV2

    packets = []
    engine = StreamingEngineV2()
    handle = engine.stream(object(), _FastProvider(n=500, delay=0.01), packets.append)

    time.sleep(0.15)
    cancel_time = time.perf_counter()
    handle.cancel()
    assert handle.wait(timeout=2), "cancellation must complete quickly, not block for the full stream duration"
    resolve_time = time.perf_counter() - cancel_time

    assert resolve_time < 1.0, f"cancel() took {resolve_time}s to take effect — should be near-immediate"
    assert packets[-1]["type"] == "stream_cancelled"
    assert len(handle.get_partial_text()) < 500, "cancellation must stop well before all 500 chunks are consumed"


def test_backpressure_does_not_deadlock_with_a_tiny_queue_and_slow_consumer():
    from backend.core.streaming_engine_v2 import StreamingEngineV2

    received = []

    def slow_send(packet):
        received.append(packet)
        time.sleep(0.005)

    engine = StreamingEngineV2()
    handle = engine.stream(object(), _FastProvider(n=40), slow_send, queue_maxsize=2, flush_interval=0.001, flush_max_chars=1)
    assert handle.wait(timeout=10), "a bounded queue must throttle the producer, never deadlock"
    assert received[-1]["type"] == "stream_end"


def test_stream_error_is_sent_when_the_provider_raises():
    from backend.core.streaming_engine_v2 import StreamingEngineV2

    class BrokenProvider:
        def stream(self, request, callback):
            callback({"content": "partial"})
            raise RuntimeError("provider exploded")

    packets = []
    engine = StreamingEngineV2()
    handle = engine.stream(object(), BrokenProvider(), packets.append)
    assert handle.wait(timeout=5)

    assert any(p["type"] == "stream_error" for p in packets)
    assert "provider exploded" in next(p["message"] for p in packets if p["type"] == "stream_error")


def test_streaming_v2_records_timing_into_perf_profiler():
    from backend.core.streaming_engine_v2 import StreamingEngineV2
    from backend.core import perf_profiler

    perf_profiler.clear()
    engine = StreamingEngineV2()
    handle = engine.stream(object(), _FastProvider(n=10), lambda p: None)
    assert handle.wait(timeout=5)

    summary = perf_profiler.get_summary()
    assert "streaming_engine_v2.stream.total" in summary
    assert "streaming_engine_v2.stream.time_to_first_token" in summary


# ============================================================
# PART 2 — execution_pipeline.py
# ============================================================
def test_pipeline_reports_no_provider_cleanly_when_none_can_resolve():
    from backend.core.execution_pipeline import run_pipeline
    from backend.core.provider_router import ProviderRouter

    original_resolve = ProviderRouter.resolve
    ProviderRouter.resolve = lambda self, model_id, prompt=None, *a: (None, None)
    try:
        class FakeRequest:
            model_id = None
            prompt = "hi"
            allow_override = False

        result = run_pipeline(FakeRequest())
        assert result.ok is False
        assert result.error["code"] == "PIPELINE_NO_PROVIDER"
    finally:
        ProviderRouter.resolve = original_resolve


def test_pipeline_blocks_on_a_safety_violation_without_allow_override():
    from backend.core.execution_pipeline import run_pipeline
    from backend.core.provider_router import ProviderRouter
    from backend.core import safety_manager

    class FakeProviderObj:
        provider_name = "local"
        def run(self, request):
            raise AssertionError("must never reach infer() when safety blocks")

    original_resolve = ProviderRouter.resolve
    original_evaluate = safety_manager.evaluate_safety
    ProviderRouter.resolve = lambda self, model_id, prompt=None, *a: (FakeProviderObj(), "fake-model-id")

    class FakeDecision:
        requires_warning = True
        severity = "block"
        message = "not enough RAM"
        projected_cpu_pct = 10
        projected_ram_pct = 99
        projected_vram_pct = 0

    import backend.core.execution_pipeline as ep
    ep.evaluate_safety = lambda model_cfg: FakeDecision()
    ep.get_model = lambda model_id: {"id": model_id}  # must look "known" for the safety check to run

    try:
        class FakeRequest:
            model_id = "fake-model-id"
            prompt = "hi"
            allow_override = False

        result = run_pipeline(FakeRequest())
        assert result.ok is False
        assert result.error["code"] == "PIPELINE_SAFETY_BLOCKED"
        assert result.safety_severity == "block"
    finally:
        ProviderRouter.resolve = original_resolve
        ep.evaluate_safety = original_evaluate
        import backend.core.model_registry as mr
        ep.get_model = mr.get_model


def test_pipeline_allow_override_bypasses_a_safety_block():
    from backend.core.provider_router import ProviderRouter
    import backend.core.execution_pipeline as ep

    class FakeProviderObj:
        provider_name = "local"
        def run(self, request):
            return "produced despite the warning"

    original_resolve = ProviderRouter.resolve
    original_evaluate = ep.evaluate_safety
    original_get_model = ep.get_model

    ProviderRouter.resolve = lambda self, model_id, prompt=None, *a: (FakeProviderObj(), "fake-model-id")

    class FakeProfile:
        n_threads = 8
        max_ctx = 4096
        n_gpu_layers = 20  # non-zero → auto_balancer.is_eligible() must reject this (GPU offload, not CPU-only)

    class FakeDecision:
        requires_warning = True
        severity = "caution"
        message = "borderline"
        projected_cpu_pct = 10
        projected_ram_pct = 80
        projected_vram_pct = 0
        profile = FakeProfile()

    ep.evaluate_safety = lambda model_cfg: FakeDecision()
    # A non-12B params count also independently disqualifies this from
    # auto_balancer eligibility — belt-and-suspenders with n_gpu_layers
    # above so this test can't accidentally start a real balancer session.
    ep.get_model = lambda model_id: {"id": model_id, "provider": "local", "params": 1_000_000_000}

    try:
        class FakeRequest:
            model_id = "fake-model-id"
            prompt = "hi"
            allow_override = True

        result = ep.run_pipeline(FakeRequest())
        assert result.ok is True
        assert result.text == "produced despite the warning"
    finally:
        ProviderRouter.resolve = original_resolve
        ep.evaluate_safety = original_evaluate
        ep.get_model = original_get_model


def test_pipeline_streaming_mode_returns_a_stream_handle_immediately():
    from backend.core.provider_router import ProviderRouter
    import backend.core.execution_pipeline as ep

    class FakeProviderObj:
        provider_name = "local"
        def run(self, request):
            return "unused"
        def stream(self, request, callback):
            for _ in range(5):
                callback({"content": "z"})

    original_resolve = ProviderRouter.resolve
    original_get_model = ep.get_model
    ProviderRouter.resolve = lambda self, model_id, prompt=None, *a: (FakeProviderObj(), None)  # None model_id → no safety check, cloud-ish path
    ep.get_model = lambda model_id: None

    try:
        class FakeRequest:
            model_id = None
            prompt = "hi"
            allow_override = False

        packets = []
        result = ep.run_pipeline(FakeRequest(), send_packet=packets.append)
        assert result.ok is True
        assert result.stream_handle is not None
        assert result.stream_handle.wait(timeout=5)
        assert any(p["type"] == "stream_end" for p in packets)
    finally:
        ProviderRouter.resolve = original_resolve
        ep.get_model = original_get_model


def test_pipeline_real_end_to_end_against_the_actual_local_model():
    """
    One genuinely real integration check (no mocks) — actually resolves
    through ProviderRouter, runs the real safety gate, and performs real
    local inference via llama.cpp. This is the one test in this file
    that's allowed to be slow (real model load + generation).

    allow_override=True: this test's purpose is proving the pipeline's
    OWN mechanics work end to end, not re-testing evaluate_safety()'s
    threshold logic (already covered by backend/tests/
    safety_projection_tests.py against synthetic, controlled snapshots).
    Without it, this test is flaky by construction — whatever else is
    running on the real machine at the moment (this repo's own test
    suite runs plenty concurrently) can legitimately push real projected
    CPU/RAM past the "caution" line, which is a correct safety decision,
    not a pipeline bug, and shouldn't fail this test.
    """
    from backend.core.execution_pipeline import run_pipeline
    from backend.core.local_inference_engine import InferenceRequest, InferenceMessage
    from backend.core.model_registry import get_default_model_id

    req = InferenceRequest(
        model_id=get_default_model_id(),
        messages=[InferenceMessage(role="user", content="Say OK.")],
        max_tokens=8,
        allow_override=True,
    )
    result = run_pipeline(req)

    assert result.ok is True, f"real pipeline run failed: {result.error}"
    assert isinstance(result.text, str) and len(result.text) > 0
    assert result.model_id == get_default_model_id()
    assert result.elapsed_seconds > 0


# ============================================================
# RUNNER
# ============================================================
def _all_tests():
    return [obj for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]


def main() -> int:
    failures = []
    for test in _all_tests():
        name = test.__name__
        try:
            test()
            print(f"PASS  {name}")
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            failures.append(name)
        except Exception as e:
            print(f"ERROR {name}: {e}")
            traceback.print_exc()
            failures.append(name)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1

    print("All tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
