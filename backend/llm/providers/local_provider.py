# backend/llm/providers/local_provider.py

import asyncio

from backend.core.model_loader import ModelLoader
from backend.core import model_registry

from backend.logger import log as unified_log

from logger import get_logger

logger = get_logger(__name__)

# The prompt below is a raw text transcript ("role: content\n" per turn),
# not a real chat template with special tokens — nothing tells the model
# to stop after playing ARIA's turn. Left alone, it will happily keep
# generating, inventing the next "user: ..." message and replying to its
# own fabrication (a real, observed failure mode, not a hypothetical
# one). These stop sequences make llama.cpp itself halt generation the
# instant it starts to emit a new turn header, which is strictly better
# than filtering it out client-side: the tokens are never generated, not
# just hidden.
TURN_BOUNDARY_STOP_SEQUENCES = [
    "\nuser:", "\nUser:", "\nUSER:",
    "\nsystem:", "\nSystem:", "\nSYSTEM:",
]

# ============================================================
# LOAD-SURVIVAL FALLBACK CHAIN
#
# request.model_id is the Auto Selector's choice — complexity_router /
# task_classifier / success_predictor already decided this is the right
# model for THIS specific request — and is always tried FIRST and is
# the only tier used in the normal case. fallback_id/emergency_id exist
# purely as load-SURVIVAL tiers now: they're only attempted if the
# previous tier actually fails to load (unknown model id, missing GGUF
# file, a safety-warning abort, or any other exception from
# ModelLoader.load_model() — see _load_model_for_request() below), never
# as a pre-emptive RAM-percentage override.
#
# This replaces an earlier RAM-threshold heuristic (SELF_QUERY_RAM_
# THRESHOLD_PCT / FALLBACK_RAM_THRESHOLD_PCT / EMERGENCY_RAM_THRESHOLD_PCT)
# that silently forced every request onto whatever model was already
# "active" whenever RAM was under 85%, regardless of what the Auto
# Selector had actually picked for that request — in practice this meant
# a low-complexity turn the router chose qwen2.5-0.5b for would still run
# on a much heavier already-loaded model, causing multi-second local
# inference and, downstream, false WebSocket "disconnects" from
# client-side heartbeat timeouts (see the diagnosis that led to this
# fix). The self-query RAM downgrade this also removed was additionally
# unreachable in practice: self-query intents are answered directly from
# backend.core.self_knowledge in backend/websocket/handlers.py /
# backend/rest/router.py, before any model is ever invoked — this
# provider's run()/stream() never saw them.
# ============================================================


def _candidate_chain(requested_id):
    """
    Ordered, de-duplicated (model_id, reason) candidates to attempt
    loading — requested_id first (reason="requested") when present,
    then whichever of fallback_id/emergency_id are configured and not
    already earlier in the chain (no point re-attempting the same
    model_id twice under a different label).

    If requested_id is empty (shouldn't normally happen — see
    _load_model_for_request()'s docstring: backend.core.provider_router
    resolves this before the provider ever runs) and neither fallback
    nor emergency is configured either, falls back to whatever's
    registered as "active" as an absolute last resort, so there's still
    something to try rather than refusing the request outright.
    """
    candidates = []
    seen = set()

    if requested_id:
        candidates.append((requested_id, "requested"))
        seen.add(requested_id)

    fallback_id = model_registry.get_fallback_model_id()
    if fallback_id and fallback_id not in seen:
        candidates.append((fallback_id, "fallback"))
        seen.add(fallback_id)

    emergency_id = model_registry.get_emergency_model_id()
    if emergency_id and emergency_id not in seen:
        candidates.append((emergency_id, "emergency"))
        seen.add(emergency_id)

    if not candidates:
        active_id = model_registry.get_active_model_id()
        if active_id:
            candidates.append((active_id, "active"))

    return candidates


