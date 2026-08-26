# backend/core/self_knowledge.py
#
# Self-Knowledge Registry (SKR): the single source of truth ARIA draws on
# when asked about herself.
#
# The model has no innate idea what model_id it's running as, what
# version this build is, or what tools exist — left to itself, "what
# model are you?" gets answered by guessing (hallucinating) a plausible-
# sounding name. This module holds the real facts and builds a snapshot
# per turn; conversation_manager.py injects the relevant slice into the
# system prompt for self-query turns so ARIA answers from ground truth
# instead of guessing.
#
# Static facts (version strings, persona/prompt summaries, tool list) are
# declared here directly. Runtime facts (which model is actually serving
# THIS turn, which conversation this is, how history is currently
# configured) are passed in by the caller — websocket/handlers.py and
# server.py already know them, and reaching for some global "the current
# model" would be wrong anyway: ProviderRouter/StreamingEngine are
# instantiated per WebSocket connection, so there is no single global
# active model to query. Ground truth for "what model are you using" is
# whatever model_id this specific turn actually resolved to.

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from backend.core import model_registry
from backend.core import model_selector
from backend.core.model_discovery import infer_arch
from backend.core.mode_manager import ModeManager
from backend.core import key_manager
from backend.llm.providers.provider_registry import list_providers, get_provider_display_name

from backend.logger import log as unified_log

from logger import get_logger

logger = get_logger(__name__)


# ============================================================
# STATIC FACTS
# ============================================================

# Sourced from backend/server.py's FastAPI(version=...) and
# ARIA-Lite Desktop/package.json's "version" — duplicated here rather
# than imported to avoid a circular import (server.py imports this
# module's caller, conversation_manager.py); keep these three in sync by
# hand if either version bumps.
ARIA_VERSION = "ARIA-Lite 1.0.0"
BACKEND_VERSION = "1.0.0"
FRONTEND_VERSION = "1.0.0"

PERSONA_SUMMARY = (
    "Direct, concise, warm, and conversational — answers what's asked "
    "without unsolicited explanations, lectures, or self-description."
)

# Deliberately a high-level paraphrase, not the verbatim SYSTEM_PROMPT —
# self-query answers should describe the persona, not dump the raw
# prompt text back at the user (see resolve_self_query()'s
# system_prompt_summary handling).
SYSTEM_PROMPT_SUMMARY = (
    "Respond only to the latest message; never invent or simulate "
    "conversation turns; keep greetings under 15 words; don't explain "
    "capabilities, tools, or reasoning unless asked; stay concise unless "
    "more detail is requested; match the user's tone; don't drift "
    "topics; never state unsure things as fact; ask a clarifying "
    "question when a request is ambiguous."
)

# Matches conversation_manager.TOOL_KEYWORDS' keys — these are the only
# tools ARIA currently has routing awareness of. Not the same as "tools
# that execute": see TOOL_ROUTING_RULES below.
AVAILABLE_TOOLS: List[str] = ["get_weather", "web_search"]

TOOL_ROUTING_RULES = (
    "Weather questions route to the get_weather tool; requests for "
    "current events, recent news, or general internet lookups route to "
    "the web_search tool. Detection is keyword-based and currently "
    "dry-run only — tool routing decisions are logged, but no tool "
    "actually executes yet in this build."
)

MEMORY_POLICY = (
    "Multi-turn ON keeps the last 12 messages of conversation history; "
    "multi-turn OFF keeps only the latest message. History is scoped per "
    "conversation_id and is fully cleared on a context reset — nothing "
    "carries over into a new chat."
)

STREAMING_MODE = (
    "Tokens stream in real time over the WebSocket connection as they're "
    "generated, off the main event loop so multiple requests can be "
    "served concurrently without one generation blocking another."
)


def get_stop_sequences() -> List[str]:
    # Imported lazily to avoid a hard dependency on the local provider
    # for callers that only need the rest of the snapshot (e.g. a pure
    # cloud-provider deployment would still want version/persona facts).
    try:
        from backend.llm.providers.local_provider import TURN_BOUNDARY_STOP_SEQUENCES
        return list(TURN_BOUNDARY_STOP_SEQUENCES)
    except Exception:
        logger.debug("get_stop_sequences() — local_provider unavailable, returning empty list")
        return []


def get_provider_names() -> List[str]:
    return list_providers()


