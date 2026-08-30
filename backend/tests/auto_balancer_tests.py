# backend/tests/auto_balancer_tests.py
#
# Regression tests for the universal local-model CPU auto-balancer
# (applies to any local CPU-only model, not just a single size bucket):
#   - backend/core/cpu_load_monitor.py
#   - backend/core/auto_balancer.py
#   - its integration points: backend/core/model_loader.py (thread/
#     context/quant overrides applied on next load),
#     backend/core/streaming_engine_v2.py (live chunk-size ticking),
#     backend/core/execution_pipeline.py (session creation, eligibility
#     gating), and the /v1/diagnostics/autobalance REST + IPC surface.
#
# Timing constants (THREADS_ENTER_AFTER_SECONDS etc.) are monkeypatched
# down to a few hundredths of a second for these tests — real wall-clock
# sleeps still happen (this exercises the actual duration-gated state
# machine, not a mocked clock), just scaled so a full throttle+restore
# sequence takes well under a second instead of the real ~6.5 seconds
# the production thresholds would require. CPU percentages themselves
# are realistic simulated values per this feature's own spec (70/80/85/
# 60), never patched.
#
# Self-contained plain-assert tests. Run directly:
#
#   python backend/tests/auto_balancer_tests.py

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _model_cfg(model_id="test-12b", params=12_000_000_000, quant="Q5_K_M", path=None, provider="local"):
    return {
        "id": model_id, "provider": provider, "params": params, "quant": quant,
        "defaultFilename": f"{model_id}.{quant}.gguf",
        "path": path or f"/fake/models/{model_id}.{quant}.gguf",
    }


def _with_fast_timings(fn):
    """Scales every duration threshold down by ~30x for the duration of fn()."""
    import backend.core.auto_balancer as ab

    originals = {
        "THREADS_ENTER_AFTER_SECONDS": ab.THREADS_ENTER_AFTER_SECONDS,
        "CONTEXT_ENTER_AFTER_SECONDS": ab.CONTEXT_ENTER_AFTER_SECONDS,
        "QUANT_ENTER_AFTER_SECONDS": ab.QUANT_ENTER_AFTER_SECONDS,
        "QUANT_RESTORE_AFTER_SECONDS": ab.QUANT_RESTORE_AFTER_SECONDS,
        "CONTEXT_RESTORE_AFTER_SECONDS": ab.CONTEXT_RESTORE_AFTER_SECONDS,
        "THREADS_RESTORE_AFTER_SECONDS": ab.THREADS_RESTORE_AFTER_SECONDS,
        "CHUNK_RESTORE_AFTER_SECONDS": ab.CHUNK_RESTORE_AFTER_SECONDS,
        "_TICK_MIN_INTERVAL_SECONDS": ab._TICK_MIN_INTERVAL_SECONDS,
    }
    ab.THREADS_ENTER_AFTER_SECONDS = 0.05
    ab.CONTEXT_ENTER_AFTER_SECONDS = 0.10
    ab.QUANT_ENTER_AFTER_SECONDS = 0.15
    ab.QUANT_RESTORE_AFTER_SECONDS = 0.15
    ab.CONTEXT_RESTORE_AFTER_SECONDS = 0.10
    ab.THREADS_RESTORE_AFTER_SECONDS = 0.05
    ab.CHUNK_RESTORE_AFTER_SECONDS = 0.02
    ab._TICK_MIN_INTERVAL_SECONDS = 0.01
    try:
        return fn()
    finally:
        for key, value in originals.items():
            setattr(ab, key, value)


def _tick_until(balancer, cpu_pct: float, deadline_seconds: float, tick_interval: float = 0.015):
    """Repeatedly ticks with a fixed simulated CPU reading until the
    deadline — the realistic way this balancer is actually driven (a
    poll loop calling tick() every ~15ms-250ms with whatever
    cpu_load_monitor.get_current_cpu() currently reads)."""
    end = time.perf_counter() + deadline_seconds
    last_action = None
    while time.perf_counter() < end:
        action = balancer.tick(cpu_pct)
        if action is not None:
            last_action = action
        time.sleep(tick_interval)
    return last_action


