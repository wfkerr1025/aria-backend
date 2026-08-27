"""ARIA Lite - the one sequence both transports run.

WebSocket and REST used to perform this identically and separately;
_prepare_chat_turn's own docstring called itself "a third independent
orchestration ... because no such module exists today". This is that
module.

What "pure" means here, precisely, because the word is doing real work:
this function performs no *effects*. It does not write to a socket, mutate
a handler's session, or emit a log line. It reads freely -- retrieval, a
resource snapshot, the model registry -- and returns a TurnResult
describing what should happen, including the session changes and log
events the transport must apply. That is what makes every branch of a chat
turn assertable without a connection.

It is synchronous, and some of what it calls blocks (a weather lookup, a
web search, retrieval). The transports call it through run_in_executor, so
one blocking turn cannot stall the event loop -- one wrapper, at one call
site, instead of an async colouring that would spread through every branch.

Order is load-bearing and is lifted from the WebSocket path, which was the
richer of the two:

    1. weather continuation window   -- before intent, so a bare location
                                        reply never reaches a model
    2. intent detection
    3. short-circuits                -- self-knowledge, model switch,
                                        weather, search
    4. model resolution              -- request > session pin > mode
    5. mode separation
    6. safety gate
    7. history shaping
    8. reasoning + InferenceRequest
"""

from __future__ import annotations

from typing import Callable

from backend.core import (
    evidence_routing,
    key_manager,
    search_intent,
    self_knowledge,
    turn_status,
    weather_nl,
)
from backend.core.conversation_manager import (
    INTENT_MODEL_SWITCH,
    INTENT_SEARCH_QUERY,
    INTENT_WEATHER_QUERY,
    SELF_QUERY_INTENTS,
    apply_history_policy,
    detect_intent,
    detect_model_switch_target,
    detect_tool_need,
    intent_hint,
    resolve_model_switch_target,
    resolve_self_query,
)
from backend.core.local_inference_engine import InferenceMessage, InferenceRequest
from backend.core.model_registry import (
    get_default_model_id,
    get_model,
    model_violates_mode_separation,
)
from backend.core.safety_manager import evaluate_safety
from backend.core.tool_executor import (
    extract_search_query,
    extract_weather_location,
    format_search_reply,
    run_search_tool,
)
from backend.core.turn_types import (
    KIND_CLARIFY,
    KIND_ERROR,
    KIND_INFERENCE,
    KIND_MODEL_SWITCH,
    KIND_SAFETY_WARNING,
    KIND_TEXT,
    SessionState,
    TurnRequest,
    TurnResult,
)
from logger import get_logger

logger = get_logger(__name__)

__all__ = ["orchestrate_turn"]

SKR_MODEL = "skr"
SEARCH_MODEL = "search"

# Cloud Mode with no key configured anywhere. Shaped like every other
# safety warning so both transports' existing warning branch renders it
# without a special case.
NO_CLOUD_PROVIDER_WARNING = {
    "type": "safety_warning",
    "model_id": None,
    "severity": "block",
    "message": (
        "You're in Cloud Mode, but no cloud provider API key is configured yet. "
        "Add one in Settings, or switch to Local Mode or automatic model selection."
    ),
    "projected": {"cpu": 0, "ram": 0, "vram": 0},
    "suggestions": [],
}


# What a turn says when the lookup it needed produced nothing. Fixed text,
# no model involved: the one thing that must not happen here is a fluent
# answer assembled from the model's weights.
EVIDENCE_MISSING_ANSWER = "I could not retrieve current data for this query."


def _tool_result_trace(request, tool_runs, lines, items) -> dict:
    """The same journey one stage earlier than _evidence_trace.

    Separate because the failures are separate: a tool can return
    structure that renders to a good line and still be dropped later, and
    a tool can return nothing while everything downstream looks healthy.
    Reading both traces side by side says which half broke.
    """
    if not getattr(request, "debug_trace", False):
        return {}
    return {
        "raw": list(tool_runs or ()),
        "normalized": [item.as_dict() for item in items],
        "flattened": lines,
        "bundle_ready": [item.as_dict() for item in items if item.usable],
    }