def resolve_active_model_and_provider(mode_manager: ModeManager) -> tuple[Optional[str], Optional[str]]:
    """
    Mode-aware (active_model_id, provider_name) for self-query answers —
    the SAME precedence real chat_request resolution already uses
    (explicit override wins, then mode-based), so a self-query can never
    compose an impossible statement like "I'm running via the 'local'
    provider, in Cloud Mode".

    Callers that answer a self-query WITHOUT actually running inference
    (backend/websocket/handlers.py's _answer_self_query_directly(),
    backend/server.py's /chat self-query branch, backend/rest/router.py's
    _prepare_chat_turn() self-query branch) used to always report
    model_registry.get_default_model_id() + its "local" provider
    unconditionally — correct in Local Mode, but flatly wrong in Cloud
    Mode (and misleading under Automatic Model Routing, which might
    route the very next message to cloud). This is the one place that
    mode-aware resolution
    lives for the self-query path, so all three callers can share it
    instead of drifting independently.

    Batch 2: Cloud Mode now returns a REAL cloud model_id — resolved via
    backend.core.model_selector.select_cloud_model(), the single source
    of truth for "which model backs this cloud provider" — rather than
    always None. Falls back to active_model_id=None only when no model
    can actually be resolved (no cloud_provider chosen yet, or the
    provider has no default/override mapping): a legitimate "nothing to
    report yet" state (see backend.core.provider_router.ProviderRouter.
    resolve()'s identical cloud-with-nothing-configured fallback), never
    a guess.

    ABSOLUTE MODE SEPARATION: an explicit_model_override pinned to the
    WRONG registry for the current mode is deliberately NOT honored here
    — mirrors the identical gate in
    backend.core.provider_router.ProviderRouter.resolve(). Without this,
    a user saying "switch to mistral" (a local model) while already in
    Cloud Mode would pin that override, and this function would then
    report exactly the impossible "local provider, in Cloud Mode" this
    whole function exists to prevent. An incompatible override is
    ignored and falls through to the mode-based default below, same as
    provider_router.py falls through to normal mode-based resolution.

    Returns the raw (active_model_id, provider_id) pair — the single
    source of truth for self-query replies. Human-readable display names
    (active_model_display_name, provider_display_name) and the mode
    itself are NOT duplicated into this return value: build_snapshot()
    derives the display names from these exact same ids one step later
    (a pure registry/provider_registry lookup, never a guess), and the
    mode is already known to the caller via the same `mode_manager`
    instance passed in here — see SelfKnowledgeSnapshot.routing_mode.
    """
    mode = mode_manager.get_mode()
    override = mode_manager.get_explicit_model_override()

    if override and not model_registry.model_violates_mode_separation(override, mode):
        cfg = model_registry.get_model(override)
        return override, (cfg.get("provider") if cfg else None)

    if mode == "cloud":
        cloud_provider = mode_manager.get_cloud_provider()
        selection = model_selector.select_cloud_model(cloud_provider)
        if isinstance(selection, model_selector.SelectionError):
            return None, cloud_provider
        return selection.model_id, selection.provider

    # Local or Automatic — Automatic Model Routing's actual per-message
    # routing hasn't run for this turn (self-queries deliberately
    # short-circuit before any routing happens), so the persisted local
    # default is the most truthful "what I'd currently use" answer
    # available without guessing; answer_self_query()'s environment_query
    # text already names the mode itself separately, so this is never
    # presented as "definitely what the next message will use" when mode
    # is automatic.
    active_id = model_registry.get_default_model_id()
    cfg = model_registry.get_model(active_id) if active_id else None
    return active_id, (cfg.get("provider") if cfg else None)


def get_installed_models() -> List[Dict[str, Any]]:
    """
    Every model backend.core.model_registry currently knows about
    (hand-curated + auto-discovered), summarized for self-query answers.
    "arch" falls back to filename-based inference for hand-curated
    entries that predate model_discovery.py and so don't carry an
    explicit "arch" field of their own.
    """
    models = []
    for cfg in model_registry.get_all_models():
        params = cfg.get("params")
        models.append({
            "model_id": cfg.get("id"),
            "arch": cfg.get("arch") or infer_arch(cfg.get("name") or cfg.get("id") or ""),
            "params_b": round(params / 1_000_000_000, 2) if params else None,
            "quantization": cfg.get("quant"),
            "context_tokens": cfg.get("maxContext"),
        })
    return models


