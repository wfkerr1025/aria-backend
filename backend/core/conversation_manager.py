# backend/core/conversation_manager.py
#
# Conversation hygiene for the chat pipeline: the persona/system prompt,
# multi-turn vs single-turn history policy + trimming, conversation
# identity (for context resets), and — preparing for Phase 2 — dry-run
# tool-need detection. Used by both live chat entry points
# (backend/websocket/handlers.py and backend/server.py's /chat) so they
# stay in lockstep instead of drifting.
#
# What "intelligent / non-hallucinatory / on-topic" concretely means
# here: there is no separate classifier verifying the model's output —
# that would need another model call this app doesn't have. The real,
# implementable levers are conversation hygiene (bounded, relevant
# context; a clear persona; no stale history bleeding into new topics)
# and prompt engineering (the SYSTEM_PROMPT below). That's what this
# module does, and it's the honest scope of "persona enforcement" for a
# pluggable local/cloud LLM backend like this one.

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.core import search_intent
from backend.core import self_knowledge
from backend.core import model_registry as _model_registry
from backend.core.key_manager import PROVIDER_ENV_VARS as _KNOWN_PROVIDER_NAMES

from backend.logger import log as unified_log

from logger import get_logger

# Imported lazily-safe: llm_engine pulls in the memory stack, which pulls in
# the embedding backend. Both degrade to no-ops rather than raising, so an
# import failure here would be the only way memory could break chat.
try:
    from backend.llm.llm_engine import (
        FILE_SECTION,
        MEMORY_SECTION,
        build_system_prompt,
        get_file_context,
        get_memory_context,
    )
    from backend.llm import semantic_routing
except Exception:  # pragma: no cover - memory prompting is optional
    semantic_routing = None
    MEMORY_SECTION = "### Memory Context"
    FILE_SECTION = "### File Context"

    def build_system_prompt(base_prompt, query="", **_kwargs):
        return base_prompt

    def get_memory_context(query, limit=None):
        return None

    def get_file_context(query, limit=None):
        return None

logger = get_logger(__name__)


# ============================================================
# PERSONA / SYSTEM PROMPT
# ============================================================
SYSTEM_PROMPT = (
    "You are ARIA, the assistant built into ARIA-Lite — highly intelligent, "
    "capable, and helpful, in the spirit of Copilot but more thorough. You "
    "talk like a sharp, direct person, not a manual.\n\n"
    "Rules you always follow:\n"
    "1. Respond only to the user's latest message, read in light of the "
    "conversation context provided to you. If no prior context is given, "
    "treat this as the start of a new conversation.\n"
    "2. Never invent, paraphrase, or continue a user message yourself, and "
    "never simulate the back-and-forth of a conversation — write only your "
    "own single reply, not the next thing 'the user' might say. Never "
    "repeat content you or they already said earlier in this conversation.\n"
    "3. Greetings get a brief, warm reply — under 15 words, no more. Don't "
    "turn 'hi' into an introduction or a list of what you can do.\n"
    "4. Don't explain your own capabilities, tools, limitations, behavior, "
    "policies, or reasoning unless the user actually asks about them. "
    "Answer what they asked, not a description of how or why you're "
    "answering it.\n"
    "5. Default to short and to the point. Only go long — structured, "
    "detailed, thorough — when the user's request genuinely needs it or "
    "they explicitly ask for more detail.\n"
    "6. Match the user's tone. Be warm, natural, and conversational, like "
    "a knowledgeable person texting back — not stiff, not corporate, not "
    "over-formal.\n"
    "7. Do not drift onto a topic the user hasn't raised, and do not keep "
    "discussing something the user has clearly moved on from unless they "
    "explicitly bring it back up.\n"
    "8. Never state a fact — including weather, current events, or "
    "anything time-sensitive — as certain unless you actually know it. If "
    "answering well genuinely requires a tool (weather, search, etc.) and "
    "the user's question depends on it, say so plainly rather than "
    "guessing — but don't bring up tools or what you can't do when they "
    "haven't asked and it isn't relevant to answering them.\n"
    "9. If a request is ambiguous or missing something you need, ask one "
    "short clarifying question instead of guessing.\n"
    "10. You can switch your own routing mode (Local Mode, Cloud Mode, or "
    "automatic model selection) and "
    "pin yourself to a specific model when the user asks — \"switch to "
    "local mode\", \"use gpt-4\", \"stop using that model\" all actually "
    "change what you run on, not just what you say. You can use cloud "
    "providers like OpenAI and Anthropic directly when the user has "
    "configured a key for them, and you can explain which mode or model "
    "you're on and why (including falling back because a provider isn't "
    "configured) whenever asked — but don't bring any of this up "
    "unprompted."
)

DEFAULT_MAX_HISTORY_MESSAGES = 12


# ============================================================
# HISTORY POLICY (multi-turn vs single-turn, trimming)
# ============================================================
# Routing flags used when semantic_routing is unavailable: memory on, no
# tools, no files -- the behaviour this module had before routing existed.
_ROUTING_FALLBACK = {
    "intent": "chat.general",
    "confidence": 0.0,
    "use_memory": True,
    "use_tools": False,
    "use_files": False,
}


def _route_intent(message, history):
    """Classify a turn, degrading to memory-on if routing is unavailable."""
    if semantic_routing is None:
        return _ROUTING_FALLBACK
    try:
        return semantic_routing.route(message, history=history)
    except Exception:
        logger.exception("Semantic routing failed; defaulting to memory-aware prompting.")
        return _ROUTING_FALLBACK