def _evidence_trace(request, tool_runs, lines, items) -> dict:
    """How the lookup's output became the evidence in the prompt.

    Four stages because four things can go wrong independently: the tool
    ran or it did not, the line was rendered or it was blank, the item
    normalized or it was a blob, and the block printed it or dropped it as
    unusable. A single "no evidence" tells you none of that.
    """
    if not getattr(request, "debug_trace", False):
        return {}
    return {
        "raw_tool_results": list(tool_runs or ()),
        "normalized": [item.as_dict() for item in items],
        "merged": [item.as_dict() for item in items if item.usable],
        "bundle": lines,
    }


def _trace(request, initial, route, post_resolution, final) -> dict:
    """How the model was chosen, when the caller asked to be told.

    provider_router is deliberately None. It resolves inside
    StreamingEngine, after this function has returned, so the orchestrator
    genuinely does not know what it picked -- and recording a guess there
    would make the trace worse than useless to whoever is reading it to
    find out.
    """
    if not getattr(request, "debug_trace", False):
        return {}
    return {
        "initial": initial,
        "provider_router": None,
        "post_resolution": post_resolution,
        "capability_routing": route,
        "final": final,
    }


def _event(name: str, **fields) -> dict:
    """One telemetry record. Emitted by the transport, never here."""
    return {"event": name, **fields}


# ======================================================
# 1-3. Short-circuits
# ======================================================
def _weather_reply(location: str, request: TurnRequest, telemetry: list) -> TurnResult:
    """A resolved weather answer, or a clarification when there is no place.

    Routed through weather_nl, which goes to the fusion engine and never
    fabricates a reading -- the reason weather has never been allowed to
    reach a model on this path.
    """
    if not request.session.connection_healthy:
        return TurnResult(
            kind=KIND_ERROR,
            text="Backend connection is down — tool requests are not accepted until it recovers.",
            conversation_id=request.conversation_id,
            telemetry=telemetry + [_event("weather_query_skipped_unhealthy")],
        )

    if not location:
        # last_turn_was_weather stays True through a clarification: the
        # correction window has to survive the question, or "no, I meant
        # Paris" lands in ordinary chat.
        return TurnResult(
            kind=KIND_CLARIFY,
            text=weather_nl.CLARIFICATION_PROMPT,
            model_id=None,
            conversation_id=request.conversation_id,
            session_updates={"awaiting_weather_location": True, "last_turn_was_weather": True},
            telemetry=telemetry + [_event("weather_clarification_requested")],
        )

    answer_text, packet = weather_nl.resolve_weather_reply(location)
    return TurnResult(
        kind=KIND_TEXT,
        text=answer_text,
        model_id=weather_nl.WEATHER_MODEL_SENTINEL,
        conversation_id=request.conversation_id,
        session_updates={"last_turn_was_weather": True, "awaiting_weather_location": False},
        telemetry=telemetry + [_event(
            "weather_query_answered_directly_via_fusion",
            location=location,
            confidence=packet.confidence if packet else None,
            providers_used=packet.providers_used if packet else [],
        )],
    )


def _search_reply(request: TurnRequest, telemetry: list) -> TurnResult:
    """A real web-search answer. No model is invoked."""
    if not request.session.connection_healthy:
        return TurnResult(
            kind=KIND_ERROR,
            text="Backend connection is down — tool requests are not accepted until it recovers.",
            conversation_id=request.conversation_id,
            telemetry=telemetry + [_event("tool_query_skipped_unhealthy")],
        )

    query = extract_search_query(request.latest_user_text)
    result = run_search_tool(query)
    return TurnResult(
        kind=KIND_TEXT,
        text=format_search_reply(result),
        model_id=SEARCH_MODEL,
        conversation_id=request.conversation_id,
        session_updates={"last_turn_was_weather": False},
        telemetry=telemetry + [_event("tool_query_answered_directly", intent=INTENT_SEARCH_QUERY)],
    )