# ============================================================
# PART 1 — cpu_load_monitor.py
# ============================================================
def test_cpu_load_monitor_reports_a_real_reading_immediately_after_start():
    from backend.core.cpu_load_monitor import CPULoadMonitor

    monitor = CPULoadMonitor(poll_interval=0.05)
    monitor.start()
    try:
        value = monitor.get_current_cpu()
        assert isinstance(value, float)
        assert 0.0 <= value <= 100.0
    finally:
        monitor.stop()


def test_cpu_load_monitor_context_manager_starts_and_stops():
    from backend.core.cpu_load_monitor import CPULoadMonitor

    with CPULoadMonitor(poll_interval=0.05) as monitor:
        assert monitor._thread is not None and monitor._thread.is_alive()
    assert monitor._thread is None


# ============================================================
# PART 2 — eligibility gating
# ============================================================
def test_is_eligible_true_for_a_12b_local_cpu_only_model():
    from backend.core.auto_balancer import is_eligible

    assert is_eligible(_model_cfg(params=12_000_000_000), n_gpu_layers=0) is True


def test_is_eligible_false_for_cloud_provider():
    from backend.core.auto_balancer import is_eligible

    assert is_eligible(_model_cfg(provider="openai", params=12_000_000_000), n_gpu_layers=0) is False


def test_is_eligible_true_for_a_7b_cpu_only_model():
    # Universal "local + CPU-only" rule (no size gate anymore) — a 7B
    # model is just as eligible as the original 12B-only scope.
    from backend.core.auto_balancer import is_eligible

    assert is_eligible(_model_cfg(params=7_000_000_000), n_gpu_layers=0) is True


def test_is_eligible_true_for_a_0_5b_cpu_only_model():
    from backend.core.auto_balancer import is_eligible

    assert is_eligible(_model_cfg(params=500_000_000), n_gpu_layers=0) is True


def test_is_eligible_true_for_a_70b_cpu_only_model():
    # A future large model IS eligible when it actually runs CPU-only —
    # eligibility is evaluated on the resolved runtime config, not on
    # the model's size/name (see auto_balancer.py's module docstring).
    from backend.core.auto_balancer import is_eligible

    assert is_eligible(_model_cfg(params=70_000_000_000), n_gpu_layers=0) is True


def test_is_eligible_false_for_a_70b_model_with_gpu_offload():
    # The SAME large model is NOT eligible once real GPU layers are
    # offloaded — thread/context/chunk tuning targets CPU-bound
    # inference specifically.
    from backend.core.auto_balancer import is_eligible

    assert is_eligible(_model_cfg(params=70_000_000_000), n_gpu_layers=40) is False


def test_is_eligible_false_when_gpu_layers_are_offloaded():
    from backend.core.auto_balancer import is_eligible

    assert is_eligible(_model_cfg(params=12_000_000_000), n_gpu_layers=20) is False


def test_is_eligible_false_for_none_model_cfg():
    from backend.core.auto_balancer import is_eligible

    assert is_eligible(None, n_gpu_layers=0) is False


# ============================================================
# PART 3 — tiered throttle sequence under a sustained CPU spike
# ============================================================
def test_cpu_spike_escalates_through_all_four_tiers_in_order():
    from backend.core.auto_balancer import AutoBalancer, TIER_CHUNK, TIER_THREADS, TIER_CONTEXT, TIER_QUANT

    def scenario():
        balancer = AutoBalancer("s1", "test-12b", _model_cfg(), baseline_threads=16, baseline_context=8192)

        # Tier 1 fires immediately once cpu >= WARN_CPU (70) — no sustained-duration gate for this one.
        action1 = balancer.tick(75.0)
        assert action1 is not None and action1.field == "chunk_size" and action1.direction == "throttle"
        assert balancer.current_tier == TIER_CHUNK
        assert balancer.chunk_size == 32

        # Sustained >= HIGH_CPU (80) for > THREADS_ENTER_AFTER_SECONDS → tier 2.
        action2 = _tick_until(balancer, 90.0, deadline_seconds=0.15)
        assert balancer.current_tier >= TIER_THREADS, f"expected tier >= THREADS, got {balancer.current_tier}"
        assert balancer.current_threads == 12  # round(16 * 0.75)

        # Longer sustained >= HIGH_CPU → tier 3.
        _tick_until(balancer, 90.0, deadline_seconds=0.15)
        assert balancer.current_tier >= TIER_CONTEXT, f"expected tier >= CONTEXT, got {balancer.current_tier}"
        assert balancer.current_context == 4096  # round(8192 * 0.5)

        # Sustained >= CRITICAL_CPU (85) → tier 4 (quant — no sibling file exists for this fake model, so this
        # must be recorded as "requested but unavailable", not a fake swap).
        _tick_until(balancer, 95.0, deadline_seconds=0.25)
        assert balancer.current_tier == TIER_QUANT
        assert balancer.current_quant == balancer.baseline_quant, "no sibling file exists — quant must NOT actually change"

        # Idempotency: every tier is already at its floor — further high-CPU ticks must not re-fire.
        history_len_before = len(balancer.history)
        balancer.tick(99.0)
        time.sleep(0.02)
        balancer.tick(99.0)
        assert len(balancer.history) == history_len_before, "no tier left to escalate to — ticking again must be a no-op"

    _with_fast_timings(scenario)


