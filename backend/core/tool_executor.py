# backend/core/tool_executor.py
#
# Real execution for the two tools ARIA-Lite ships (tools/get_weather.py,
# tools/web_search.py). Intent detection for these already existed
# (conversation_manager.INTENT_WEATHER_QUERY / INTENT_SEARCH_QUERY via
# TOOL_KEYWORDS/detect_tool_need) — until now it only ever produced a
# hint telling the model "a tool would answer this, but tool execution
# isn't wired up yet". This module is what actually runs them and turns
# the result into a deterministic reply, the same "answer from real
# data, not a model guess" philosophy as backend.core.self_knowledge —
# no model call, so a tool answer can never hallucinate a temperature or
# a search result that doesn't exist.

from __future__ import annotations

import re
from typing import Optional

from logger import get_logger

logger = get_logger(__name__)


# ============================================================
# WEATHER
# ============================================================
_LOCATION_PATTERNS = [
    re.compile(r"\bweather\s+(?:in|for|at)\s+(.+?)[\.\?!]*$", re.IGNORECASE),
    re.compile(r"\bin\s+(.+?)\s+(?:right now|today|tomorrow)?[\.\?!]*$", re.IGNORECASE),
    # Appended, never reordered: the two patterns above still match
    # first, so this only fires where extraction previously gave up and
    # returned None. "Forecast for Oslo" and "temperature at Heathrow"
    # are ordinary phrasings that used to reach the caller as "ask which
    # city", which is a worse answer than the one now available.
    re.compile(
        r"\b(?:forecast|temperature|conditions|humidity)\s+(?:in|for|at)\s+(.+?)[\.\?!]*$",
        re.IGNORECASE,
    ),
]


def extract_weather_location(text: str) -> Optional[str]:
    """
    Best-effort location extraction from a free-text weather question
    ("what's the weather in Richmond VA" -> "Richmond VA"). Heuristic,
    not NLP — good enough for the common "weather in/for/at X" phrasing;
    falls back to None (caller asks the user to clarify) rather than
    guessing a wrong location.
    """
    t = (text or "").strip()
    for pattern in _LOCATION_PATTERNS:
        m = pattern.search(t)
        if m:
            location = m.group(1).strip()
            if location:
                return location
    return None


def run_weather_tool(location: str) -> dict:
    from tools.get_weather import get_weather
    return get_weather({"location": location})


def format_weather_reply(result: dict) -> str:
    if result.get("status") != "ok":
        error = result.get("error", "unknown error")
        return (
            f"I couldn't get the weather for that location ({error}). "
            f"If this keeps happening, a weather API key may need to be added in Settings."
        )

    location = result.get("location", "that location")
    data = result.get("data", {})
    provider = result.get("provider", "a weather provider")

    # Provider result shapes differ (NOAA vs WeatherAPI vs Open-Meteo —
    # see tools/get_weather.py's three get_weather_*() functions) —
    # report whichever fields are actually present rather than assuming
    # one fixed schema.
    parts = [f"Weather for {location} (via {provider}):"]

    if data.get("temperature_f") is not None:
        parts.append(f"{data['temperature_f']}°F")
    elif data.get("temperature_c") is not None:
        parts.append(f"{data['temperature_c']}°C")

    if data.get("condition"):
        parts.append(str(data["condition"]))
    elif data.get("detailed"):
        parts.append(str(data["detailed"]))

    wind = data.get("wind_mph") or data.get("wind_kph") or data.get("wind_speed") or data.get("windspeed")
    if wind is not None:
        unit = "mph" if "wind_mph" in data else ("kph" if "wind_kph" in data else "")
        parts.append(f"wind {wind}{(' ' + unit) if unit else ''}")

    return " ".join(parts)


# ============================================================
# WEB SEARCH
# ============================================================
_SEARCH_PREFIXES = re.compile(
    r"^(search for|search|look up|google|find|what is|what's|who is|who's)\s+",
    re.IGNORECASE,
)


def extract_search_query(text: str) -> str:
    t = (text or "").strip()
    t = _SEARCH_PREFIXES.sub("", t).strip()
    return t.rstrip("?.! ") or text.strip()


def run_search_tool(query: str) -> dict:
    from tools.web_search import web_search
    return web_search(query)


def format_search_reply(result: dict) -> str:
    if result.get("status") != "ok":
        error = result.get("error", "the search failed")
        return f"I couldn't search for that ({error})."

    summary = result.get("summary")
    heading = result.get("heading")
    url = result.get("source_url")

    if summary:
        text = f"{heading}: {summary}" if heading else summary
        if url:
            text += f" ({url})"
        return text

    related = result.get("related") or []
    if related:
        first = related[0]
        text = first.get("text")
        if text:
            return text + (f" ({first['url']})" if first.get("url") else "")

    return "I searched, but didn't get a clear answer back — DuckDuckGo's instant-answer API doesn't cover every query. Try rephrasing, or a more specific question."