def _self_knowledge_reply(intent: str, request: TurnRequest, mode_manager, telemetry: list) -> TurnResult:
    """An answer from the registry about ARIA itself. No model is invoked."""
    active_id, provider_name = self_knowledge.resolve_active_model_and_provider(mode_manager)
    snapshot = self_knowledge.build_snapshot(
        active_model_id=active_id,
        provider_name=provider_name,
        conversation_id=request.conversation_id,
        history_length=1,
        multi_turn_mode=True,
    )
    return TurnResult(
        kind=KIND_TEXT,
        text=self_knowledge.answer_self_query(intent, snapshot),
        model_id=SKR_MODEL,
        conversation_id=request.conversation_id,
        session_updates={"last_turn_was_weather": False},
        telemetry=telemetry + [
            _event("self_query_resolved", intent=intent, snapshot=snapshot),
            _event("self_query_answered_directly", intent=intent),
        ],
    )


# ======================================================
# 4-6. Model, mode, safety
# ======================================================
def _resolve_model_id(request: TurnRequest, default_local_model: Callable[[], str | None]):
    """Model precedence, unchanged: request field > session pin > mode.

    Cloud and Automatic deliberately resolve to None so ProviderRouter
    reaches its own mode-based branches instead of always taking the
    explicit-model one.
    """
    session = request.session
    requested = request.requested_model_id or session.explicit_model_override

    if requested and model_violates_mode_separation(requested, session.mode):
        logger.warning(
            "requested model_id '%s' is incompatible with mode '%s' — ignoring it "
            "(absolute mode separation).", requested, session.mode,
        )
        requested = None

    if requested:
        return requested, True
    if session.mode == "local":
        return default_local_model(), False
    return None, False


def _evaluate_safety(model_id: str | None, request: TurnRequest, suggester):
    """Run the safety gate. Returns (decision, model_cfg, warning_packet).

    warning_packet is None unless the gate actually refuses. decision and
    model_cfg come back either way, because the transport emits warnings on
    both paths today and losing that would be a behaviour change.

    Skipped entirely for a turn with no model to load -- the same exemption
    self-knowledge already had. A RAM-pressure warning should not block an
    answer that never touches RAM.
    """
    if model_id is None:
        return None, None, None

    model_cfg = get_model(model_id)
    if model_cfg is None:
        return None, None, None

    decision = evaluate_safety(model_cfg)
    if not getattr(decision, "requires_warning", False):
        return decision, model_cfg, None

    suggestions = suggester.suggest(decision.snapshot, model_cfg) if suggester else []
    packet = {
        "type": "safety_warning",
        "model_id": model_id,
        "severity": decision.severity,
        "message": decision.message,
        "projected": {
            "cpu": decision.projected_cpu_pct,
            "ram": decision.projected_ram_pct,
            "vram": decision.projected_vram_pct,
        },
        "suggestions": [{"id": s.model_id, "reason": s.reason} for s in suggestions],
    }
    return decision, model_cfg, packet


# ======================================================
# 7-8. Reasoning
# ======================================================
# Intents that are answered by a sentence and would gain nothing from
# planning, tools or an evidence bundle. Running the reasoning core for
# "hello" costs a retrieval pass and a much longer prompt to produce the
# same two words.
_TRIVIAL_INTENTS = frozenset({"greeting", "clarification_needed", "context_reset"})


def _is_trivial(intent: str) -> bool:
    return intent in _TRIVIAL_INTENTS