def test_no_changes_when_cpu_stays_under_warn_threshold():
    from backend.core.auto_balancer import AutoBalancer, TIER_NONE

    def scenario():
        balancer = AutoBalancer("s2", "test-12b", _model_cfg(), baseline_threads=16, baseline_context=8192)
        for _ in range(20):
            action = balancer.tick(65.0)  # below WARN_CPU=70 the whole time
            assert action is None
            time.sleep(0.01)
        assert balancer.current_tier == TIER_NONE
        assert balancer.chunk_size == 64
        assert balancer.current_threads == 16
        assert balancer.current_context == 8192

    _with_fast_timings(scenario)


def test_brief_high_cpu_that_never_sustains_does_not_advance_past_tier_1():
    """
    A CPU spike that touches HIGH_CPU only briefly (never sustained past
    THREADS_ENTER_AFTER_SECONDS) must not reach tier 2 — the duration
    gate resets the moment CPU drops back under HIGH_CPU.
    """
    from backend.core.auto_balancer import AutoBalancer, TIER_CHUNK

    def scenario():
        balancer = AutoBalancer("s3", "test-12b", _model_cfg(), baseline_threads=16, baseline_context=8192)
        balancer.tick(72.0)  # crosses WARN_CPU → tier 1
        assert balancer.current_tier == TIER_CHUNK

        # Oscillate above/below HIGH_CPU — never sustained.
        #
        # The sleep must exceed _TICK_MIN_INTERVAL_SECONDS (0.01 under
        # _with_fast_timings), because tick() self-throttles and silently
        # returns without evaluating when called faster than that. At the
        # 0.005 this used to use, roughly every other tick was discarded,
        # and which ones depended on wall-clock timing: if the 72.0 resets
        # were the ones dropped, the balancer saw uninterrupted 82.0 and
        # advanced past tier 1. That made this test pass alone and fail
        # after a neighbour had warmed the machine up.
        for _ in range(10):
            balancer.tick(82.0)
            time.sleep(0.012)
            balancer.tick(72.0)  # drops back below HIGH_CPU each time, resetting the "above high" timer
            time.sleep(0.012)

        assert balancer.current_tier == TIER_CHUNK, "oscillating CPU must never accumulate sustained HIGH_CPU duration"

    _with_fast_timings(scenario)


