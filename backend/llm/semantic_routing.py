"""ARIA Lite - semantic routing.

Decides, per user message, which subsystems a turn should activate: memory,
tools, files, or none of them (plain chat). The point is to stop paying for
retrieval on turns that cannot benefit from it -- "what's 2+2" should not
embed a query, search the note store and rank the results before answering.

This first version is deliberately heuristic: phrase and shape matching over
the message text. No embedding call, no database access, no model call,
nothing cached or mutated -- classify_intent() is a pure function of its
arguments, which is what lets it sit in front of every turn without becoming
the thing that makes turns slow.

A heuristic router is wrong sometimes, so the failure modes are chosen
rather than accidental. Memory is the default for anything unrecognised: a
missed retrieval costs the user a worse answer, while an unnecessary one
costs a few hundred milliseconds. The intents that switch memory OFF are
only the ones whose answers cannot come from the note store.

Rules are applied in a fixed priority order (memory, weather, code, files,
chat). Order matters where a message matches more than one -- "why did that
error happen, you mentioned it earlier" is a recall question about an error,
not a debugging request -- and PRIORITY is one list, so changing that
judgement is a reordering rather than a rewrite.
"""

from __future__ import annotations

import re

# ======================================================
# Intents
# ======================================================
MEMORY_RECALL = "memory.recall"
WEATHER_QUERY = "weather.query"
CODE_HELP = "code.help"
FILES_QUERY = "files.query"
CHAT_GENERAL = "chat.general"

# Which subsystems each intent turns on. chat.general keeps memory because
# an unclassified message is exactly the case where recall might help and
# nothing else can.
INTENT_FLAGS = {
    MEMORY_RECALL: {"use_memory": True, "use_tools": False, "use_files": False},
    WEATHER_QUERY: {"use_memory": False, "use_tools": True, "use_files": False},
    CODE_HELP: {"use_memory": False, "use_tools": True, "use_files": False},
    FILES_QUERY: {"use_memory": False, "use_tools": False, "use_files": True},
    CHAT_GENERAL: {"use_memory": True, "use_tools": False, "use_files": False},
}

# ======================================================
# Signals
# ======================================================
# Phrases that point at something the user already told ARIA. These are
# multi-word on purpose: single words like "before" fire on far too much
# ordinary English to be evidence of anything.
MEMORY_PHRASES = (
    "what did i tell you",
    "what did i say",
    "do you remember",
    "remind me",
    "you said",
    "you mentioned",
    "earlier you mentioned",
    "we discussed",
    "last time",
    "previously",
    "my preferences",
    "my settings",
    "what do you know about me",
    "my notes",
)

WEATHER_WORDS = (
    "weather", "forecast", "temperature", "rain", "raining", "snow",
    "snowing", "wind", "windy", "humidity", "sunny", "cloudy", "storm",
)

TIME_PHRASES = (
    "today", "tomorrow", "tonight", "this morning", "this afternoon",
    "this evening", "this week", "this weekend", "next week", "right now",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)

CODE_WORDS = (
    "error", "stack trace", "stacktrace", "traceback", "exception",
    "compile", "compiler", "runtime", "segfault", "syntax error",
    "null pointer", "undefined is not", "debug", "breakpoint",
)

FILE_WORDS = (
    "file", "files", "document", "documents", "pdf", "spreadsheet",
    "attachment", "folder", "directory",
)
FILE_PHRASES = (
    "search my files", "summarize this file", "search my documents",
    "in this document", "read the file", "open the file",
)

# Shapes that say "this is code" without any keyword being present.
CODE_SHAPES = (
    re.compile(r"```"),                                  # fenced block
    re.compile(r"\bdef\s+\w+\s*\("),                     # python def
    re.compile(r"\b(function|const|let|var)\s+\w+"),     # js declaration
    re.compile(r"\b(import|from)\s+\w+\s+(import|as)\b"),
    re.compile(r"\bclass\s+\w+\s*[:({]"),
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"\bat\s+[\w./\\]+:\d+"),                 # file:line
    re.compile(r"\w+\.(py|js|ts|java|cs|cpp|go|rs):\d+"),
    re.compile(r"[;{}]\s*$", re.MULTILINE),
)

# A location is hard to detect without a gazetteer. What is detectable is
# the shape English uses to introduce one: a preposition followed by a
# capitalised word ("in Berlin", "for New York"). This misses lowercase
# place names and that is fine -- it only adjusts confidence, never whether
# the weather path is taken.
LOCATION_PATTERN = re.compile(r"\b(?:in|at|for|near|around)\s+[A-Z][\w-]+")

