# backend/core/weather_router.py

"""
Batch 3 established this as the single, centralized entry point every
weather request goes through. Batch 3.5 replaces its single-provider
resolution (NOAA, then WeatherAPI, then Open-Meteo, first-success-wins,
via tools.get_weather.get_weather()) with backend.core.weather_fusion's
multi-provider BLEND — NOAA + Open-Meteo + OpenWeatherMap + WeatherAPI
combined into one confidence-scored result, rather than picking exactly
one. tools/get_weather.py itself is untouched and still real/callable
(the NL chat weather short-circuit in backend/websocket/handlers.py and
backend/server.py/backend/rest/router.py still uses it directly, exactly
as before Batch 3.5 — this module was never their only path to weather
data, and changing that is out of this batch's scope).

Geocoding a free-text location into (lat, lon) is now a required first
step (the fusion engine is lat/lon-native) — done via
backend.core.open_meteo_provider.geocode(), Open-Meteo's own free,
keyless geocoding API, so resolving "Richmond, VA" into coordinates
never requires a paid key any more than the weather lookup itself does.

Every field a caller needs (temperatureC, conditionsText, windSpeedMps,
humidityPercent, confidence, location, timestamp) is guaranteed present
on success — WeatherPacket.samples is the only field that can
legitimately be a single-entry list (one provider succeeded) rather than
multiple; the packet is never partial.

Deliberately does NOT hard-require WeatherAPI (or any single provider)
for the general "what's the weather" path: NOAA, Open-Meteo, and — once
Batch 3.5 lands — the fusion blend as a whole are real, legitimate,
freely-available sources, and denying a user real weather data because
they haven't configured a specific paid provider would make this
feature worse, not more truthful. backend.core.weather_provider's
WEATHER_PROVIDER_NOT_CONFIGURED gate is reserved for a caller that
explicitly needs WeatherAPI BY NAME (require_weatherapi=True).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.core import weather_provider
from backend.core import open_meteo_provider
from backend.core import weather_fusion
from backend.core.weather_types import ProviderWeatherSample, FusionError

from backend.logger import log as unified_log
from logger import get_logger

logger = get_logger(__name__)

MISSING_LOCATION = "MISSING_LOCATION"
WEATHER_FETCH_FAILED = "WEATHER_FETCH_FAILED"
GEOCODING_FAILED = "GEOCODING_FAILED"
WEATHER_FUSION_FAILED = weather_fusion.WEATHER_FUSION_FAILED


# Batch 3 diagnostics — "last successful weather call" / "last error",
# read by ipc_router._handle_diagnostics_weather(). Deliberately just
# the two most recent facts (not a ring buffer — see
# backend.core.routing_history's identical reasoning for the same
# shape), updated at the bottom of get_weather_truthful()'s every
# return path so it's always in sync with what actually happened, never
# independently re-derived.
_last_success: Optional[Dict[str, Any]] = None
_last_error: Optional[Dict[str, Any]] = None


def get_last_call_history() -> Dict[str, Optional[Dict[str, Any]]]:
    return {"last_success": _last_success, "last_error": _last_error}


def _record_success(packet: "WeatherPacket") -> None:
    global _last_success
    _last_success = {**packet.to_dict()}


def _record_error(error: "WeatherError", location: str) -> None:
    global _last_error
    _last_error = {"code": error.code, "message": error.message, "location": location, "timestamp": time.time()}


@dataclass(frozen=True)
class WeatherError:
    code: str
    message: str


@dataclass(frozen=True)
class WeatherPacket:
    """
    Guaranteed-truthful FUSED weather result (Batch 3.5). Every field
    except `samples` is non-null by construction — get_weather_truthful()
    never returns a WeatherPacket with a null temperatureC/
    conditionsText/location/timestamp; it returns a WeatherError instead.
    `samples` always has at least one entry (fusion requires at least
    one successful provider) but may have just one — see `confidence`
    for whether that single-source result should be trusted less.
    """
    location: str
    lat: float
    lon: float
    temperatureC: float
    conditionsText: str
    windSpeedMps: Optional[float]
    humidityPercent: Optional[float]
    confidence: str
    samples: List[ProviderWeatherSample]
    timestamp: float

    @property
    def providers_used(self) -> List[str]:
        return [s.providerName for s in self.samples]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "location": self.location,
            "lat": self.lat,
            "lon": self.lon,
            "temperatureC": self.temperatureC,
            "conditionsText": self.conditionsText,
            "windSpeedMps": self.windSpeedMps,
            "humidityPercent": self.humidityPercent,
            "confidence": self.confidence,
            "samples": [s.to_dict() for s in self.samples],
            "providersUsed": self.providers_used,
            "timestamp": self.timestamp,
        }


def get_weather_truthful(location: str, require_weatherapi: bool = False):
    """
    Returns (WeatherPacket, None) on success or (None, WeatherError) on
    failure — never a partial/guessed packet, and never raises. Every
    outcome (success or failure) is recorded via _record_success()/
    _record_error() before returning, so get_last_call_history() always
    reflects exactly what actually happened here.

    `require_weatherapi`: see module docstring — only a caller that
    specifically needs WeatherAPI by name should pass True. This gates
    the request entirely (rejected before geocoding/fusion even run) —
    it does NOT mean "only use WeatherAPI"; the fusion blend still
    combines every available provider, WeatherAPI's presence is just
    additionally required.
    """
    raw_location = location
    location = (location or "").strip()

    if not location:
        error = WeatherError(MISSING_LOCATION, "A location is required.")
        _record_error(error, raw_location or "")
        return None, error

    if require_weatherapi:
        gate_error = weather_provider.require_weather_configured()
        if gate_error is not None:
            unified_log("weather_router", "WARNING", "Weather request rejected — WeatherAPI not configured", {
                "location": location,
            })
            error = WeatherError(gate_error.code, gate_error.message)
            _record_error(error, location)
            return None, error

    try:
        geocoded = open_meteo_provider.geocode(location)
    except Exception as e:
        logger.exception(f"weather_router: geocode() raised for location={location!r}: {e}")
        error = WeatherError(GEOCODING_FAILED, f"Could not resolve location: {e}")
        _record_error(error, location)
        return None, error

    if not geocoded or geocoded.get("lat") is None or geocoded.get("lon") is None:
        unified_log("weather_router", "ERROR", "Geocoding failed", {"location": location})
        error = WeatherError(GEOCODING_FAILED, f"Could not resolve '{location}' to a real location.")
        _record_error(error, location)
        return None, error

    lat, lon = geocoded["lat"], geocoded["lon"]
    resolved_name = ", ".join(
        part for part in [geocoded.get("name"), geocoded.get("region"), geocoded.get("country")] if part
    ) or location
    # How sure open_meteo_provider.geocode() was that THIS is the right
    # place (see its own docstring / _disambiguate_candidates()) — folded
    # into the packet's overall confidence below via min-rank, never
    # reported on its own, so an ambiguous location can't produce a
    # weather answer that reads as more certain than it actually is.
    geocode_confidence = geocoded.get("geocodeConfidence") or "medium"

    try:
        fusion_result = weather_fusion.get_fused_weather(lat, lon, target_elevation=geocoded.get("elevation"))
    except Exception as e:
        logger.exception(f"weather_router: get_fused_weather() raised for ({lat}, {lon}): {e}")
        error = WeatherError(WEATHER_FUSION_FAILED, f"Weather fusion failed: {e}")
        _record_error(error, resolved_name)
        return None, error

    if isinstance(fusion_result, FusionError):
        unified_log("weather_router", "ERROR", "Weather fusion failed", {
            "location": resolved_name, "lat": lat, "lon": lon, "message": fusion_result.message,
        })
        error = WeatherError(fusion_result.code, fusion_result.message)
        _record_error(error, resolved_name)
        return None, error

    # Overall confidence is never better than the weaker of "how sure are
    # we this is the right PLACE" and "how sure are we of the weather DATA
    # for that place" — a perfectly-agreeing 4-provider fusion result is
    # still only as trustworthy as the geocode it was fused for.
    combined_rank = min(
        open_meteo_provider.CONFIDENCE_RANK.get(geocode_confidence, 2),
        open_meteo_provider.CONFIDENCE_RANK.get(fusion_result.confidence, 2),
    )
    combined_confidence = next(
        level for level, rank in open_meteo_provider.CONFIDENCE_RANK.items() if rank == combined_rank
    )

    packet = WeatherPacket(
        location=resolved_name,
        lat=lat, lon=lon,
        temperatureC=fusion_result.fusedTemperatureC,
        conditionsText=fusion_result.fusedConditionsText,
        windSpeedMps=fusion_result.fusedWindSpeedMps,
        humidityPercent=fusion_result.fusedHumidityPercent,
        confidence=combined_confidence,
        samples=fusion_result.samples,
        timestamp=time.time(),
    )
    unified_log("weather_router", "INFO", "Weather fetch succeeded", {
        "location": packet.location, "confidence": packet.confidence,
        "geocode_confidence": geocode_confidence, "fusion_confidence": fusion_result.confidence,
        "providers_used": packet.providers_used,
    })
    _record_success(packet)
    return packet, None