# ============================================================
# PART 4 — reverse restore sequence
# ============================================================
def test_cpu_drop_restores_all_four_tiers_in_reverse_order():
    from backend.core.auto_balancer import AutoBalancer, TIER_NONE

    def scenario():
        balancer = AutoBalancer("s4", "test-12b", _model_cfg(), baseline_threads=16, baseline_context=8192)

        # Escalate all the way to tier 4 first (quant unavailable, but tier bookkeeping still advances).
        balancer.tick(75.0)
        _tick_until(balancer, 90.0, deadline_seconds=0.15)
        _tick_until(balancer, 90.0, deadline_seconds=0.15)
        _tick_until(balancer, 95.0, deadline_seconds=0.25)
        assert balancer.current_tier == 4

        fields_restored_in_order = []
        end = time.perf_counter() + 1.5
        while balancer.current_tier > TIER_NONE and time.perf_counter() < end:
            action = balancer.tick(30.0)  # well under RESTORE_CPU=60
            if action is not None:
                assert action.direction == "restore"
                fields_restored_in_order.append(action.field)
            time.sleep(0.015)

        assert balancer.current_tier == TIER_NONE, f"expected full restore, stuck at tier {balancer.current_tier}"
        assert fields_restored_in_order == ["quant", "context", "threads", "chunk_size"], (
            f"restore order must be reverse of throttle order, got {fields_restored_in_order}"
        )
        assert balancer.chunk_size == 64
        assert balancer.current_threads == 16
        assert balancer.current_context == 8192
        assert balancer.current_quant == balancer.baseline_quant

    _with_fast_timings(scenario)


def test_restore_never_skips_more_than_one_tier_per_tick():
    from backend.core.auto_balancer import AutoBalancer

    def scenario():
        balancer = AutoBalancer("s5", "test-12b", _model_cfg(), baseline_threads=16, baseline_context=8192)
        balancer.tick(75.0)
        _tick_until(balancer, 90.0, deadline_seconds=0.15)
        tier_before = balancer.current_tier
        assert tier_before >= 2

        action = balancer.tick(30.0)  # first low reading — no sustained duration yet
        assert action is None or balancer.current_tier == tier_before, "a single tick right at the drop must not restore anything yet"

    _with_fast_timings(scenario)


def test_restore_aborts_if_cpu_climbs_back_up_before_the_duration_elapses():
    """Hysteresis: a brief dip below RESTORE_CPU that climbs back up
    before the required sustained duration must not restore anything."""
    from backend.core.auto_balancer import AutoBalancer

    def scenario():
        balancer = AutoBalancer("s6", "test-12b", _model_cfg(), baseline_threads=16, baseline_context=8192)
        balancer.tick(75.0)
        tier_before = balancer.current_tier

        balancer.tick(30.0)  # dips low
        time.sleep(0.01)
        balancer.tick(72.0)  # climbs back above WARN_CPU before CHUNK_RESTORE_AFTER_SECONDS elapses
        assert balancer.current_tier == tier_before, "an aborted restore attempt must leave the tier unchanged"

    _with_fast_timings(scenario)


# ============================================================
# PART 5 — context-fit guard
# ============================================================
def test_context_reduction_is_skipped_when_the_prompt_does_not_fit():
    from backend.core.auto_balancer import AutoBalancer, TIER_THREADS, TIER_CONTEXT

    def scenario():
        # baseline_context=8192, reduced would be 4096 (50%) * 0.8 headroom = 3276 max prompt tokens.
        # A 5000-token prompt estimate does not fit.
        balancer = AutoBalancer("s7", "test-12b", _model_cfg(), baseline_threads=16, baseline_context=8192, prompt_token_estimate=5000)
        balancer.tick(75.0)
        _tick_until(balancer, 90.0, deadline_seconds=0.15)
        assert balancer.current_tier == TIER_THREADS

        # Sustained further — would normally reach CONTEXT tier, but the fit check must block it.
        _tick_until(balancer, 90.0, deadline_seconds=0.2)
        assert balancer.current_tier == TIER_THREADS, "context tier must be skipped when the prompt doesn't fit in the reduced size"
        assert balancer.current_context == 8192

    _with_fast_timings(scenario)


def test_context_reduction_proceeds_when_the_prompt_fits():
    from backend.core.auto_balancer import AutoBalancer, TIER_CONTEXT

    def scenario():
        balancer = AutoBalancer("s8", "test-12b", _model_cfg(), baseline_threads=16, baseline_context=8192, prompt_token_estimate=100)
        balancer.tick(75.0)
        _tick_until(balancer, 90.0, deadline_seconds=0.15)
        _tick_until(balancer, 90.0, deadline_seconds=0.15)
        assert balancer.current_tier >= TIER_CONTEXT
        assert balancer.current_context == 4096

    _with_fast_timings(scenario)