class Provider:
    """
    Local GGUF provider using llama.cpp via ModelLoader.
    This provider behaves exactly like a cloud provider:
    - .run(request) for non-streaming inference
    - .stream(request, callback) for streaming inference
    """

    def __init__(self):
        logger.debug("Initializing Local Provider")
        self.loader = ModelLoader()
        logger.debug("Local Provider initialized")

    # Exposed for backend.core.self_knowledge.
    def get_active_model_id(self) -> str | None:
        return self.loader.get_active_model_id()

    def _build_prompt(self, request) -> str:
        prompt = ""
        for msg in request.messages:
            prompt += f"{msg.role}: {msg.content}\n"
        # Explicit cue for whose turn is next — without this the model has
        # to infer it's supposed to continue as "assistant" purely from
        # pattern-matching the transcript, which is a weaker anchor.
        prompt += "assistant:"
        return prompt

    def _load_model_for_request(self, request):
        """
        Loads the model that will actually serve this request, honoring
        the Auto Selector's choice (request.model_id) — the only tier
        tried in the normal case — and only falling through to
        fallback_id/emergency_id when a candidate genuinely fails to
        load. See this module's "LOAD-SURVIVAL FALLBACK CHAIN" docstring
        above for why this replaced a RAM-percentage heuristic.

        Returns (model_id, model) for whichever candidate actually
        loaded. Raises the last candidate's exception if every candidate
        in the chain fails — there's nothing left to fall back to.

        Known limitation: the packet(s) already built by
        backend/core/streaming_engine.py for this request report
        request.model_id (resolved earlier, by ProviderRouter, before
        this provider is ever called) as the modelId — if a real
        fallback happens here, what the client is told and what actually
        served the reply can briefly diverge. The unified log is the
        source of truth for what really ran; making the client-facing
        modelId reflect a mid-flight fallback would mean plumbing an
        override back through streaming_engine.py's packet builder,
        which is out of scope for this change.
        """
        requested_id = request.model_id

        if getattr(request, "allow_override", False):
            # The user was already shown a safety warning for this exact
            # model and explicitly chose "proceed anyway" — falling back
            # to something else here would silently override that choice
            # with the opposite of what they just asked for, so no
            # fallback chain applies at all in this case.
            logger.debug(f"_load_model_for_request() — allow_override set, no fallback chain for {requested_id}")
            model = self.loader.load_model(requested_id, allow_override=True)
            return requested_id, model

        candidates = _candidate_chain(requested_id)
        if not candidates:
            logger.warning("_load_model_for_request() — no requested/active/fallback/emergency model configured")
            raise RuntimeError("No local model is configured to serve this request.")

        last_error = None
        for model_id, reason in candidates:
            try:
                model = self.loader.load_model(model_id, allow_override=False)
            except Exception as e:
                last_error = e
                logger.warning(f"_load_model_for_request() — {reason} model '{model_id}' failed to load: {e}")
                unified_log("local_provider", "WARNING", "model_load_failed", {
                    "model_id": model_id, "reason": reason, "error": str(e),
                })
                continue

            if reason != "requested":
                # A REAL fallback happened — the Auto Selector's choice
                # genuinely couldn't be loaded, not a pre-emptive
                # RAM-percentage override. Logged distinctly so a log
                # consumer can trust these events now always mean an
                # actual load failure occurred.
                event = {
                    "fallback": "fallback_model_selected",
                    "emergency": "emergency_fallback_selected",
                }.get(reason, "model_switch_completed")

                logger.info(f"Model load fallback: {requested_id!r} -> {model_id} (reason={reason})")
                unified_log("local_provider", "WARNING", event, {
                    "requested_model_id": requested_id, "selected_model_id": model_id, "reason": reason,
                })
                # Distinct from the reason-specific event above — this one
                # just confirms a fallback actually completed, regardless
                # of which tier it landed on, so a log consumer can watch
                # for "any fallback happened" without knowing every label.
                unified_log("local_provider", "INFO", "model_switch_completed", {
                    "requested_model_id": requested_id, "selected_model_id": model_id, "reason": reason,
                })

            return model_id, model

        logger.error(f"_load_model_for_request() — every candidate failed to load: {[c[0] for c in candidates]}")
        raise last_error

    # -----------------------------------------------------
    # Non-streaming inference
    # -----------------------------------------------------
    def run(self, request):
        logger.debug(f"run() called → model_id={request.model_id}")

        model_id, model = self._load_model_for_request(request)
        logger.debug(f"Model loaded → {model_id}")

        prompt = self._build_prompt(request)
        logger.debug(f"Prompt built (len={len(prompt)})")

        # Run inference
        output = self.loader.run(
            prompt,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            stop=TURN_BOUNDARY_STOP_SEQUENCES,
        )

        logger.debug("run() completed successfully")
        return output

    # -----------------------------------------------------
    # Streaming inference
    # -----------------------------------------------------
    def stream(self, request, callback):
        logger.debug(f"stream() called → model_id={request.model_id}")

        model_id, model = self._load_model_for_request(request)
        logger.debug(f"Model loaded → {model_id}")

        prompt = self._build_prompt(request)
        logger.debug(f"Prompt built (len={len(prompt)})")

        # Streaming loop
        try:
            for chunk in self.loader.run_stream(
                prompt,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                stop=TURN_BOUNDARY_STOP_SEQUENCES,
            ):
                logger.debug(f"stream() chunk received (len={len(chunk)})")
                callback({
                    "type": "chat_stream",
                    "content": chunk
                })

            callback({"type": "chat_complete"})
            logger.debug("stream() completed successfully")

        except Exception as e:
            logger.debug(f"stream() ERROR → {e}")
            callback({
                "type": "error",
                "message": f"Local streaming error: {str(e)}"
            })
