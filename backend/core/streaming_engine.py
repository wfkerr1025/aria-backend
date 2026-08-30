# backend/llm/streaming_engine.py

from __future__ import annotations

import time

from backend.core.provider_router import ProviderRouter
from backend.core.mode_manager import ModeManager
from backend.core import model_selector
from backend.core import perf_profiler
from backend.core import packet_validation
from backend import ipc_schema as schema

from backend.logger import log as unified_log

from logger import get_logger

logger = get_logger(__name__)


def _describe_provider(provider, resolved_model_id):
    """
    Derive ("local"|"cloud"|"unknown", provider_name) for the
    active_model_changed broadcast below. Provider instances don't carry
    their own name as an attribute — this derives it from the instance's
    module name using the exact same filename -> name transform
    backend.llm.providers.provider_registry.load_providers() already
    uses to register it in the first place (strip a trailing
    "_provider"/"_wrapper"), so it can never drift out of sync with how
    providers are actually looked up.
    """
    if provider is None:
        return "unknown", None

    module_name = type(provider).__module__.rsplit(".", 1)[-1]
    provider_name = module_name.replace("_provider", "").replace("_wrapper", "")
    location = "local" if provider_name == "local" else "cloud"
    return location, provider_name


def _extract_history(request):
    """The conversation, for routing only.

    _extract_prompt_text returns the LAST user message, which is the
    right prompt and the wrong basis for classifying a follow-up. "ok, I
    need you to add some things to the inventory" names no file, so on
    its own it reads as ordinary chat -- and the ladder's role floor left
    it on a chat model while the file it was about went unchanged. The
    message before it said "created player_inventory.cs".
    """
    return list(getattr(request, "messages", None) or ())


def _extract_prompt_text(request) -> str:
    """
    Pull the text to hand to routing/classification (AutoSelector) out of
    a request object. Requests may carry a flat `.prompt` string (legacy
    callers) or a `.messages` list of InferenceMessage/dict (the shape
    backend.core.local_inference_engine.InferenceRequest actually uses) —
    without this, requests built from `.messages` always routed on an
    empty string.
    """
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