# ============================================================
# PER-TURN SNAPSHOT
# ============================================================
@dataclass
class SelfKnowledgeSnapshot:
    active_model_id: Optional[str]
    active_model_display_name: Optional[str]
    fallback_model_id: Optional[str]
    emergency_model_id: Optional[str]
    provider_name: Optional[str]
    provider_display_name: Optional[str]
    aria_version: str
    available_tools: List[str]
    installed_models: List[Dict[str, Any]]
    persona_summary: str
    system_prompt_summary: str
    conversation_id: Optional[str]
    history_length: int
    multi_turn_mode: bool
    tool_routing_rules: str
    memory_policy: str
    streaming_mode: str
    stop_sequences: List[str]
    backend_version: str
    frontend_version: str

    # Routing awareness (backend.core.mode_manager) — see PART B: "state
    # current mode, active model, whether using a permanent override,
    # whether falling back due to provider availability".
    routing_mode: str
    explicit_model_override: Optional[str]
    cloud_provider: Optional[str]
    cloud_provider_configured: Optional[bool]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def build_snapshot(
    *,
    active_model_id: Optional[str] = None,
    active_model_display_name: Optional[str] = None,
    provider_name: Optional[str] = None,
    provider_display_name: Optional[str] = None,
    conversation_id: Optional[str] = None,
    history_length: int = 0,
    multi_turn_mode: bool = True,
) -> SelfKnowledgeSnapshot:
    """
    Assemble the authoritative, current-turn snapshot: static facts plus
    whatever the caller knows about this specific request (the actual
    resolved model_id, which conversation this is, how much history is
    in play), plus the live model registry state (installed models and
    the configured active/fallback/emergency roles — see
    backend/core/model_registry.py and backend/core/model_discovery.py).
    Falls back to the registry's configured active model when the caller
    doesn't have a resolved active_model_id yet (e.g. before model
    resolution has run for this turn).

    active_model_display_name / provider_display_name are derived
    automatically from active_model_id / provider_name when the caller
    doesn't already have them on hand (a pure registry/provider_registry
    lookup off the same ids — never an independent guess), so every
    existing caller gets truthful display names for free without having
    to change. This is what self_knowledge.answer_self_query() uses
    exclusively for self-query replies (see its "model_query" and
    "mode_query" branches) instead of the legacy fallback/emergency/
    installed-models text.

    Routing state (mode, explicit override, cloud provider) is read live
    from mode_manager rather than passed in — like the model registry
    fields above, it's durable, cross-connection state (see
    backend.core.mode_manager.ModeManager), not something specific to
    this one turn, so every caller gets it "for free" and can't
    accidentally build a snapshot with stale routing info.
    """
    mm = ModeManager()
    cloud_provider = mm.get_cloud_provider()
    cloud_provider_configured = (
        key_manager.list_configured_providers().get(cloud_provider, False)
        if cloud_provider else None
    )

    resolved_active_model_id = active_model_id or model_registry.get_active_model_id()
    if active_model_display_name is None and resolved_active_model_id:
        active_cfg = model_registry.get_model(resolved_active_model_id)
        if active_cfg:
            active_model_display_name = active_cfg.get("name")
        elif provider_name:
            # Batch 2: resolved_active_model_id may be a cloud model id
            # (backend.core.model_selector, e.g. "gpt-4") — unknown to
            # model_registry (a local-only registry) — so fall back to
            # the same selector for its display name rather than
            # leaving it null.
            selection = model_selector.select_cloud_model(provider_name)
            if isinstance(selection, model_selector.ModelInfo) and selection.model_id == resolved_active_model_id:
                active_model_display_name = selection.display_name
    if provider_display_name is None and provider_name:
        provider_display_name = get_provider_display_name(provider_name)

    snapshot = SelfKnowledgeSnapshot(
        active_model_id=resolved_active_model_id,
        active_model_display_name=active_model_display_name,
        fallback_model_id=model_registry.get_fallback_model_id(),
        emergency_model_id=model_registry.get_emergency_model_id(),
        provider_name=provider_name,
        provider_display_name=provider_display_name,
        aria_version=ARIA_VERSION,
        available_tools=list(AVAILABLE_TOOLS),
        installed_models=get_installed_models(),
        persona_summary=PERSONA_SUMMARY,
        system_prompt_summary=SYSTEM_PROMPT_SUMMARY,
        conversation_id=conversation_id,
        history_length=history_length,
        multi_turn_mode=multi_turn_mode,
        tool_routing_rules=TOOL_ROUTING_RULES,
        memory_policy=MEMORY_POLICY,
        streaming_mode=STREAMING_MODE,
        stop_sequences=get_stop_sequences(),
        backend_version=BACKEND_VERSION,
        frontend_version=FRONTEND_VERSION,
        routing_mode=mm.get_mode(),
        explicit_model_override=mm.get_explicit_model_override(),
        cloud_provider=cloud_provider,
        cloud_provider_configured=cloud_provider_configured,
    )
    logger.debug(f"build_snapshot() → {snapshot.to_dict()}")
    return snapshot