# ============================================================
# PART 6 — quant sibling detection (the one case where a real file exists)
# ============================================================
def test_quant_downgrade_applies_when_a_lower_quant_sibling_file_exists():
    from backend.core.auto_balancer import AutoBalancer, TIER_QUANT

    def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            q5_path = os.path.join(tmp, "nemo-12b.Q5_K_M.gguf")
            q4_path = os.path.join(tmp, "nemo-12b.Q4_K_M.gguf")
            open(q5_path, "w").close()
            open(q4_path, "w").close()

            cfg = _model_cfg(path=q5_path, quant="Q5_K_M")
            balancer = AutoBalancer("s9", "test-12b", cfg, baseline_threads=16, baseline_context=8192)

            balancer.tick(75.0)
            _tick_until(balancer, 90.0, deadline_seconds=0.15)
            _tick_until(balancer, 90.0, deadline_seconds=0.15)
            _tick_until(balancer, 95.0, deadline_seconds=0.25)

            assert balancer.current_tier == TIER_QUANT
            assert balancer.current_quant == "Q4_K_M", "a real sibling file exists — the downgrade must actually apply"
            assert balancer.quant_override_path == q4_path

    _with_fast_timings(scenario)


def test_quant_downgrade_recorded_as_unavailable_without_a_sibling_file():
    from backend.core.auto_balancer import AutoBalancer, TIER_QUANT

    def scenario():
        balancer = AutoBalancer("s10", "test-12b", _model_cfg(path="/fake/does/not/exist.Q5_K_M.gguf"), baseline_threads=16, baseline_context=8192)
        balancer.tick(75.0)
        _tick_until(balancer, 90.0, deadline_seconds=0.15)
        _tick_until(balancer, 90.0, deadline_seconds=0.15)
        _tick_until(balancer, 95.0, deadline_seconds=0.25)

        assert balancer.current_tier == TIER_QUANT
        assert balancer.current_quant == "Q5_K_M", "must not claim a downgrade that never actually happened"
        assert balancer.quant_override_path is None

    _with_fast_timings(scenario)


# ============================================================
# PART 7 — idempotency
# ============================================================
def test_tick_self_throttles_and_does_not_double_apply_within_the_min_interval():
    from backend.core.auto_balancer import AutoBalancer

    def scenario():
        balancer = AutoBalancer("s11", "test-12b", _model_cfg(), baseline_threads=16, baseline_context=8192)
        action1 = balancer.tick(75.0)
        action2 = balancer.tick(75.0)  # called immediately again, well within _TICK_MIN_INTERVAL_SECONDS
        assert action1 is not None
        assert action2 is None, "a second tick() faster than the min interval must be a no-op, not a duplicate action"

    _with_fast_timings(scenario)


# ============================================================
# PART 8 — no changes for non-12B models / cloud providers, end to end
# via execution_pipeline.py
# ============================================================
def test_execution_pipeline_never_creates_a_balancer_for_a_cloud_provider():
    from backend.core.provider_router import ProviderRouter
    import backend.core.execution_pipeline as ep
    from backend.core import auto_balancer

    class FakeCloudProvider:
        provider_name = "openai"
        def run(self, request):
            return "cloud response"

    original_resolve = ProviderRouter.resolve
    original_get_model = ep.get_model
    ProviderRouter.resolve = lambda self, model_id, prompt=None, *a: (FakeCloudProvider(), None)
    ep.get_model = lambda model_id: None  # cloud: no local model_cfg

    active_before = dict(auto_balancer._active_sessions)
    try:
        class FakeRequest:
            model_id = None
            prompt = "hi"
            allow_override = False

        result = ep.run_pipeline(FakeRequest())
        assert result.ok is True
        assert dict(auto_balancer._active_sessions) == active_before, "a cloud request must never create a balance session"
    finally:
        ProviderRouter.resolve = original_resolve
        ep.get_model = original_get_model