def _build_reasoning_prompt(request: TurnRequest, text: str, policy_info: dict, intent: str):
    """Engine B's AnswerPrompt for this turn, or None if the core did not run.

    Fails open. The reasoning core is newer than everything around it, and
    a turn that would otherwise have been answered plainly must not be lost
    to an exception in it -- returning None falls back to the ordinary
    path, which is exactly what happened before this seam existed.
    """
    try:
        from backend.aria_synthesis.bundle_builder import build_evidence_bundle
        from backend.aria_synthesis.synthesis_engine import build_answer_prompt
    except Exception:  # pragma: no cover - reasoning core is optional
        return None

    try:
        bundle = build_evidence_bundle(
            text,
            policy_info.get("retrieved_items") or [],
            routing_intent=policy_info.get("routing_intent") or intent,
            # The FULL history, never final_messages: turn selection is
            # what reaches past a trim, and handing it the trimmed list
            # would reduce it to the truncation it replaces.
            conversation=request.messages,
        )
        return build_answer_prompt(text, bundle)
    except Exception:
        logger.exception("reasoning core failed for this turn; answering without it.")
        return None


# ======================================================
# The sequence
# ======================================================
def orchestrate_turn(
    request: TurnRequest,
    *,
    mode_manager=None,
    default_local_model: Callable[[], str | None] | None = None,
    suggester=None,
    generator: Callable[[str], str] | None = None,
    reasoning_enabled: bool = True,
    on_status: Callable[[str], None] | None = None,
) -> TurnResult:
    """Decide what should happen for one chat turn.

    mode_manager and default_local_model are injected so this stays
    testable without a live ModeManager or a persisted model; both fall
    back to the real thing when omitted.

    generator is passed through to the reasoning core. It is unused until
    the Phase core is inserted at the two seams; accepting it now keeps the
    transports from needing a signature change then.

    on_status is an optional progress callback, called with a value from
    backend.core.turn_status as each phase begins. It is the one piece of
    output this function has that is not in the TurnResult, and it is kept
    honest by two rules: it is optional and defaulted, so every existing
    caller behaves exactly as before; and it is fired only where the
    statement it makes is true at the moment it is made.

    That second rule is why only `planning` is emitted here and not
    `executing` or `synthesizing`. Planning, tool routing, tool execution
    and prompt assembly all happen inside one call into Engine B
    (build_answer_prompt), which this function cannot see into. It learns a
    tool ran only from AnswerPrompt.tool_runs, after the fact -- and
    announcing "Searching..." once the search has finished is a progress
    indicator that lies about the present tense, which is worse than the
    coarser one it replaces. Emitting those two live needs the same
    optional callback threaded into build_answer_prompt; that is an Engine
    B change and is deliberately not made here.

    A callback that raises is swallowed. Progress reporting is decoration,
    and a turn must not be lost to it.
    """
    def status(value: str) -> None:
        """Report progress, if anyone is listening, without ever failing."""
        if on_status is None:
            return
        try:
            on_status(value)
        except Exception:  # pragma: no cover - decoration must not break a turn
            logger.debug("on_status(%r) raised; continuing", value, exc_info=True)

    if mode_manager is None:
        from backend.core.mode_manager import ModeManager

        mode_manager = ModeManager()
    if default_local_model is None:
        default_local_model = get_default_model_id

    session = request.session
    text = request.latest_user_text
    telemetry: list[dict] = []

    # --- 1. Weather continuation window, before intent detection. A bare
    # location reply ("Orange, VA, 22960") carries no weather phrasing at
    # all and would otherwise land in ordinary chat, where a model could
    # hallucinate a "corrected" reading instead of a real lookup.
    #
    # These two return before step 2, so they record the intent
    # themselves -- both transports logged a weather intent here before
    # the extraction, and a continuation turn that produced no
    # intent_detected line at all would be a hole in the audit trail
    # exactly where the reading came from a tool rather than a model.
    if session.awaiting_weather_location:
        telemetry.append(_event("intent_detected", intent=INTENT_WEATHER_QUERY))
        return _weather_reply(text.strip(), request, telemetry)

    if session.last_turn_was_weather and weather_nl.is_correction_phrase(text):
        telemetry.append(_event("intent_detected", intent=INTENT_WEATHER_QUERY))
        return _weather_reply("", request, telemetry)

    # --- 2. Intent.
    is_followup = request.multi_turn and len(request.messages) > 1
    intent = detect_intent(text, is_multi_turn_followup=is_followup)
    telemetry.append(_event("intent_detected", intent=intent))

    # --- 3. Short-circuits. Any other message ends the weather window.
    if intent in SELF_QUERY_INTENTS:
        return _self_knowledge_reply(intent, request, mode_manager, telemetry)

    if intent == INTENT_MODEL_SWITCH:
        raw_target = detect_model_switch_target(text.strip().lower())
        resolved = resolve_model_switch_target(raw_target) if raw_target else None
        if resolved is not None:
            # Handed back exactly as resolve_model_switch_target returned
            # it -- {"kind": "model"|"mode"|"provider", ...} -- because
            # performing the switch means validating it against the
            # current routing mode (routing_guard) and emitting the
            # packets that tell the UI what changed. Both are Engine A's
            # by the blueprint's own division: mode separation and
            # transport. The orchestrator's job is deciding that this
            # turn is a switch and what it points at.
            return TurnResult(
                kind=KIND_MODEL_SWITCH,
                conversation_id=request.conversation_id,
                metadata={"model_switch": dict(resolved)},
                session_updates={"last_turn_was_weather": False},
                telemetry=telemetry + [_event("model_switch", target=dict(resolved))],
            )
        logger.warning("model_switch intent but target no longer resolves: %r", raw_target)

    if intent == INTENT_WEATHER_QUERY:
        return _weather_reply(extract_weather_location(text) or "", request, telemetry)

    # The search bypass is gone: a lookup is now a planned step inside the
    # reasoning turn (seam A), so answering here as well would run the
    # search twice. detect_intent still classifies it -- the hint and the
    # logs still use it -- it just no longer routes.
    #
    # The one case it is kept for is an unhealthy connection, which is a
    # transport fact the reasoning core has no way to know.
    if intent == INTENT_SEARCH_QUERY and not request.session.connection_healthy:
        return _search_reply(request, telemetry)

    # --- 4/5. Model resolution and mode separation.
    model_id, explicit = _resolve_model_id(request, default_local_model)
    initial_model_id = model_id

    # Cloud Mode, nothing asked for by name, and no cloud provider
    # configured anywhere. "Must NOT silently fall back to local" -- so
    # this is a structured refusal rather than a request that fails deep
    # inside the provider with a generic error.
    #
    # Both transports had this check and each had written it out; it
    # belongs with the mode resolution that produces the condition.
    if model_id is None and request.session.mode == "cloud":
        if not any(key_manager.list_configured_providers().values()):
            return TurnResult(
                kind=KIND_SAFETY_WARNING,
                model_id=None,
                conversation_id=request.conversation_id,
                warning=dict(NO_CLOUD_PROVIDER_WARNING),
                telemetry=telemetry + [_event("cloud_mode_no_provider")],
            )

    # --- 5b. Capability routing for a turn that will carry evidence.
    #
    # Decided here, beside the model resolution it overrides, and before
    # the safety gate runs -- so the gate evaluates the model that will
    # actually be loaded. Deciding it after Engine B, when tool_runs is a
    # fact rather than a prediction, would mean projecting RAM for one
    # model and loading another.
    #
    # The prediction is the search vocabulary that the planner itself uses
    # (backend/core/search_intent.py), so "will a lookup happen" is
    # answered here by the same table that decides it later. It is
    # confirmed against the real tool_runs further down.
    expects_evidence = (
        intent == INTENT_SEARCH_QUERY or search_intent.mentions_web_search(text)
    )
    cloud_available = any(key_manager.list_configured_providers().values())
    route = evidence_routing.choose_route(
        expects_evidence=expects_evidence,
        model_id=model_id,
        mode=session.mode,
        cloud_available=cloud_available,
    )

    if route != evidence_routing.ROUTE_NORMAL:
        telemetry.append(_event(
            "model_routing", decision=route, model_id=model_id, mode=session.mode,
        ))

    # --- 5c. Automatic mode and the allowlist.
    #
    # Automatic leaves model_id None so ProviderRouter can weigh local
    # against cloud, which means capability routing here has no model to
    # act on. The obvious fix -- pin the evidence floor model when there
    # is no cloud to escalate to -- was implemented, measured, and
    # removed, because it makes things worse:
    #
    #   a concrete model_id is a model the SAFETY GATE evaluates. With
    #   model_id None the gate is skipped entirely and AutoSelector does
    #   its own hardware-aware stepping without ever refusing a turn.
    #   Pinning mistral-7b put an evidence turn in front of the gate for
    #   the first time, and on a loaded machine it came back
    #   safety_warning/caution -- refusing a turn that would previously
    #   have run.
    #
    # Trading "might pick a weak model" for "might refuse outright" is not
    # a trade worth making, and it is not needed: complexity_router now
    # floors an evidence-bearing prompt at the medium tier
    # (prompt_carries_evidence), so Automatic's local branch cannot reach
    # the emergency model for one of these turns anyway. The guarantee is
    # kept; the new failure mode is not introduced.
    if expects_evidence and model_id is None:
        telemetry.append(_event(
            "model_routing", decision=evidence_routing.ROUTE_DEFERRED_TO_ROUTER,
            mode=session.mode, cloud_available=cloud_available,
        ))

    if route == evidence_routing.ROUTE_CLOUD:
        # model_id=None is how this codebase says "let ProviderRouter
        # choose", which in Cloud or Automatic mode means the configured
        # cloud provider. There is deliberately no "cloud-default" id to
        # set instead: the registry holds no cloud models, and
        # model_violates_mode_separation() treats any concrete id in Cloud
        # Mode as a violation.
        logger.info(
            "evidence-bearing turn: %s cannot synthesize evidence, routing to cloud.",
            model_id,
        )
        model_id = None

    # --- 6. Safety.
    #
    # Two bypasses, and neither is the packet's allowOverride: that flag
    # threads down to the model loader but does not exempt a turn from the
    # gate. Only a session suppression (skipSafetyCheck) or a one-shot
    # "Proceed Anyway" granted for this exact model does, and both force
    # allow_override on the request that follows.
    session_updates: dict = {"last_turn_was_weather": False}
    bypass_reason = None

    if request.skip_safety_check:
        bypass_reason = "session_suppression"
    elif model_id is not None and session.override_model_id == model_id:
        bypass_reason = "proceed_anyway"
        # Consumed here, so the next request for this model is gated again.
        session_updates["override_model_id"] = None

    allow_override = request.allow_override or bypass_reason is not None

    if bypass_reason is not None:
        telemetry.append(_event("safety_check_bypassed", model_id=model_id, reason=bypass_reason))
        decision, model_cfg, warning_packet = None, None, None
    else:
        decision, model_cfg, warning_packet = _evaluate_safety(model_id, request, suggester)
    if warning_packet is not None:
        return TurnResult(
            kind=KIND_SAFETY_WARNING,
            model_id=model_id,
            conversation_id=request.conversation_id,
            warning=warning_packet,
            safety_decision=decision,
            model_cfg=model_cfg,
            telemetry=telemetry + [_event(
                "safety_warning", model_id=model_id, severity=decision.severity,
            )],
        )

    # --- 7. History shaping. Retrieval runs once, inside here, and the
    # ranked items come back on policy_info so the reasoning core can use
    # them without a second pass.
    #
    # `final_messages` is the provider's view and is trimmed to a token
    # budget. request.messages stays whole for the reasoning core, whose
    # turn selection exists precisely to reach past a trim.
    reason = reasoning_enabled and not _is_trivial(intent)

    final_messages, policy_info = apply_history_policy(
        request.messages,
        multi_turn=request.multi_turn,
        # When the reasoning core owns the turn it also owns the evidence,
        # so the system prompt carries persona only. Running both would put
        # the same notes in front of the model twice, in two formats, with
        # two different sets of instructions about citing them.
        suppress_context=reason,
    )
    telemetry.append(_event("history_policy", **policy_info))

    hint = intent_hint(intent)
    if hint:
        final_messages = final_messages[:1] + [{"role": "system", "content": hint}] + final_messages[1:]

    # --- Seam A + B: planning, tools and synthesis.
    #
    # Engine B builds the prompt and stops there. Generation stays with
    # Engine A, which means this turn streams exactly like any other and
    # Engine B still knows nothing about streaming.
    if reason:
        # True at the moment it is said: build_answer_prompt's first act is
        # to build the plan.
        status(turn_status.PLANNING)

    answer = _build_reasoning_prompt(request, text, policy_info, intent) if reason else None
    reasoning_prompt = answer.text if answer is not None else None
    tool_runs = tuple(answer.tool_runs) if answer is not None else ()
    if reason:
        telemetry.append(_event(
            "reasoning_applied",
            used=reasoning_prompt is not None,
            tool_runs=list(tool_runs),
            evidence_items=len(policy_info.get("retrieved_items") or []),
        ))

    # --- Evidence was expected and did not arrive.
    #
    # answer is not None but answer.text is None means Engine B ran and
    # found nothing to work with: an empty bundle and no successful
    # lookup. Answering anyway means answering from the model's weights,
    # which for a question about a current price is fabrication by
    # construction -- and the old behaviour made it worse, because the
    # fallback prompt was the short raw question, which the length
    # heuristic classified "low" and handed to the 0.5B model. The
    # weakest model, on the turn with the highest hallucination risk.
    #
    # answer is None is a different case -- the reasoning core was
    # unavailable or raised -- and still fails open to ordinary chat, as
    # it always has.
    # What the lookup actually produced, normalized once and reused by the
    # detection below, by the prompt builders, and by the debug trace.
    evidence_lines = evidence_routing.extract_tool_results(reasoning_prompt or "")
    evidence_items = evidence_routing.normalize_evidence(evidence_lines)

    # Evidence is missing when a lookup ran and produced nothing usable --
    # not when Engine B happened to return no prompt.
    #
    # The old test was `answer.text is None`, which only catches a tool
    # that raised. Measured against the seven ways a tool can come back,
    # five slipped through: an empty list, None, a malformed shape, an
    # empty reply string, and a serialized API envelope all produced a
    # turn that reported success and handed the model "" or a JSON blob --
    # under an instruction reading "use ONLY the evidence above". That is
    # a prompt that asks for a fabrication.
    #
    # retrieved_items is the other half: a turn backed by notes or files
    # has evidence even when no tool ran, and must not be refused because
    # the search came back thin.
    retrieved_items = policy_info.get("retrieved_items") or []
    tool_evidence_unusable = (
        bool(tool_runs)
        and not evidence_routing.has_usable_evidence(evidence_items)
        and not retrieved_items
    )

    if expects_evidence and (
        (answer is not None and answer.text is None) or tool_evidence_unusable
    ):
        logger.info("evidence was expected for this turn and none arrived; not answering from weights.")
        telemetry.append(_event(
            "evidence_missing", intent=intent,
            tool_runs=list(tool_runs), lines_returned=len(evidence_lines),
            usable=evidence_routing.has_usable_evidence(evidence_items),
        ))
        return TurnResult(
            kind=KIND_TEXT,
            text=EVIDENCE_MISSING_ANSWER,
            model_id=None,
            conversation_id=request.conversation_id,
            policy_info=policy_info,
            session_updates=session_updates,
            metadata={"tool_runs": tool_runs},
            evidence_missing=True,
            model_resolution_trace=_trace(
                request, initial_model_id, route, model_id, None,
            ),
            evidence_trace=_evidence_trace(request, tool_runs, evidence_lines, evidence_items),
            tool_result_trace=_tool_result_trace(request, tool_runs, evidence_lines, evidence_items),
            telemetry=telemetry,
        )

    # The prediction above is now a fact. A turn routed to a fallback that
    # turns out to have run no evidence tool needs no fallback, so the
    # downgrade is applied only where it is still warranted.
    synthesis_mode = evidence_routing.SYNTHESIS_FULL
    if route in (evidence_routing.ROUTE_SIMPLIFIED, evidence_routing.ROUTE_RAW_EVIDENCE):
        confirmed = evidence_routing.is_evidence_bearing(tool_runs)
        telemetry.append(_event(
            "model_routing_confirmed", decision=route, evidence_bearing=confirmed,
        ))

        if confirmed:
            evidence = evidence_items

            if route == evidence_routing.ROUTE_RAW_EVIDENCE:
                # No model is asked to synthesize. Answering from a model
                # that cannot read the evidence is the failure the whole
                # pipeline exists to prevent, so this shows the findings
                # instead.
                return TurnResult(
                    kind=KIND_TEXT,
                    text=evidence_routing.format_raw_evidence(evidence, text),
                    model_id=evidence_routing.SYNTHESIS_RAW_EVIDENCE,
                    conversation_id=request.conversation_id,
                    policy_info=policy_info,
                    session_updates=session_updates,
                    metadata={"tool_runs": tool_runs},
                    synthesis_mode=evidence_routing.SYNTHESIS_RAW_EVIDENCE,
                    model_resolution_trace=_trace(
                        request, initial_model_id, route, model_id, None,
                    ),
                    evidence_trace=_evidence_trace(
                        request, tool_runs, evidence_lines, evidence_items,
                    ),
                    tool_result_trace=_tool_result_trace(
                        request, tool_runs, evidence_lines, evidence_items,
                    ),
                    telemetry=telemetry,
                )

            # ROUTE_SIMPLIFIED: same model, far less asked of it.
            reasoning_prompt = evidence_routing.simplified_prompt(text, evidence)
            synthesis_mode = evidence_routing.SYNTHESIS_SIMPLIFIED

    if reasoning_prompt is not None:
        # The synthesis prompt replaces the user turn; the persona system
        # message and the trimmed history stay in front of it.
        provider_messages = [
            InferenceMessage(role=m.get("role", "user"), content=m.get("content", ""))
            for m in final_messages[:-1]
        ] + [InferenceMessage(role="user", content=reasoning_prompt)]
    else:
        provider_messages = [
            InferenceMessage(role=m.get("role", "user"), content=m.get("content", ""))
            for m in final_messages
        ]

    tool_decision = detect_tool_need(text)
    telemetry.append(_event("tool_routing_decision", decision=tool_decision))

    # --- 8. The provider request.
    inference_request = InferenceRequest(
        model_id=model_id,
        messages=provider_messages,
        max_tokens=request.max_tokens,
        temperature=request.temperature,
        intent=intent,
        allow_override=allow_override,
    )

    return TurnResult(
        kind=KIND_INFERENCE,
        inference_request=inference_request,
        model_id=model_id,
        conversation_id=request.conversation_id,
        policy_info=policy_info,
        session_updates=session_updates,
        safety_decision=decision,
        model_cfg=model_cfg,
        metadata={"tool_runs": tool_runs},
        synthesis_mode=synthesis_mode,
        model_resolution_trace=_trace(request, initial_model_id, route, model_id, model_id),
        evidence_trace=_evidence_trace(request, tool_runs, evidence_lines, evidence_items),
        tool_result_trace=_tool_result_trace(request, tool_runs, evidence_lines, evidence_items),
        telemetry=telemetry,
    )