# Confidence floors. A single weak signal is a guess; several agreeing
# signals, or an unambiguous phrase, is not.
CONFIDENCE_STRONG = 0.9
CONFIDENCE_CLEAR = 0.75
CONFIDENCE_WEAK = 0.55
CONFIDENCE_DEFAULT = 0.4

PRIORITY = (MEMORY_RECALL, WEATHER_QUERY, CODE_HELP, FILES_QUERY)


# ======================================================
# Feature extraction
# ======================================================
def _matches(haystack: str, needles) -> list[str]:
    return [needle for needle in needles if needle in haystack]


def extract_features(message: str) -> dict:
    """Every signal the rules look at, computed once.

    Returned on the result as raw_features so a misrouted message can be
    explained rather than guessed at.
    """
    text = message or ""
    lowered = text.lower()

    return {
        "memory_phrases": _matches(lowered, MEMORY_PHRASES),
        "weather_words": _matches(lowered, WEATHER_WORDS),
        "time_phrases": _matches(lowered, TIME_PHRASES),
        "has_location": bool(LOCATION_PATTERN.search(text)),
        "code_words": _matches(lowered, CODE_WORDS),
        "code_shapes": [pattern.pattern for pattern in CODE_SHAPES if pattern.search(text)],
        "file_words": _matches(lowered, FILE_WORDS),
        "file_phrases": _matches(lowered, FILE_PHRASES),
        "length": len(text),
    }


# ======================================================
# Rules
# ======================================================
def _score_memory(features: dict) -> float | None:
    if not features["memory_phrases"]:
        return None
    return CONFIDENCE_STRONG if len(features["memory_phrases"]) > 1 else CONFIDENCE_CLEAR


def _score_weather(features: dict) -> float | None:
    if not features["weather_words"]:
        return None
    # The specified rule is a weather word plus a location or time phrase.
    # A bare "what's the weather?" still routes to weather -- refusing it
    # would send the one unambiguous weather question to plain chat -- but
    # it scores lower, so the qualified form remains distinguishable.
    if features["has_location"] or features["time_phrases"]:
        return CONFIDENCE_STRONG
    return CONFIDENCE_WEAK


def _score_code(features: dict) -> float | None:
    words, shapes = features["code_words"], features["code_shapes"]
    if not words and not shapes:
        return None
    if words and shapes:
        return CONFIDENCE_STRONG
    if shapes:
        return CONFIDENCE_CLEAR
    # A keyword alone is the weakest case: "error" appears in plenty of
    # sentences that are not debugging requests.
    return CONFIDENCE_WEAK


def _score_files(features: dict) -> float | None:
    if features["file_phrases"]:
        return CONFIDENCE_STRONG
    if features["file_words"]:
        return CONFIDENCE_WEAK
    return None


_SCORERS = {
    MEMORY_RECALL: _score_memory,
    WEATHER_QUERY: _score_weather,
    CODE_HELP: _score_code,
    FILES_QUERY: _score_files,
}


# ======================================================
# Public API
# ======================================================
def classify_intent(message: str, history: list[dict] | None = None) -> dict:
    """Classify one message into an intent and subsystem flags.

    history is accepted for signature stability -- a later version will use
    the previous turns to disambiguate a follow-up like "and tomorrow?" --
    but this version does not read it, which keeps classification a pure
    function of the message and therefore trivially reproducible.
    """
    features = extract_features(message)

    for intent in PRIORITY:
        confidence = _SCORERS[intent](features)
        if confidence is not None:
            return {
                "intent": intent,
                "confidence": confidence,
                **INTENT_FLAGS[intent],
                "raw_features": features,
            }

    return {
        "intent": CHAT_GENERAL,
        "confidence": CONFIDENCE_DEFAULT,
        **INTENT_FLAGS[CHAT_GENERAL],
        "raw_features": features,
    }


def route(message: str, history: list[dict] | None = None) -> dict:
    """Routing decision for a turn.

    Currently a straight pass-through to classify_intent(). It exists as its
    own entry point because routing is where later phases will add decisions
    classification should not make -- budget, user overrides, whether a tool
    is actually reachable -- and callers should not have to move when that
    happens.
    """
    return classify_intent(message, history=history)