class StreamingEngine:
    """
    Unified streaming engine.
    Works with ANY provider (local or cloud) via ProviderRouter.
    Handles:
        - stream_start
        - stream_token
        - stream_end
        - stream_error
    """

    def __init__(self):
        logger.debug("Initializing StreamingEngine")
        self.mode_manager = ModeManager()
        self.provider_router = ProviderRouter(self.mode_manager)
        logger.debug("StreamingEngine initialized successfully")

    # ---------------------------------------------------------
    # Streaming entry point
    # ---------------------------------------------------------
    def stream(self, request, send_packet):
        request_id = id(request)
        model_id = getattr(request, "model_id", None)
        prompt = _extract_prompt_text(request)

        logger.debug("stream() called → model_id=%s, prompt_len=%d", model_id, len(prompt))

        # Real per-turn timing (time-to-first-token, total token count,
        # total duration) — recorded into perf_profiler regardless of
        # outcome (see the finally block at the end of this method), so
        # /v1/diagnostics/performance and diagnostics_performance_request
        # can show actual observed streaming behavior, not just
        # estimate_speed()'s pre-flight guess.
        stream_start_time = time.perf_counter()
        first_token_time = None
        token_count = 0

        # Start event
        logger.debug("Sending stream_start packet")
        send_packet({
            "type": "stream_start",
            "modelId": model_id,
            "requestId": request_id
        })

        try:
            # ---------------------------------------------------------
            # Resolve provider using ProviderRouter + ModeManager logic
            # (Local / Cloud / Automatic Model Routing)
            # ---------------------------------------------------------
            logger.debug("Resolving provider via ProviderRouter")
            provider, resolved_model_id = self.provider_router.resolve(
                model_id, prompt, _extract_history(request))

            if provider is None:
                logger.debug("ERROR: No available provider")
                raise RuntimeError("No available provider.")

            location, provider_name = _describe_provider(provider, resolved_model_id)

            # ---------------------------------------------------------
            # Batch 2 — backend.core.model_selector is the single source
            # of truth for "which model backs this turn", for both local
            # and cloud. Before this, a cloud turn's resolved_model_id
            # was always None (the provider picked its own model at the
            # HTTP layer) — every cloud wrapper (backend/llm/providers/
            # *_wrapper.py) sends request.model_id straight to the real
            # API as the "model" field, so every such call actually sent
            # "model": None. select_cloud_model()/select_local_model()
            # give this turn a REAL, named model instead.
            #
            # A resolution failure here aborts the turn with a
            # structured stream_error instead of ever emitting
            # active_model_changed with a null/lying field — this should
            # be rare in practice, since backend.core.routing_guard
            # already requires a successful model_selector resolution at
            # SWITCH time; reaching a failure here means that state went
            # stale mid-session (e.g. a cloud provider's key was deleted
            # after the mode was already set to use it).
            # ---------------------------------------------------------
            if location == "cloud":
                selection = model_selector.select_cloud_model(provider_name)
            else:
                selection = model_selector.select_local_model(resolved_model_id)

            if isinstance(selection, model_selector.SelectionError):
                logger.error(f"streaming_engine: model selection failed — {selection.code}: {selection.message}")
                unified_log("streaming_engine", "ERROR", "Model selection failed — aborting turn", {
                    "code": selection.code, "message": selection.message, "location": location,
                })
                send_packet({
                    "type": "stream_error",
                    "modelId": resolved_model_id,
                    "requestId": request_id,
                    "message": selection.message,
                    "code": selection.code,
                })
                return

            resolved_model_id = selection.model_id
            display_name = selection.display_name
            provider_display_name = selection.provider_display_name

            logger.debug(
                f"Provider resolved → provider={provider}, model={resolved_model_id}"
            )

            # The real per-turn model id — cloud wrappers send
            # request.model_id straight to the live API (see the block
            # comment above); local_provider.py's own RAM-aware
            # auto-switch (_resolve_model_id()) explicitly documents
            # expecting this to already be ProviderRouter's resolved
            # choice, not the raw original request.
            request.model_id = resolved_model_id

            # ---------------------------------------------------------
            # Active-model broadcast — no dedicated "which model
            # actually got picked for this turn" notification existed
            # before this: a frontend could only infer it indirectly
            # from stream_token packets' modelId field, which tells it
            # nothing about whether that model is local or cloud, or
            # which cloud provider. Sent once, right after resolution,
            # before any tokens — never changes stream_start/
            # stream_token/stream_end's own shape or timing.
            # ---------------------------------------------------------
            active_model_packet = {
                "type": schema.ACTIVE_MODEL_CHANGED,
                "modelId": resolved_model_id,
                "displayName": display_name,
                "requestId": request_id,
                "location": location,
                "provider": provider_name,
                "providerDisplayName": provider_display_name,
            }
            validation_error = packet_validation.validate_active_model_changed_payload(active_model_packet)
            if validation_error:
                # Should be unreachable — model_selector just returned a
                # ModelInfo, which by construction has non-null
                # model_id/display_name. Defense-in-depth only.
                logger.error(f"streaming_engine: active_model_changed failed validation — {validation_error}")
                unified_log("streaming_engine", "ERROR", "active_model_changed blocked by validation", {
                    "error": validation_error, "packet": active_model_packet,
                })
            else:
                send_packet(active_model_packet)

            # ---------------------------------------------------------
            # Provider streaming callback
            # ---------------------------------------------------------
            def callback(chunk):
                nonlocal first_token_time, token_count
                token = chunk.get("content", "")

                if first_token_time is None:
                    first_token_time = time.perf_counter()
                token_count += 1

                # %-style, not an f-string: this runs once per token, and
                # an f-string builds its formatted result eagerly even
                # when the debug logger is disabled — %-style formatting
                # only happens if the log record is actually emitted.
                logger.debug("Streaming token received (len=%d)", len(token))
                unified_log("streaming_engine", "DEBUG", "stream_token", {
                    "model_id": resolved_model_id,
                    "request_id": request_id,
                    "token": token,
                })

                send_packet({
                    "type": "stream_token",
                    "modelId": resolved_model_id,
                    "requestId": request_id,
                    "token": token,
                })

            # ---------------------------------------------------------
            # Stream tokens from provider
            # ---------------------------------------------------------
            if hasattr(provider, "stream"):
                logger.debug("Provider supports streaming → invoking provider.stream()")
                provider.stream(request, callback)
            else:
                logger.debug("Provider does NOT support streaming → using fallback run()")
                result = provider.run(request)
                callback({"type": "chat_stream", "content": result})

            # End event
            logger.debug("Sending stream_end packet")
            send_packet({
                "type": "stream_end",
                "modelId": resolved_model_id,
                "requestId": request_id
            })

        except Exception as e:
            logger.debug(f"ERROR during streaming → {e}")
            unified_log("streaming_engine", "ERROR", f"Streaming exception: {e}", {
                "model_id": model_id,
                "request_id": request_id,
            })

            # Error event
            send_packet({
                "type": "stream_error",
                "modelId": model_id,
                "requestId": request_id,
                "message": str(e)
            })

        finally:
            # Real observed streaming performance — see this method's
            # top for why this feeds /v1/diagnostics/performance instead
            # of only ever having estimate_speed()'s pre-flight guess to
            # go on. Recorded regardless of success/failure so a
            # systemic slowdown (or a provider that errors after
            # emitting some tokens) still shows up.
            total_elapsed_ms = (time.perf_counter() - stream_start_time) * 1000.0
            perf_profiler.record("streaming_engine.stream.total", total_elapsed_ms)
            if first_token_time is not None:
                ttft_ms = (first_token_time - stream_start_time) * 1000.0
                perf_profiler.record("streaming_engine.stream.time_to_first_token", ttft_ms)
            if token_count > 0 and total_elapsed_ms > 0:
                observed_tokens_per_sec = token_count / (total_elapsed_ms / 1000.0)
                perf_profiler.record("streaming_engine.stream.observed_tokens_per_sec", observed_tokens_per_sec)