def test_execution_pipeline_creates_a_balancer_for_a_7b_cpu_only_model_too():
    # Universal auto-balance: a 7B CPU-only model now gets a session
    # exactly like a 12B one used to be the only case that did.
    from backend.core.provider_router import ProviderRouter
    import backend.core.execution_pipeline as ep
    from backend.core import auto_balancer

    class FakeProfile:
        n_threads = 8
        max_ctx = 4096
        n_gpu_layers = 0

    class FakeDecision:
        requires_warning = False
        profile = FakeProfile()

    class FakeLocalProvider:
        provider_name = "local"
        def run(self, request):
            return "small model response"

    original_resolve = ProviderRouter.resolve
    original_get_model = ep.get_model
    original_evaluate = ep.evaluate_safety
    ProviderRouter.resolve = lambda self, model_id, prompt=None, *a: (FakeLocalProvider(), "small-7b-model")
    ep.get_model = lambda model_id: _model_cfg(model_id="small-7b-model", params=7_000_000_000)
    ep.evaluate_safety = lambda model_cfg: FakeDecision()

    try:
        class FakeRequest:
            model_id = "small-7b-model"
            prompt = "hi"
            allow_override = False

        result = ep.run_pipeline(FakeRequest())
        assert result.ok is True
        assert auto_balancer.get_active_balancer("small-7b-model") is not None, \
            "a 7B CPU-only model must now get a balance session under the universal rule"
    finally:
        ProviderRouter.resolve = original_resolve
        ep.get_model = original_get_model
        ep.evaluate_safety = original_evaluate


def test_execution_pipeline_creates_a_balancer_for_an_eligible_12b_cpu_only_request():
    from backend.core.provider_router import ProviderRouter
    import backend.core.execution_pipeline as ep
    from backend.core import auto_balancer

    class FakeProfile:
        n_threads = 16
        max_ctx = 8192
        n_gpu_layers = 0

    class FakeDecision:
        requires_warning = False
        profile = FakeProfile()

    class FakeLocalProvider:
        provider_name = "local"
        def run(self, request):
            return "12b response"

    original_resolve = ProviderRouter.resolve
    original_get_model = ep.get_model
    original_evaluate = ep.evaluate_safety
    ProviderRouter.resolve = lambda self, model_id, prompt=None, *a: (FakeLocalProvider(), "test-12b-eligible")
    ep.get_model = lambda model_id: _model_cfg(model_id="test-12b-eligible", params=12_000_000_000)
    ep.evaluate_safety = lambda model_cfg: FakeDecision()

    try:
        class FakeRequest:
            model_id = "test-12b-eligible"
            prompt = "hi"
            allow_override = False

        result = ep.run_pipeline(FakeRequest())
        assert result.ok is True
        assert auto_balancer.get_active_balancer("test-12b-eligible") is not None, "an eligible 12B CPU-only request must create a balance session"
    finally:
        ProviderRouter.resolve = original_resolve
        ep.get_model = original_get_model
        ep.evaluate_safety = original_evaluate


# ============================================================
# PART 9 — model_loader.py override application
# ============================================================
def test_model_loader_applies_active_thread_and_context_overrides():
    """
    Verifies model_loader.load_model() actually reads
    auto_balancer.get_effective_overrides() and applies them to the
    profile used to construct Llama() — without invoking real llama.cpp
    (Llama() itself is monkeypatched to a recorder).
    """
    import backend.core.model_loader as ml
    from backend.core import auto_balancer

    model_id = "test-12b-loader"
    cfg = _model_cfg(model_id=model_id, params=12_000_000_000)

    original_get_model = ml.get_model
    original_evaluate = ml.evaluate_safety
    original_llama = ml.Llama

    class FakeProfile:
        n_threads = 16
        max_ctx = 8192
        n_gpu_layers = 0

    class FakeDecision:
        requires_warning = False
        profile = FakeProfile()

    constructed_with = {}

    class FakeLlama:
        def __init__(self, model_path, n_ctx, n_gpu_layers, n_threads, verbose):
            constructed_with.update({"model_path": model_path, "n_ctx": n_ctx, "n_threads": n_threads})

    ml.get_model = lambda mid: cfg if mid == model_id else None
    ml.evaluate_safety = lambda model_cfg: FakeDecision()
    ml.Llama = FakeLlama

    session_id = auto_balancer.next_session_id()
    balancer = auto_balancer.start_session(session_id, model_id, cfg, baseline_threads=16, baseline_context=8192)
    balancer.tick(75.0)  # tier 1 only — no thread override yet
    assert auto_balancer.get_effective_overrides(model_id) is None, "tier 1 alone must not produce a threads/context override"

    # Directly sets tier 2 rather than racing real sustained-duration
    # timers (already covered by the dedicated tier-escalation tests
    # above) — this test's actual subject is model_loader.py's override
    # APPLICATION, not auto_balancer's own timing logic.
    balancer.current_tier = 2
    balancer.current_threads = 12

    try:
        assert balancer.current_tier >= 2
        overrides = auto_balancer.get_effective_overrides(model_id)
        assert overrides is not None and overrides["n_threads"] == 12

        loader = ml.ModelLoader()
        loader.resolver = type("FakeResolver", (), {"resolve_model_path": lambda self, mid: cfg["path"]})()
        loader.load_model(model_id)

        assert constructed_with["n_threads"] == 12, "model_loader must apply the active auto-balancer thread override"
    finally:
        ml.get_model = original_get_model
        ml.evaluate_safety = original_evaluate
        ml.Llama = original_llama
        with auto_balancer._sessions_lock:
            auto_balancer._active_sessions.pop(model_id, None)