# ============================================================
# DEDICATED SKR ANSWERER
#
# Self-queries are answered here, directly, deterministically, from the
# snapshot — NOT by asking the LLM to summarize the snapshot in a system
# message and hoping it stays faithful to it (the previous approach:
# conversation_manager.resolve_self_query() built grounding text and
# still routed through the active model for the actual reply). Two
# concrete problems that fixes: an under-RAM-pressure request could hit
# a safety_warning and never get answered at all, and generation is slow
# compared to just... reading the answer out of data already in memory.
# ARIA "always" answering these now literally means no model load, no
# generation, no safety gate — just formatting facts that are already
# known to be true.
#
# Intent strings are duplicated here as literals rather than imported
# from backend.core.conversation_manager, which imports this module —
# importing back would be circular. They're plain strings (conversation_
# manager.IntentType is just `str`), so there's no real type to share,
# only the literal values, which must stay in sync by hand.
# ============================================================
_SELF_QUERY_INTENT_LITERALS = frozenset({
    "model_query", "tool_query", "version_query", "capability_query",
    "persona_query", "environment_query", "mode_query", "configuration_query",
})


def is_self_query_intent(intent: Optional[str]) -> bool:
    return intent in _SELF_QUERY_INTENT_LITERALS


def describe_active_model(snapshot: "SelfKnowledgeSnapshot") -> str:
    """
    Truthful, mode-aware "what am I running on" phrase — the single
    building block every self-query answer that needs to name the active
    model/provider uses (model_query, capability_query, and the generic
    fallback answer below), so they can never drift into naming a local
    model while in Cloud Mode or a cloud provider while in Local Mode.

    Never mentions fallback/emergency/installed models — those roles are
    real RAM-pressure escape hatches, not "what you're using right now",
    and self-query replies must not describe them unless the user
    specifically asks (see answer_self_query()'s module docstring).
    """
    if snapshot.routing_mode == "cloud":
        return snapshot.provider_display_name or snapshot.provider_name or "a cloud provider"
    return snapshot.active_model_display_name or snapshot.active_model_id or "a local model"


def suggest_improvements() -> list[str]:
    """
    Real capability-gap awareness — checked live against key_manager/
    module_manager, not a canned list. Every suggestion here names an
    actual, currently-true gap the user can act on directly (add a key
    in Settings), not a vague aspiration.
    """
    from backend.core import key_manager, module_manager

    suggestions: list[str] = []

    provider_status = key_manager.list_configured_providers()
    configured_providers = [p for p, ok in provider_status.items() if ok]
    if not configured_providers:
        suggestions.append(
            "No cloud provider API keys are configured — I can only use local models right now. "
            "Add an OpenAI or Anthropic key in Settings to unlock Cloud Mode."
        )
    else:
        missing = sorted(p for p, ok in provider_status.items() if not ok)
        if "openai" in missing or "anthropic" in missing:
            named = " and ".join(p for p in ("openai", "anthropic") if p in missing)
            suggestions.append(
                f"{named.title()} {'is' if ' and ' not in named else 'are'} not configured — adding "
                f"{'it' if ' and ' not in named else 'them'} would improve coding/reasoning task quality in Cloud Mode or under automatic model selection."
            )

    module_status = module_manager.list_modules()
    unconfigured_modules = [m["name"] for m in module_status if not m["configured"]]
    if unconfigured_modules:
        suggestions.append(
            f"The following module(s) have no key configured, so their tools may not fully work: "
            f"{', '.join(unconfigured_modules)}."
        )

    if not suggestions:
        suggestions.append(
            "Nothing obvious is missing — cloud providers and modules I know about are configured. "
            "I can still only search via DuckDuckGo's Instant Answer API, which doesn't cover every query."
        )

    return suggestions