def apply_history_policy(
    messages: Optional[List[Dict[str, Any]]],
    multi_turn: bool = True,
    max_messages: int = DEFAULT_MAX_HISTORY_MESSAGES,
    memory_context: dict | None = None,
    file_context: dict | None = None,
    suppress_context: bool = False,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Decide what actually gets sent to the model for this turn.

    - multi_turn=False: only the latest message survives (topic-drift
      guard — nothing from earlier turns bleeds into a fresh question).
    - multi_turn=True: the last `max_messages` survive; anything older is
      dropped rather than growing the prompt unbounded.

    In both cases the persona SYSTEM_PROMPT is prepended. Returns
    (final_messages, policy_info) — policy_info is the shape logged as
    the "history_policy" unified-log event, so the decision is always
    visible, not just its effect.
    """
    incoming = list(messages or [])

    if not multi_turn:
        kept = incoming[-1:]
        mode = "single_turn"
    else:
        kept = incoming[-max_messages:] if max_messages > 0 else incoming
        mode = "multi_turn"

    dropped_count = max(0, len(incoming) - len(kept))

    # Memory-aware prompting: the persona prompt is extended with what
    # ARIA remembers about whatever was just asked. Retrieval runs against
    # the latest user message only -- earlier turns are already in `kept`,
    # and re-retrieving for them would rank stale topics into this turn.
    # build_system_prompt() is fail-open: if memory is disabled or
    # unavailable it returns SYSTEM_PROMPT unchanged, so a retrieval
    # problem can never cost the user their message.
    latest_user_message = next(
        (m.get("content", "") for m in reversed(kept) if m.get("role") == "user"),
        "",
    )

    # Semantic routing decides whether this turn should consult memory at
    # all. It is heuristic and side-effect free (no model call, no
    # database), so it costs nothing to run in front of every turn and
    # saves a full retrieval pass on the turns it rules out.
    intent = _route_intent(latest_user_message, kept)

    # Retrieval happens exactly once per turn, here. The context dicts
    # carry their ranked `items` alongside the formatted summary, so the
    # same pass serves the system prompt, the policy flags, and any caller
    # that wants the raw items for further reasoning -- nothing downstream
    # needs to retrieve again.
    # Retrieval follows the router's decision alone. suppress_context is
    # about *rendering* -- whether the system prompt carries the sections --
    # not about whether the turn consults memory at all. Conflating the two
    # would starve a caller that suppressed the sections precisely because
    # it intends to use the items itself.
    want_memory = intent["use_memory"]
    want_files = intent["use_files"]

    if memory_context is None and want_memory:
        memory_context = get_memory_context(latest_user_message)
    if file_context is None and want_files:
        file_context = get_file_context(latest_user_message)

    system_prompt = build_system_prompt(
        SYSTEM_PROMPT,
        query=latest_user_message,
        use_memory=want_memory and not suppress_context,
        use_files=want_files and not suppress_context,
        memory_context=memory_context,
        file_context=file_context,
    )
    # Derived from the retrieval result, not from the assembled prompt.
    #
    # This used to substring-match MEMORY_SECTION/FILE_SECTION in the
    # system prompt, which breaks the moment something else owns the
    # evidence: a turn that used ten notes would report
    # memory_context_applied=False purely because the markers live in a
    # different message. Asking retrieval -- which knows -- is also
    # strictly more accurate than asking the prompt, because "found
    # nothing" and "never consulted" both render as no section and the
    # old test could not tell them apart.
    #
    # The marker constants stay exactly as they are; they are still what
    # build_system_prompt emits and what any consumer greps for.
    memory_applied = bool(memory_context and memory_context.get("items"))
    file_applied = bool(file_context and file_context.get("items"))

    final_messages = [{"role": "system", "content": system_prompt}] + kept

    policy_info = {
        "retrieved_items": (
            ((memory_context or {}).get("items") or [])
            + ((file_context or {}).get("items") or [])
        ),
        "mode": mode,
        "memory_context_applied": memory_applied,
        "file_context_applied": file_applied,
        "routing_intent": intent["intent"],
        "routing_confidence": intent["confidence"],
        "routing_use_memory": intent["use_memory"],
        "routing_use_tools": intent["use_tools"],
        "routing_use_files": intent["use_files"],
        "incoming_count": len(incoming),
        "kept_count": len(kept),
        "dropped_count": dropped_count,
        "max_messages": max_messages,
    }

    logger.debug(f"apply_history_policy() → {policy_info}")
    return final_messages, policy_info


def log_history_policy(subsystem: str, policy_info: Dict[str, Any], conversation_id: Optional[str]) -> None:
    unified_log(subsystem, "INFO", f"history_policy_applied: {policy_info['mode']}", {
        **policy_info, "conversation_id": conversation_id,
    })


def log_system_prompt_applied(subsystem: str, conversation_id: Optional[str]) -> None:
    unified_log(subsystem, "DEBUG", "system_prompt_applied", {
        "conversation_id": conversation_id, "prompt_chars": len(SYSTEM_PROMPT),
    })


# ============================================================
# INTENT ENGINE (Copilot-style reasoning)
#
# A heuristic classifier, not a model call — the same honesty note as
# the module docstring applies: this buys predictable routing/logging,
# not true language understanding. It feeds two things: the unified log
# (intent_detected) and a short behavioral hint appended to the system
# prompt for this turn (intent_hint), which is the mechanism that
# actually makes "greeting -> short friendly reply" or "unknown_intent ->
# ask a clarifying question" real instead of aspirational.
# ============================================================
IntentType = str  # one of the constants below; kept as str for simple JSON logging

INTENT_GREETING = "greeting"
INTENT_QUESTION = "question"
INTENT_REQUEST = "request"
INTENT_WEATHER_QUERY = "weather_query"
INTENT_SEARCH_QUERY = "search_query"
INTENT_TASK_REQUEST = "task_request"
INTENT_CLARIFICATION_NEEDED = "clarification_needed"
INTENT_MULTI_TURN_FOLLOWUP = "multi_turn_followup"
INTENT_CONTEXT_RESET = "context_reset"
INTENT_UNKNOWN = "unknown_intent"

# "switch to X" / "use X" / "change to X" / "set model to X" / "go back
# to auto mode" — a request to permanently change the active model or
# routing mode, not a question about it (that's INTENT_MODEL_QUERY).
# Resolved deterministically against real known targets (installed local
# models, known cloud providers, or the auto/local/cloud mode keywords)
# by resolve_model_switch_target() below — never guessed by the model.
INTENT_MODEL_SWITCH = "model_switch_intent"

# Self-Knowledge Registry (backend/core/self_knowledge.py) queries —
# questions about ARIA herself rather than about the world. Grouped as
# "self_query" in the unified log (see log_intent_detected) alongside
# their specific intent, since they share one resolution path
# (resolve_self_query) even though each is a distinct IntentType.
INTENT_MODEL_QUERY = "model_query"
INTENT_TOOL_QUERY = "tool_query"
INTENT_VERSION_QUERY = "version_query"
INTENT_CAPABILITY_QUERY = "capability_query"
INTENT_PERSONA_QUERY = "persona_query"
INTENT_ENVIRONMENT_QUERY = "environment_query"
# "what mode are you using?" / "which mode are you in?" — deliberately
# distinct from INTENT_ENVIRONMENT_QUERY: a mode question gets the exact
# terse "I am using Local/Cloud Mode." / "I am using automatic model
# selection..." sentence and nothing else (no provider explanation, no
# streaming/concurrency explanation — see
# self_knowledge.answer_self_query()'s "mode_query" branch), while a
# genuine "what environment/backend are you running in" question still
# gets the richer environment_query answer.
INTENT_MODE_QUERY = "mode_query"
INTENT_CONFIGURATION_QUERY = "configuration_query"
# "what could you do better" / "what's missing" — real capability-gap
# awareness (unconfigured providers/modules), not a canned answer; see
# self_knowledge.suggest_improvements().
INTENT_SELF_IMPROVEMENT_QUERY = "self_improvement_query"

SELF_QUERY_INTENTS = frozenset({
    INTENT_MODEL_QUERY, INTENT_TOOL_QUERY, INTENT_VERSION_QUERY,
    INTENT_SELF_IMPROVEMENT_QUERY,
    INTENT_CAPABILITY_QUERY, INTENT_PERSONA_QUERY, INTENT_ENVIRONMENT_QUERY,
    INTENT_MODE_QUERY, INTENT_CONFIGURATION_QUERY,
})

_GREETING_PHRASES = [
    "hi", "hello", "hey", "yo", "sup", "good morning", "good afternoon",
    "good evening", "howdy", "greetings",
]
_RESET_PHRASES = [
    "start over", "start a new chat", "new chat", "forget everything",
    "forget what we talked about", "reset the conversation", "clear history",
    "clear the conversation",
]
_TASK_VERBS = [
    "create", "build", "write", "generate", "fix", "run", "execute",
    "make", "implement", "refactor", "delete", "add", "update", "install",
    "debug", "optimize", "rename", "move", "copy",
]
_QUESTION_STARTERS = (
    "what", "why", "how", "when", "where", "who", "which",
    "is", "are", "can", "could", "does", "do", "did", "should", "would",
)

_MODEL_QUERY_PHRASES = [
    "what model", "which model", "what llm", "which llm",
    "model are you", "model is this", "what are you running on",
    "fallback model", "emergency model", "backup model",
    "models do you have", "models installed", "installed models",
]
_TOOL_QUERY_PHRASES = [
    "what tools", "which tools", "tools do you have", "tools are available",
    "available tools", "what tools can you use",
]
_VERSION_QUERY_PHRASES = [
    "what version", "which version", "version are you", "your version",
    "aria version", "what's your version",
]
_PERSONA_QUERY_PHRASES = [
    "your persona", "describe yourself", "who are you", "your personality",
    "what are you", "your system prompt", "what is your prompt",
    "show me your prompt", "your instructions",
]
_ENVIRONMENT_QUERY_PHRASES = [
    "what environment", "your environment", "where are you running",
    "what backend", "what provider are you", "local or cloud",
    "are you local or", "are you running locally",
    "using a permanent override", "are you falling back",
]
# Plain substring matching would false-positive here — "mode" is a
# literal prefix of "model"/"models" ("which mode" is the first 10
# characters of "which model"), so a naive `"what mode" in text` check
# would misclassify "what models do you have" as a mode query before
# model_query's own (later) fuzzy match ever got a chance. Word
# boundaries avoid that without needing every phrase spelled out above.
# Routes to INTENT_MODE_QUERY (not INTENT_ENVIRONMENT_QUERY) — see that
# constant's comment for why the two are kept separate.
_MODE_QUERY_MODE_PATTERN = re.compile(r"\b(what|which|current|your)\s+mode\b")
_CONFIGURATION_QUERY_PHRASES = [
    "your configuration", "how are you configured", "history policy",
    "memory policy", "multi-turn setting", "multi turn setting",
    "how much history", "how much context",
]
# Checked after the above (more specific) self-query phrases so "what can
# you do with the weather tool" still resolves as capability_query rather
# than being swallowed by a narrower match; "what can you do" alone is
# too generic to check first without shadowing everything else.
_CAPABILITY_QUERY_PHRASES = [
    "what can you do", "what do you do", "your capabilities",
    "capable of", "what are you able to do", "what can you help with",
]
_SELF_IMPROVEMENT_QUERY_PHRASES = [
    "what could you do better", "how could you improve", "what's missing",
    "what is missing", "suggest improvements", "improve yourself",
    "what would make you better", "what are you missing",
    "any missing features", "what should be added",
]

# ------------------------------------------------------------------
# Fuzzy fallback for self-queries the exact phrase lists above miss —
# e.g. "tell me your models" contains neither "what model" nor "models
# do you have" as a substring, but is unambiguously the same question.
# Requires a self-referential cue word/phrase ("your", "you have", "tell
# me", ...) AND a domain noun (model/tool/version/...) as separate whole
# words, so it generalizes across phrasing/word-order without becoming
# so loose it starts swallowing unrelated messages that merely mention
# "model" (e.g. "write a data model for this").
# ------------------------------------------------------------------
_SELF_REFERENTIAL_CUES = [
    "your", "you have", "you're", "youre", "you are", "yourself",
    "tell me", "list", "show me",
]
_SELF_QUERY_DOMAIN_WORDS: List[tuple] = [
    (re.compile(r"\bmodels?\b"), INTENT_MODEL_QUERY),
    # Checked right after "model(s)" (word-boundary matched, so it can
    # never collide with "model"/"models" themselves) — "mode" alone
    # routes here, not to environment_query; see INTENT_MODE_QUERY.
    (re.compile(r"\bmode\b"), INTENT_MODE_QUERY),
    (re.compile(r"\btools?\b"), INTENT_TOOL_QUERY),
    (re.compile(r"\bversion\b"), INTENT_VERSION_QUERY),
    (re.compile(r"\bpersona(lity)?\b"), INTENT_PERSONA_QUERY),
    (re.compile(r"\benvironment\b"), INTENT_ENVIRONMENT_QUERY),
    (re.compile(r"\bconfig(uration)?\b"), INTENT_CONFIGURATION_QUERY),
    (re.compile(r"\bcapab(le|ilit)"), INTENT_CAPABILITY_QUERY),
]


def _fuzzy_self_query_match(text: str) -> Optional[str]:
    if not any(cue in text for cue in _SELF_REFERENTIAL_CUES):
        return None
    for pattern, intent in _SELF_QUERY_DOMAIN_WORDS:
        if pattern.search(text):
            return intent
    return None


# ------------------------------------------------------------------
# MODEL / MODE SWITCH ("switch to X", "use X", "change to X", "set
# model to X", "go back to automatic model selection")
# ------------------------------------------------------------------
# "auto" is accepted as a legacy synonym (older phrasing/persisted
# state) but always normalizes to the canonical "automatic" — the old
# mode name is retired; the mode itself (Automatic Model Routing) is
# still selectable by either word.
_MODE_KEYWORDS = {"automatic", "auto", "local", "cloud"}
_MODE_ALIASES = {"auto": "automatic"}

# Ordered most-specific-phrase-first so "set model to X" isn't shadowed
# by a looser pattern. "use X" is intentionally the loosest (and most
# false-positive-prone — "use your best judgment", "use the weather
# tool") — resolve_model_switch_target() below is what actually keeps
# it safe: a captured target that doesn't resolve to a REAL installed
# model, known cloud provider, or mode keyword is not treated as a
# switch at all, so ordinary sentences fall through to normal handling.
_MODEL_SWITCH_PATTERNS = [
    re.compile(r"^set\s+model\s+to\s+(.+)$"),
    re.compile(r"^(?:switch|change)\s+to\s+(.+)$"),
    re.compile(r"^go\s+back\s+to\s+(.+)$"),
    re.compile(r"^use\s+(.+)$"),
]


def detect_model_switch_target(text: str) -> Optional[str]:
    """Raw (unvalidated) captured target from a switch-style phrase in
    already-lowercased `text`, or None if no pattern matched at all."""
    for pattern in _MODEL_SWITCH_PATTERNS:
        m = pattern.match(text)
        if m:
            return m.group(1).strip()
    return None


def resolve_model_switch_target(raw_target: str) -> Optional[Dict[str, str]]:
    """
    Resolve a raw captured phrase (e.g. "gpt-4", "mistral 7b", "local
    mode", "openai") against real known targets. Returns None if it
    doesn't match anything real — the caller must NOT treat the message
    as a model-switch in that case.

    Returns one of:
      {"kind": "model", "model_id": <registered model_id>}
      {"kind": "provider", "provider": <known cloud provider name>}
      {"kind": "mode", "mode": "automatic" | "local" | "cloud"}
    """
    target = (raw_target or "").strip().rstrip(".!?").strip()
    if not target:
        return None

    lowered = target.lower()
    if lowered.endswith(" model selection"):
        lowered = lowered[: -len(" model selection")].strip()
    elif lowered.endswith(" mode"):
        lowered = lowered[: -len(" mode")].strip()

    if lowered in _MODE_KEYWORDS:
        return {"kind": "mode", "mode": _MODE_ALIASES.get(lowered, lowered)}

    # Exact model_id match first, then a looser match against the
    # human-readable display name — "switch to mistral 7b" won't equal
    # the registry id ("mistral-7b-q4km") but does appear in its name
    # ("Mistral 7B Instruct v0.2 (Q4_K_M)").
    for model_cfg in _model_registry.list_models():
        model_id = (model_cfg.get("id") or "")
        name = (model_cfg.get("name") or "")
        if lowered == model_id.lower() or lowered == name.lower() or (lowered and lowered in name.lower()):
            return {"kind": "model", "model_id": model_id}

    if lowered in _KNOWN_PROVIDER_NAMES:
        return {"kind": "provider", "provider": lowered}

    return None


def detect_intent(latest_user_message: str, is_multi_turn_followup: bool = False) -> str:
    """
    Classify the latest user message into one of the IntentType constants.
    `is_multi_turn_followup` should reflect whether multi-turn is ON *and*
    there's already prior history for this conversation — the caller
    (websocket/handlers.py, server.py) knows that from the history policy
    it just applied, so it's passed in rather than re-derived here.
    """
    text = (latest_user_message or "").strip().lower()

    if not text:
        return INTENT_UNKNOWN

    if any(phrase in text for phrase in _RESET_PHRASES):
        return INTENT_CONTEXT_RESET

    if len(text) <= 25 and any(text == g or text.startswith(g + " ") or text.startswith(g + ",") or text == g + "!" for g in _GREETING_PHRASES):
        return INTENT_GREETING

    # Self-Knowledge Registry queries — checked before weather/search/task/
    # question so "what model are you using" doesn't fall through to the
    # generic "question" bucket, and specific phrasings (model/tool/
    # version/persona/environment/configuration) are checked before the
    # broader "what can you do" capability catch-all.
    if any(phrase in text for phrase in _MODEL_QUERY_PHRASES):
        return INTENT_MODEL_QUERY
    if any(phrase in text for phrase in _TOOL_QUERY_PHRASES):
        return INTENT_TOOL_QUERY
    if any(phrase in text for phrase in _VERSION_QUERY_PHRASES):
        return INTENT_VERSION_QUERY
    if any(phrase in text for phrase in _PERSONA_QUERY_PHRASES):
        return INTENT_PERSONA_QUERY
    if any(phrase in text for phrase in _ENVIRONMENT_QUERY_PHRASES):
        return INTENT_ENVIRONMENT_QUERY
    if _MODE_QUERY_MODE_PATTERN.search(text):
        return INTENT_MODE_QUERY
    if any(phrase in text for phrase in _CONFIGURATION_QUERY_PHRASES):
        return INTENT_CONFIGURATION_QUERY
    # Checked before the broader capability_query catch-all so "what
    # could you do better" doesn't get swallowed by "what can you do".
    if any(phrase in text for phrase in _SELF_IMPROVEMENT_QUERY_PHRASES):
        return INTENT_SELF_IMPROVEMENT_QUERY
    if any(phrase in text for phrase in _CAPABILITY_QUERY_PHRASES):
        return INTENT_CAPABILITY_QUERY

    fuzzy_intent = _fuzzy_self_query_match(text)
    if fuzzy_intent:
        return fuzzy_intent

    # Checked before task/weather/search/question detection so "switch
    # to Mistral" or "use Local Mode" aren't swallowed by _TASK_VERBS
    # ("switch" reads like a task verb) or the generic question bucket.
    # Validated against real targets (resolve_model_switch_target), so
    # this never misfires on "use your best judgment" or "use the
    # weather tool" — those don't resolve to a real model/provider/mode
    # and fall through to normal handling below.
    raw_switch_target = detect_model_switch_target(text)
    if raw_switch_target is not None and resolve_model_switch_target(raw_switch_target) is not None:
        return INTENT_MODEL_SWITCH

    # An explicit "use web search" outranks the weather keyword. Checked
    # before both so that naming the tool is honoured rather than being
    # overridden by a topic word appearing in the same sentence.
    normalized_for_tools = _normalize_for_keywords(text)
    explicitly_web = any(
        f" {phrase} " in normalized_for_tools for phrase in EXPLICIT_WEB_SEARCH
    )
    if explicitly_web and _match_tool_keywords(text, "web_search"):
        return INTENT_SEARCH_QUERY

    weather_decision = _match_tool_keywords(text, "get_weather")
    if weather_decision:
        return INTENT_WEATHER_QUERY

    search_decision = _match_tool_keywords(text, "web_search")
    if search_decision:
        return INTENT_SEARCH_QUERY

    if any(f" {verb} " in f" {text} " or text.startswith(verb + " ") for verb in _TASK_VERBS):
        return INTENT_TASK_REQUEST

    first_word = text.split()[0] if text.split() else ""
    if text.endswith("?") or first_word in _QUESTION_STARTERS:
        return INTENT_QUESTION

    if is_multi_turn_followup:
        return INTENT_MULTI_TURN_FOLLOWUP

    # A very short, low-content message with no other signal ("it", "that
    # one", "yes but why") is usually missing context rather than a
    # genuine on-topic request.
    if len(text.split()) <= 2:
        return INTENT_CLARIFICATION_NEEDED

    return INTENT_REQUEST


def _normalize_for_keywords(text: str) -> str:
    """Lowercased, punctuation-flattened and padded for whole-word matching.

    Padding both ends is what lets a single word live in TOOL_KEYWORDS
    safely: " search " cannot be found inside "research", and " recent "
    cannot be found inside "recently-added". Flattening punctuation is what
    lets "search," and "search" be the same keyword.
    """
    lowered = str(text or "").lower().replace("'", "")
    return f" {' '.join(re.sub(r'[^a-z0-9]+', ' ', lowered).split())} "


def _match_tool_keywords(text: str, tool_name: str) -> bool:
    """Whether a tool's keywords appear in the text as whole words.

    web_search additionally loses to LOCAL_SCOPE_VETO: a question about the
    user's own notes or repository is a retrieval question however it is
    phrased, and must never become a web request.
    """
    if tool_name == "web_search":
        # Delegated so the veto and the phrase list cannot be applied here
        # in one order and in the planner in another.
        #
        # search_activation reads the same vocabulary and adds this
        # turn's classifier verdict when the transport primed one. Called
        # without a generator, so this never spends an inference of its
        # own: it either finds the verdict already decided or falls back
        # to the vocabulary, and either way agrees with the planner.
        from backend.core import search_activation

        return search_activation.wants_web_search(text)

    normalized = _normalize_for_keywords(text)
    return any(
        f" {keyword} " in normalized for keyword in TOOL_KEYWORDS.get(tool_name, [])
    )


_INTENT_HINTS: Dict[str, str] = {
    INTENT_GREETING: "The user's latest message is just a greeting — reply in under 15 words, warm and natural, like texting a friend back. No introduction, no capability list, no explanation.",
    INTENT_CLARIFICATION_NEEDED: "The user's latest message is too short or vague to act on safely — ask one short clarifying question instead of guessing what they mean.",
    # Only reached in the rare case a weather/search intent surfaces
    # outside the primary short-circuit (see backend/websocket/
    # handlers.py's _answer_tool_query_directly(), which normally
    # answers these directly from the real tool result before this hint
    # table is ever consulted) — a defensive fallback, not the normal path.
    INTENT_WEATHER_QUERY: "The user is asking about weather. You do not have live weather data and must not guess it — say you'd need to actually look it up.",
    INTENT_SEARCH_QUERY: "The user is asking for current/external information a web search would answer. You must not invent an answer — say you'd need to actually search for it.",
    INTENT_CONTEXT_RESET: "The user is asking to start over or forget prior context — treat this message as the start of a fresh conversation.",
}


def intent_hint(intent: str) -> Optional[str]:
    """
    Short behavioral instruction to append for this turn, or None.
    Self-query intents (model_query, tool_query, ...) are deliberately
    absent from this table — they're grounded with real SKR data via
    resolve_self_query() instead of a generic hint; see
    backend/websocket/handlers.py and backend/server.py.
    """
    return _INTENT_HINTS.get(intent)


def log_intent_detected(subsystem: str, intent: str, conversation_id: Optional[str]) -> None:
    label = f"self_query ({intent})" if intent in SELF_QUERY_INTENTS else intent
    unified_log(subsystem, "INFO", f"intent_detected: {label}", {
        "intent": intent, "is_self_query": intent in SELF_QUERY_INTENTS,
        "conversation_id": conversation_id,
    })


# ============================================================
# SELF-KNOWLEDGE QUERY RESOLUTION
#
# Grounds a self-query turn in real facts from backend.core.self_knowledge
# instead of leaving the model to guess. The resolved text is injected as
# an additional system message (same mechanism as intent_hint()) — it
# names the exact real values and tells the model to use only those, so
# an answer about "what model are you running" is sourced from the
# actual resolved model_id for this turn, not a plausible-sounding guess.
# ============================================================
def resolve_self_query(intent: str, snapshot: self_knowledge.SelfKnowledgeSnapshot) -> Optional[str]:
    if intent == INTENT_MODEL_QUERY:
        # Backend truth only — see self_knowledge.answer_self_query()'s
        # "model_query" branch for the deterministic version of this same
        # rule, which is what real callers actually use (this hint-based
        # path is a defensive fallback for a self-query somehow reaching
        # LLM generation instead of the short-circuit). Never mentions
        # other model roles (fallback/emergency/the full install list),
        # and never names a local model while in Cloud Mode or a cloud
        # provider while in Local Mode — the same absolute mode
        # separation the routing layer enforces.
        if snapshot.routing_mode == "cloud":
            return (
                f"The user is asking which model/provider you're using. Real fact — you are using "
                f"'{snapshot.provider_display_name or snapshot.provider_name or 'a cloud provider'}' "
                f"(Cloud). State only this. Do not mention any local model or invent a specific cloud "
                f"model name. Answer in 1 sentence."
            )
        model_name = snapshot.active_model_display_name or snapshot.active_model_id or "a local model"
        mode_phrase = (
            "via automatic model selection" if snapshot.routing_mode == "automatic" else "(Local)"
        )
        return (
            f"The user is asking which model you're using. Real fact — you are using '{model_name}' "
            f"{mode_phrase}. State only this. Do not invent a different model name, and do not mention "
            f"other model roles (the fallback role, the emergency role, or the full list of what's "
            f"installed) unless specifically asked. Answer in 1 sentence."
        )
    if intent == INTENT_TOOL_QUERY:
        tools = ", ".join(snapshot.available_tools) or "none configured"
        return (
            f"The user is asking what tools you have. Real fact — available tools: {tools}. "
            f"{snapshot.tool_routing_rules} Answer using only this list. Do not invent other tools. "
            f"Answer in 1-2 sentences."
        )
    if intent == INTENT_VERSION_QUERY:
        return (
            f"The user is asking your version. Real facts — ARIA version: '{snapshot.aria_version}'; "
            f"backend version: '{snapshot.backend_version}'; frontend version: '{snapshot.frontend_version}'. "
            f"State these exactly. Do not invent a different version. Answer in 1 sentence."
        )
    if intent == INTENT_CAPABILITY_QUERY:
        tools = ", ".join(snapshot.available_tools) or "none configured"
        running_on = self_knowledge.describe_active_model(snapshot)
        return (
            f"The user is asking what you can do. Summarize briefly using only these real facts — "
            f"persona: {snapshot.persona_summary} Tool awareness (routing only, not yet executing): {tools}. "
            f"Conversation memory: {snapshot.memory_policy} You are currently running on {running_on}. "
            f"Do not invent capabilities beyond these, and do not mention other model roles (fallback, "
            f"emergency, or the full installed list) unless specifically asked. Answer in 2-3 sentences."
        )
    if intent == INTENT_PERSONA_QUERY:
        return (
            f"The user is asking about your persona or system prompt. Give a brief, high-level summary — "
            f"you were only given a summary here, not the verbatim system prompt, so there is nothing to "
            f"quote word-for-word. Persona: {snapshot.persona_summary} Behavior summary: "
            f"{snapshot.system_prompt_summary} Answer in 2-3 sentences, in your own words."
        )
    if intent == INTENT_ENVIRONMENT_QUERY:
        override_fact = (
            f"; you are explicitly pinned to model '{snapshot.explicit_model_override}' regardless of mode"
            if snapshot.explicit_model_override else ""
        )
        fallback_fact = (
            f"; Cloud Mode's chosen provider '{snapshot.cloud_provider}' has no API key configured, so you "
            f"are currently falling back to another configured provider or local"
            if snapshot.routing_mode == "cloud" and snapshot.cloud_provider and snapshot.cloud_provider_configured is False
            else ""
        )
        return (
            f"The user is asking about your runtime environment. Real facts — provider: "
            f"'{snapshot.provider_name or 'not yet resolved'}'; routing mode: {snapshot.routing_mode}"
            f"{override_fact}{fallback_fact}; streaming: {snapshot.streaming_mode} "
            f"State these plainly. Do not invent infrastructure details not listed here. "
            f"Answer in 1-2 sentences."
        )
    if intent == INTENT_MODE_QUERY:
        # Deliberately terse and standalone — Section 4's requirement: no
        # provider explanation, no streaming-engine explanation, no
        # concurrency explanation unless the user actually asks for it.
        if snapshot.routing_mode == "local":
            mode_sentence = "I am using Local Mode."
        elif snapshot.routing_mode == "cloud":
            mode_sentence = "I am using Cloud Mode."
        else:
            mode_sentence = "I am using automatic model selection based on task complexity."
        return (
            f"The user is asking what mode you're in. Reply with exactly this sentence and nothing "
            f"else: \"{mode_sentence}\" Do not add a provider explanation, streaming explanation, or "
            f"concurrency explanation unless the user specifically asks for one."
        )
    if intent == INTENT_CONFIGURATION_QUERY:
        mode = "multi-turn (keeps the last 12 messages)" if snapshot.multi_turn_mode else "single-turn (only the latest message)"
        return (
            f"The user is asking about your configuration. Real facts — conversation mode: {mode}; "
            f"history currently holds {snapshot.history_length} message(s) for this conversation. "
            f"{snapshot.memory_policy} State these exactly. Do not invent settings not listed here. "
            f"Answer in 1-2 sentences."
        )
    return None


def log_self_query_resolved(subsystem: str, intent: str, snapshot: self_knowledge.SelfKnowledgeSnapshot, conversation_id: Optional[str]) -> None:
    unified_log(subsystem, "INFO", f"self_query_resolved: {intent}", {
        "intent": intent,
        "conversation_id": conversation_id,
        "active_model_id": snapshot.active_model_id,
        "provider_name": snapshot.provider_name,
        "aria_version": snapshot.aria_version,
    })


# ============================================================
# CONVERSATION IDENTITY / CONTEXT RESET
# ============================================================
def new_conversation_id() -> str:
    return uuid.uuid4().hex


def log_context_reset(subsystem: str, previous_conversation_id: Optional[str], new_id: str) -> None:
    logger.info(f"context_reset: {previous_conversation_id} -> {new_id}")
    unified_log(subsystem, "INFO", "context_reset", {
        "previous_conversation_id": previous_conversation_id,
        "new_conversation_id": new_id,
    })


# ============================================================
# PHASE 2 PREP — dry-run tool-need detection
#
# Detection + logging only. Nothing here calls a tool, builds a real
# tool_request the executor would act on, or touches
# backend.tool_execution / backend.toolchain.registry (both already
# exist and already work — see tools/get_weather.py, tools/web_search.py,
# tools/http_fetch.py — this module intentionally does not invoke them
# yet, per "do NOT integrate tools yet").
# ============================================================
TOOL_KEYWORDS: Dict[str, List[str]] = {
    "get_weather": [
        "weather", "temperature", "forecast", "raining", "snowing",
        "humidity", "wind speed", "is it cold", "is it hot",
    ],
    # Imported, not retyped. This list and backend/planning/plan_builder.py's
    # SEARCH_WORDS were maintained separately and drifted: this one was
    # widened to catch bare imperatives, that one was not, so a query could
    # route as a search and then plan no search step -- classified as needing
    # the web, then answered from the model's weights. See
    # backend/core/search_intent.py for the full account.
    "web_search": list(search_intent.WEB_SEARCH_PHRASES),
}

# Re-exported so existing importers keep working; search_intent owns them.
LOCAL_SCOPE_VETO: List[str] = list(search_intent.LOCAL_SCOPE_PHRASES)
EXPLICIT_WEB_SEARCH: List[str] = list(search_intent.EXPLICIT_WEB_SEARCH)


@dataclass
class ToolRoutingDecision:
    needs_tool: bool
    tool: Optional[str] = None
    matched_keyword: Optional[str] = None
    confidence: str = "none"
    context: Dict[str, Any] = field(default_factory=dict)


def detect_tool_need(user_text: str) -> ToolRoutingDecision:
    """
    Heuristic, keyword-based "would a tool answer this better" check.
    Deliberately simple (a real intent classifier is future work) —
    Phase 2's job is the routing *scaffold* and its logging, not a smart
    detector. Always dry-run: the caller logs the decision and continues
    with normal chat inference regardless of the result.
    """
    text = (user_text or "").lower()

    for tool_name, keywords in TOOL_KEYWORDS.items():
        for keyword in keywords:
            if keyword in text:
                return ToolRoutingDecision(
                    needs_tool=True,
                    tool=tool_name,
                    matched_keyword=keyword,
                    confidence="heuristic",
                )

    return ToolRoutingDecision(needs_tool=False)


def log_tool_routing_decision(subsystem: str, decision: ToolRoutingDecision, conversation_id: Optional[str]) -> None:
    if decision.needs_tool:
        unified_log(subsystem, "INFO", f"tool_routing_decision (dry-run): {decision.tool}", {
            "tool": decision.tool,
            "matched_keyword": decision.matched_keyword,
            "confidence": decision.confidence,
            "conversation_id": conversation_id,
            "executed": False,
        })
    else:
        unified_log(subsystem, "DEBUG", "tool_routing_decision (dry-run): none", {
            "conversation_id": conversation_id, "executed": False,
        })


# ============================================================
# RESPONSE OPTIMIZER
#
# The primary defense against a simulated/hallucinated conversation is
# now at the source — backend/llm/providers/local_provider.py passes
# stop sequences to llama.cpp so it halts generation the instant it
# starts a new "user:"/"system:" turn, instead of generating the whole
# fabricated exchange and only filtering it out afterward. This is the
# safety net behind that: a provider-agnostic cleanup pass for whatever
# a stop sequence doesn't catch (a cloud provider without stop-sequence
# control, a model that ignores it, or plain repetition loops, which stop
# sequences don't address at all).
#
# Where this actually changes what the user sees vs. where it's only
# observability differs by entry point:
#   - server.py's /chat buffers the full reply before responding, so
#     applying this there is a real, effective edit.
#   - websocket/handlers.py streams tokens live; by the time the full
#     text is available to run this against, those tokens are already
#     sent and rendered. It's applied there too, but only to log what
#     the optimizer found — the honest fix for the streaming path is the
#     stop-sequence prevention above, not a post-hoc filter that can't
#     un-send anything.
# ============================================================
_TURN_BOUNDARY_MARKERS = [
    "\nuser:", "\nUser:", "\nUSER:",
    "\nsystem:", "\nSystem:", "\nSYSTEM:",
]


def optimize_response(text: str) -> tuple[str, Dict[str, Any]]:
    """
    Returns (optimized_text, optimize_info). optimize_info always reports
    what was found/changed, even when nothing needed trimming, so
    "response_optimized" is a meaningful log line rather than a formality.
    """
    original = text or ""
    working = original
    truncated_at_turn_marker = False

    earliest_marker_pos = None
    for marker in _TURN_BOUNDARY_MARKERS:
        pos = working.find(marker)
        if pos != -1 and (earliest_marker_pos is None or pos < earliest_marker_pos):
            earliest_marker_pos = pos

    if earliest_marker_pos is not None:
        working = working[:earliest_marker_pos].rstrip()
        truncated_at_turn_marker = True

    # Repetition guard: collapse consecutive duplicate non-blank lines
    # (a model stuck in a loop tends to repeat a line or short paragraph
    # verbatim several times in a row).
    lines = working.split("\n")
    deduped_lines: List[str] = []
    removed_repeated_lines = 0
    for line in lines:
        if deduped_lines and line.strip() and line.strip() == deduped_lines[-1].strip():
            removed_repeated_lines += 1
            continue
        deduped_lines.append(line)
    working = "\n".join(deduped_lines)

    # Collapse 3+ consecutive blank lines down to a single blank line.
    while "\n\n\n" in working:
        working = working.replace("\n\n\n", "\n\n")

    working = working.strip()

    optimize_info = {
        "original_length": len(original),
        "final_length": len(working),
        "truncated_at_turn_marker": truncated_at_turn_marker,
        "removed_repeated_lines": removed_repeated_lines,
        "changed": working != original.strip(),
    }

    logger.debug(f"optimize_response() → {optimize_info}")
    return working, optimize_info


def log_response_optimized(subsystem: str, optimize_info: Dict[str, Any], conversation_id: Optional[str]) -> None:
    level = "WARNING" if (optimize_info["truncated_at_turn_marker"] or optimize_info["removed_repeated_lines"]) else "DEBUG"
    unified_log(subsystem, level, "response_optimized", {
        **optimize_info, "conversation_id": conversation_id,
    })