def test_model_loader_forces_reload_when_override_changes():
    import backend.core.model_loader as ml
    from backend.core import auto_balancer

    model_id = "test-12b-reload"
    cfg = _model_cfg(model_id=model_id, params=12_000_000_000)

    original_get_model = ml.get_model
    original_evaluate = ml.evaluate_safety
    original_llama = ml.Llama

    class FakeProfile:
        n_threads = 16
        max_ctx = 8192
        n_gpu_layers = 0

    class FakeDecision:
        requires_warning = False
        profile = FakeProfile()

    construct_count = {"n": 0}

    class FakeLlama:
        def __init__(self, model_path, n_ctx, n_gpu_layers, n_threads, verbose):
            construct_count["n"] += 1

    ml.get_model = lambda mid: cfg if mid == model_id else None
    ml.evaluate_safety = lambda model_cfg: FakeDecision()
    ml.Llama = FakeLlama

    try:
        loader = ml.ModelLoader()
        loader.resolver = type("FakeResolver", (), {"resolve_model_path": lambda self, mid: cfg["path"]})()

        loader.load_model(model_id)
        assert construct_count["n"] == 1
        loader.load_model(model_id)  # no override yet — must reuse the cached instance
        assert construct_count["n"] == 1, "no active override → cached instance must be reused"

        session_id = auto_balancer.next_session_id()
        balancer = auto_balancer.start_session(session_id, model_id, cfg, baseline_threads=16, baseline_context=8192)
        balancer.current_tier = 2
        balancer.current_threads = 12  # simulate an already-escalated session without waiting on real timers

        loader.load_model(model_id)
        assert construct_count["n"] == 2, "an active override that differs from the cached instance must force a reload"
        loader.load_model(model_id)
        assert construct_count["n"] == 2, "once reloaded WITH the override, the next call must reuse it again"
    finally:
        ml.get_model = original_get_model
        ml.evaluate_safety = original_evaluate
        ml.Llama = original_llama
        with auto_balancer._sessions_lock:
            auto_balancer._active_sessions.pop(model_id, None)


# ============================================================
# PART 10 — streaming_engine_v2.py live chunk-size integration
# ============================================================
def test_streaming_engine_v2_applies_live_chunk_size_from_balancer():
    from backend.core.streaming_engine_v2 import StreamingEngineV2
    from backend.core.auto_balancer import AutoBalancer
    from backend.core.cpu_load_monitor import CPULoadMonitor

    def scenario():
        balancer = AutoBalancer("stream-s1", "test-12b", _model_cfg(), baseline_threads=16, baseline_context=8192)

        class FixedCpuMonitor:
            def get_current_cpu(self):
                return 90.0  # sustained high the whole stream

            def stop(self):
                pass

        class SlowFakeProvider:
            def stream(self, request, callback):
                for _ in range(80):
                    callback({"content": "x"})
                    time.sleep(0.01)

        packets = []
        engine = StreamingEngineV2()
        handle = engine.stream(object(), SlowFakeProvider(), packets.append, balancer=balancer, cpu_monitor=FixedCpuMonitor(), flush_max_chars=1000, flush_interval=1.0)
        assert handle.wait(timeout=10)

        assert balancer.chunk_size == 32, "sustained high CPU during an active stream must reduce the live chunk size"
        token_packets = [p for p in packets if p["type"] == "stream_token"]
        assert "".join(p["token"] for p in token_packets) == "x" * 80

    _with_fast_timings(scenario)