def answer_self_query(intent: str, snapshot: SelfKnowledgeSnapshot) -> str:
    """
    Deterministic, model-free answer text for a self-query intent. Always
    returns *something* sensible, even for an intent this function
    doesn't specifically recognize (falls through to a capability-style
    summary), so a caller can treat this as never failing.

    "model_query" and "mode_query" in particular must NEVER mention
    fallback/emergency/installed models, and must never name a local
    model while snapshot.routing_mode == "cloud" or a cloud provider
    while snapshot.routing_mode == "local" — see describe_active_model()
    above, which is what both branches (and capability_query, and the
    generic fallback at the bottom) build their answer from.
    """
    if intent == "model_query":
        if snapshot.routing_mode == "cloud":
            name = snapshot.provider_display_name or snapshot.provider_name or "a cloud provider"
            return f"I am using {name} (Cloud)."
        model_name = snapshot.active_model_display_name or snapshot.active_model_id or "a local model"
        if snapshot.routing_mode == "local":
            return f"I am using {model_name} (Local)."
        return f"I am using {model_name} via automatic model selection."

    if intent == "mode_query":
        if snapshot.routing_mode == "local":
            return "I am using Local Mode."
        if snapshot.routing_mode == "cloud":
            return "I am using Cloud Mode."
        return "I am using automatic model selection based on task complexity."

    if intent == "self_improvement_query":
        return " ".join(suggest_improvements())

    if intent == "tool_query":
        tools = ", ".join(snapshot.available_tools) or "none configured"
        return f"I have working tools for: {tools}. Ask me directly (e.g. \"weather in Richmond, VA\" or \"search for X\") and I'll run the real tool and answer from its actual result, not a guess."

    if intent == "version_query":
        return f"I'm {snapshot.aria_version} (backend {snapshot.backend_version}, frontend {snapshot.frontend_version})."

    if intent == "capability_query":
        tools = ", ".join(snapshot.available_tools) or "none configured"
        style = snapshot.persona_summary.split("—")[0].strip() if "—" in snapshot.persona_summary else snapshot.persona_summary
        return (
            f"I'm {style}. I'm currently running on {describe_active_model(snapshot)}, "
            f"and I have routing awareness for: {tools}."
        )

    if intent == "persona_query":
        return f"{snapshot.persona_summary} In short: {snapshot.system_prompt_summary}"

    if intent == "environment_query":
        if snapshot.routing_mode == "local":
            mode_sentence = "I am using Local Mode."
        elif snapshot.routing_mode == "cloud":
            mode_sentence = "I am using Cloud Mode."
        else:
            mode_sentence = "I am using automatic model selection based on task complexity."

        parts = [
            mode_sentence,
            f"I'm running via the '{snapshot.provider_name or snapshot.active_model_id}' provider.",
        ]

        if snapshot.explicit_model_override:
            parts.append(
                f"You've pinned me to '{snapshot.explicit_model_override}' specifically — "
                f"I'll stay on it regardless of mode until you switch modes again."
            )

        if snapshot.routing_mode == "cloud":
            if snapshot.cloud_provider and snapshot.cloud_provider_configured is False:
                parts.append(
                    f"You've set Cloud Mode to '{snapshot.cloud_provider}', but no API key is configured for it "
                    f"yet — I'm currently falling back to whichever configured cloud provider ranks next, "
                    f"or local if none are configured at all."
                )
            elif snapshot.cloud_provider:
                parts.append(f"My preferred cloud provider is '{snapshot.cloud_provider}'.")

        parts.append(snapshot.streaming_mode)
        return " ".join(parts)

    if intent == "configuration_query":
        mode = "multi-turn (last 12 messages)" if snapshot.multi_turn_mode else "single-turn (latest message only)"
        return (
            f"This conversation is in {mode} mode, currently holding {snapshot.history_length} message(s). "
            f"{snapshot.memory_policy}"
        )

    # Fallback for a self-query intent this function doesn't specifically
    # handle — still answers from real data rather than returning nothing.
    return f"I'm {snapshot.aria_version}, currently running on {describe_active_model(snapshot)}."
