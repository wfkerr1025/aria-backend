# backend/core/execution_pipeline.py

"""
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
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .provider_router import ProviderRouter
from .mode_manager import ModeManager
from .model_registry import get_model
from .safety_manager import evaluate_safety
from .provider_interface import ProviderAdapter
from .streaming_engine_v2 import StreamingEngineV2, StreamHandle
from . import perf_profiler
from . import auto_balancer
from .cpu_load_monitor import CPULoadMonitor
from .errors import PipelineError, ErrorCode

from backend.logger import log as unified_log
from logger import get_logger

logger = get_logger(__name__)


@dataclass
class PipelineResult:
    ok: bool
    text: Optional[str] = None
    stream_handle: Optional[StreamHandle] = None
    model_id: Optional[str] = None
    provider_name: Optional[str] = None
    location: Optional[str] = None
    safety_severity: Optional[str] = None
    error: Optional[Dict[str, Any]] = None
    elapsed_seconds: float = 0.0


_stream_engine_v2 = StreamingEngineV2()


def _resolve(request: Any) -> tuple:
    """Pre-execution: provider selection, exactly ProviderRouter's own
    resolve() — the same logic ProviderAdapter/StreamingEngine v1 already
    route through, not a second implementation of mode-aware routing."""
    router = ProviderRouter(ModeManager())
    model_id = getattr(request, "model_id", None)
    prompt = getattr(request, "prompt", None) or ""
    provider, resolved_model_id = router.resolve(model_id, prompt)
    return provider, resolved_model_id


def _extract_prompt_text(request: Any) -> str:
    """Same shape-tolerant extraction as backend.core.streaming_engine's
    own helper — duplicated rather than imported to keep this module's
    only dependency on streaming_engine.py at zero (v1 and v2 are
    deliberately decoupled; see streaming_engine_v2.py's docstring)."""
    prompt = getattr(request, "prompt", None)
    if prompt:
        return prompt
    messages = getattr(request, "messages", None) or []
    last_user_content = ""
    for message in reversed(messages):
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        if not last_user_content and content:
            last_user_content = content
        if role == "user" and content:
            return content
    return last_user_content


def _check_safety(model_cfg: Optional[dict], allow_override: bool):
    """Pre-execution: safety gate. Returns (block_dict_or_None, decision_or_None).
    Only meaningful for a resolved LOCAL model — a cloud provider has no
    RAM/VRAM projection to gate on, exactly matching how
    handlers.py/rest/router.py already treat this."""
    if model_cfg is None:
        return None, None

    decision = evaluate_safety(model_cfg)
    if decision.requires_warning and not allow_override:
        return {
            "severity": decision.severity,
            "message": decision.message,
            "projected_cpu_pct": decision.projected_cpu_pct,
            "projected_ram_pct": decision.projected_ram_pct,
            "projected_vram_pct": decision.projected_vram_pct,
        }, decision
    return None, decision


def _start_balancer_if_eligible(resolved_model_id: Optional[str], model_cfg: Optional[dict], decision: Any, request: Any):
    """
    Auto-balancer session setup — a strict no-op (returns (None, None))
    for anything outside this feature's scope (cloud providers, or any
    local model that resolved to GPU offload rather than a CPU-only
    run). See auto_balancer.py's module docstring for the full
    eligibility/scope rationale — this now applies to every local
    CPU-only model, not just a single size bucket.
    """
    if decision is None or model_cfg is None or resolved_model_id is None:
        return None, None
    if not auto_balancer.is_eligible(model_cfg, decision.profile.n_gpu_layers):
        return None, None

    prompt_text = _extract_prompt_text(request)
    prompt_token_estimate = len(prompt_text) // 4  # rough chars-per-token heuristic, documented in auto_balancer.py

    session_id = auto_balancer.next_session_id()
    balancer = auto_balancer.start_session(
        session_id, resolved_model_id, model_cfg,
        baseline_threads=decision.profile.n_threads, baseline_context=decision.profile.max_ctx,
        prompt_token_estimate=prompt_token_estimate,
    )
    monitor = CPULoadMonitor()
    monitor.start()
    return balancer, monitor


def run_pipeline(request: Any, send_packet: Optional[Callable[[dict], None]] = None) -> PipelineResult:
    """
    The full pipeline, non-streaming variant when send_packet is None
    (blocks and returns the complete text) or streaming when it's given
    (returns immediately with a StreamHandle the caller can cancel()).
    """
    start = time.perf_counter()
    allow_override = bool(getattr(request, "allow_override", False))

    with perf_profiler.timed("execution_pipeline.run_pipeline.resolve"):
        provider_raw, resolved_model_id = _resolve(request)

    if provider_raw is None:
        elapsed = time.perf_counter() - start
        error = PipelineError(code=ErrorCode.PIPELINE_NO_PROVIDER, message="No available provider for this request.")
        unified_log("execution_pipeline", "ERROR", "No provider resolved", {"model_id": getattr(request, "model_id", None)})
        return PipelineResult(ok=False, error=error.to_dict(), elapsed_seconds=elapsed)

    model_cfg = get_model(resolved_model_id) if resolved_model_id else None
    location = "local" if model_cfg else ("cloud" if resolved_model_id is None else "local")

    with perf_profiler.timed("execution_pipeline.run_pipeline.safety_check"):
        safety_block, decision = _check_safety(model_cfg, allow_override)

    if safety_block is not None:
        elapsed = time.perf_counter() - start
        error = PipelineError(
            code=ErrorCode.PIPELINE_SAFETY_BLOCKED, message=safety_block["message"],
            context={k: v for k, v in safety_block.items() if k != "message"},
        )
        unified_log("execution_pipeline", "WARNING", f"Safety {safety_block['severity']} for {resolved_model_id}", safety_block)
        return PipelineResult(
            ok=False, model_id=resolved_model_id, safety_severity=safety_block["severity"],
            error=error.to_dict(), elapsed_seconds=elapsed,
        )

    # Wraps provider_raw DIRECTLY — the exact object ProviderRouter.resolve()
    # just returned — rather than re-looking it up by name via
    # get_unified_provider()/provider_registry.get_provider(). Those two
    # happen to be the same singleton in real usage (ProviderRouter
    # itself sources every provider from provider_registry.PROVIDERS),
    # but re-fetching by name is still the wrong operation: it silently
    # discards whatever ProviderRouter actually resolved in favor of a
    # fresh lookup, which breaks the moment provider_raw ISN'T a
    # registry entry (already caught this in testing — a fake/mocked
    # provider used for a unit test got silently ignored in favor of the
    # real "local" provider from the registry).
    provider_name = provider_raw.provider_name if hasattr(provider_raw, "provider_name") else ("local" if location == "local" else "unknown")
    provider = ProviderAdapter(provider_name=provider_name, _provider=provider_raw, model_id=resolved_model_id)

    # Universal local-CPU-only auto-balancing (see backend.core.
    # auto_balancer) — a strict no-op tuple (None, None) for every
    # request outside that scope (cloud, or GPU-offloaded local),
    # verified by is_eligible() inside this helper.
    balancer, cpu_monitor = _start_balancer_if_eligible(resolved_model_id, model_cfg, decision, request)

    if send_packet is not None:
        with perf_profiler.timed("execution_pipeline.run_pipeline.stream_dispatch"):
            handle = _stream_engine_v2.stream(request, provider, send_packet, balancer=balancer, cpu_monitor=cpu_monitor)
        elapsed = time.perf_counter() - start
        unified_log("execution_pipeline", "INFO", "Streaming turn dispatched", {"model_id": resolved_model_id, "provider": provider.provider_name, "auto_balanced": balancer is not None})
        return PipelineResult(ok=True, stream_handle=handle, model_id=resolved_model_id, provider_name=provider.provider_name, location=location, elapsed_seconds=elapsed)

    # Non-streaming: provider.infer() blocks synchronously for the whole
    # turn, so — unlike the streaming path, which ticks the balancer from
    # its own already-running consumer loop — a small dedicated thread
    # drives tick() here for the duration of the call. Any threads/
    # context/quant adjustment it decides on can't affect THIS already-
    # in-flight call (see auto_balancer.py's docstring on why), but is in
    # place for model_loader.py to pick up on this model's next load.
    stop_ticking = None
    ticker_thread = None
    if balancer is not None and cpu_monitor is not None:
        import threading as _threading
        stop_ticking = _threading.Event()

        def _tick_loop():
            while not stop_ticking.is_set():
                balancer.tick(cpu_monitor.get_current_cpu())
                stop_ticking.wait(0.1)

        ticker_thread = _threading.Thread(target=_tick_loop, daemon=True, name=f"auto-balance-tick-{resolved_model_id}")
        ticker_thread.start()

    try:
        with perf_profiler.timed("execution_pipeline.run_pipeline.infer"):
            text = provider.infer(request)
    except Exception as e:
        elapsed = time.perf_counter() - start
        error = PipelineError(code=ErrorCode.HANDLER_EXCEPTION, message=str(e))
        unified_log("execution_pipeline", "ERROR", f"infer() failed: {e}", {"model_id": resolved_model_id})
        return PipelineResult(ok=False, model_id=resolved_model_id, error=error.to_dict(), elapsed_seconds=elapsed)
    finally:
        if stop_ticking is not None:
            stop_ticking.set()
            ticker_thread.join(timeout=1.0)
        if cpu_monitor is not None:
            cpu_monitor.stop()

    elapsed = time.perf_counter() - start
    unified_log("execution_pipeline", "INFO", "Turn completed", {"model_id": resolved_model_id, "provider": provider.provider_name, "elapsed_seconds": round(elapsed, 3), "auto_balanced": balancer is not None})
    return PipelineResult(ok=True, text=text, model_id=resolved_model_id, provider_name=provider.provider_name, location=location, elapsed_seconds=elapsed)