# ============================================================
# PART 11 — diagnostics + logging
# ============================================================
def test_diagnostics_snapshot_shape_and_thresholds():
    from backend.core.auto_balancer import diagnostics_snapshot, WARN_CPU, HIGH_CPU, CRITICAL_CPU, RESTORE_CPU

    snapshot = diagnostics_snapshot()
    assert "active_sessions" in snapshot
    assert "recent_actions" in snapshot
    assert snapshot["thresholds"] == {
        "warn_cpu": WARN_CPU, "high_cpu": HIGH_CPU, "critical_cpu": CRITICAL_CPU, "restore_cpu": RESTORE_CPU,
    }


def test_diagnostics_snapshot_reflects_an_active_session():
    from backend.core.auto_balancer import AutoBalancer, start_session, diagnostics_snapshot, next_session_id, _sessions_lock, _active_sessions

    session_id = next_session_id()
    model_id = "test-12b-diag"
    balancer = start_session(session_id, model_id, _model_cfg(model_id=model_id), baseline_threads=16, baseline_context=8192)
    balancer.tick(75.0)

    try:
        snapshot = diagnostics_snapshot()
        assert model_id in snapshot["active_sessions"]
        entry = snapshot["active_sessions"][model_id]
        assert entry["tier_name"] == "chunk"
        assert entry["adjustments"]["chunk_size"] == 32
        assert entry["baseline"]["threads"] == 16
        assert entry["last_change_at"] is not None
    finally:
        with _sessions_lock:
            _active_sessions.pop(model_id, None)


def test_rest_diagnostics_autobalance_endpoint():
    from fastapi.testclient import TestClient
    from backend.rest.server import app

    resp = TestClient(app).get("/v1/diagnostics/autobalance")
    assert resp.status_code == 200
    body = resp.json()
    assert "active_sessions" in body and "thresholds" in body


def test_ipc_diagnostics_autobalance_request():
    from backend import ipc_router, ipc_schema as schema

    result = ipc_router.dispatch({"type": schema.DIAGNOSTICS_AUTOBALANCE_REQUEST, "payload": {}})
    assert result["type"] == schema.DIAGNOSTICS_AUTOBALANCE_RESULT
    assert "active_sessions" in result["payload"]


def test_balance_actions_are_logged_via_unified_log():
    """
    Verifies every throttle/restore action actually calls
    backend.logger.log() (the unified cross-runtime logging server
    client) — monkeypatches it rather than standing up a real logging
    server, matching this project's established test convention for
    unified_log call sites.
    """
    import backend.core.auto_balancer as ab

    logged_calls = []
    original_log = ab.unified_log
    ab.unified_log = lambda subsystem, level, message, context=None: logged_calls.append((subsystem, level, message, context))

    def scenario():
        balancer = ab.AutoBalancer("log-s1", "test-12b", _model_cfg(), baseline_threads=16, baseline_context=8192)
        balancer.tick(75.0)

    try:
        _with_fast_timings(scenario)
        assert len(logged_calls) >= 1
        subsystem, level, message, context = logged_calls[0]
        assert subsystem == "auto_balancer"
        assert level == "WARNING"  # throttle actions log at WARNING
        assert context["model_id"] == "test-12b"
    finally:
        ab.unified_log = original_log


def test_history_is_visible_in_diagnostics_after_session_ends():
    from backend.core.auto_balancer import AutoBalancer, diagnostics_snapshot, _history

    history_len_before = len(_history)

    def scenario():
        balancer = AutoBalancer("log-s2", "test-12b", _model_cfg(), baseline_threads=16, baseline_context=8192)
        balancer.tick(75.0)

    _with_fast_timings(scenario)

    snapshot = diagnostics_snapshot()
    assert len(_history) > history_len_before
    assert any(a["model_id"] == "test-12b" and a["field"] == "chunk_size" for a in snapshot["recent_actions"])


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
