# backend/core/weather_nl.py

"""
Batch 3.6 — natural-language weather intent handling, routed through
backend.core.weather_router's fusion-engine-backed
get_weather_truthful() instead of the legacy single-provider chain
(tools.get_weather.py, via backend.core.tool_executor.run_weather_tool()
/ format_weather_reply()) that previously served every NL "what's the
weather" chat reply.

Shared by backend/websocket/handlers.py and backend/rest/router.py so
the two live chat entry points can't drift into different weather
behavior. Location EXTRACTION still reuses backend.core.tool_executor.
extract_weather_location() (a real, working heuristic for "weather
in/for/at X" phrasing — not being replaced, still used by the
tool-registry path's schema-driven "location" parameter too); only
RESOLUTION and REPLY FORMATTING move to the fusion engine here.

WEATHER_MODEL_SENTINEL replaces the old "weather" modelId sentinel —
deliberately renamed, not reused, so nothing downstream can mistake it
for the retired single-provider path, and so a straightforward grep for
modelId == "weather" turns up nothing after this batch.
"""

from __future__ import annotations

from typing import Optional, Tuple

from backend.core.tool_executor import extract_weather_location
from backend.core import weather_router
from backend.core.weather_router import WeatherPacket, WeatherError

WEATHER_MODEL_SENTINEL = "weather-fusion"

CLARIFICATION_PROMPT = 'Which location\'s weather do you want? (e.g. "weather in Richmond, VA")'

# Deliberately plain substring matching, not an LLM call — a correction
# phrase only ever triggers a re-ask for location (see
# backend/websocket/handlers.py's _ask_weather_clarification()), never
# a generated response, so a false positive here just means an
# unnecessary "which location?" prompt, not a hallucinated answer.
_CORRECTION_PHRASES = (
    "that is incorrect", "that's incorrect",
    "that is wrong", "that's wrong",
    "not correct", "that's not correct",
    "that's not right", "that is not right",
    "that's inaccurate", "that is inaccurate",
    "wrong answer", "try again",
)


def is_correction_phrase(text: str) -> bool:
    """
    True if `text` reads as "that answer was wrong" — used ONLY to
    decide whether to re-ask for a location (see the module docstring);
    never used to decide what the correct answer actually is, so a
    generic phrase like "try again" is safe to match broadly here.
    """
    t = (text or "").strip().lower()
    return any(p in t for p in _CORRECTION_PHRASES)


def format_weather_reply(packet: WeatherPacket) -> str:
    """
    Human-readable NL reply built ENTIRELY from the fused WeatherPacket
    — every number and word here traces back to a real provider sample
    (see backend.core.weather_fusion), never model-generated text.
    Deliberately terse per the batch's own example ("Weather for
    Orange, VA (22960): 66°F, Cloudy, wind 3.6 mph") — provider list and
    confidence are omitted from the user-visible string (available via
    diagnostics_weather_result/tool_execute_result for anyone who wants
    them, not needed in every chat reply).
    """
    temp_f = packet.temperatureC * 9.0 / 5.0 + 32.0
    parts = [f"Weather for {packet.location}: {temp_f:.0f}°F, {packet.conditionsText}"]
    if packet.windSpeedMps is not None:
        wind_mph = packet.windSpeedMps * 2.23694
        parts.append(f"wind {wind_mph:.1f} mph")
    return ", ".join(parts) + "."


def format_weather_error_reply(error: WeatherError) -> str:
    return (
        f"I couldn't get the weather for that location ({error.message}). "
        f"Try naming the city and state, e.g. \"weather in Richmond, VA\"."
    )


def resolve_weather_reply(location: str) -> Tuple[str, Optional[WeatherPacket]]:
    """
    Given an already-extracted (non-empty) location string, resolves it
    through backend.core.weather_router's fusion-engine path.

    Returns (reply_text, packet_or_None) — packet is None on failure,
    but reply_text is always a real, non-hallucinated string either way
    (either the fused data or a structured "couldn't get it" message —
    never an LLM-generated guess at a temperature).
    """
    packet, error = weather_router.get_weather_truthful(location)
    if error is not None:
        return format_weather_error_reply(error), None
    return format_weather_reply(packet), packet
