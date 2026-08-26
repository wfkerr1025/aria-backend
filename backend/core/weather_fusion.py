# backend/core/weather_fusion.py

"""
Batch 3.5 — multi-provider weather fusion engine.

Combines NOAA (USA only, no key), Open-Meteo (global baseline model, no
key), OpenWeatherMap (global, key-gated), and WeatherAPI (global,
optional, key-gated) into ONE blended, confidence-scored result —
replacing Batch 3's weather_router.py behavior of picking exactly ONE
provider per request (NOAA, then WeatherAPI, then Open-Meteo,
first-success-wins). Does NOT remove or modify those single-provider
modules (tools/get_weather.py, backend.core.noaa_provider et al. are
each still real, independent, directly-callable modules) — this is a
new layer on top that calls all of them and blends the results.

Fusion rules (see get_fused_weather()):
  - Distance weighting: a station within DISTANCE_FULL_WEIGHT_KM gets
    full weight; weight decays linearly to MIN_WEIGHT by
    DISTANCE_ZERO_WEIGHT_KM. Open-Meteo's model sample always has
    stationDistanceKm=0 (see open_meteo_provider.py), so it's never
    down-weighted — this is what makes it the effective baseline that
    far-away station data gets pulled toward rather than the reverse.
  - Elevation correction: a station's raw temperature is adjusted
    toward the target elevation using the standard ~3.5°F/1000ft lapse
    rate (converted to °C internally) BEFORE weighting/averaging, so a
    high-altitude station near a low-elevation target (or vice versa)
    doesn't skew the blend just because of where its sensor sits.
  - Confidence: high when ≥2 providers' (elevation-corrected)
    temperatures agree within 2°C, medium within 5°C, low otherwise or
    with only one provider available.
  - Conditions text: prefers a real provider's own condition string
    (NOAA/OWM/WeatherAPI, in that order) over Open-Meteo's WMO-code
    translation, but never leaves it null — Open-Meteo's mapped text is
    always the fallback since it's the one provider guaranteed present.

Every provider call is wrapped so a single provider's exception, bad
response, or timeout can never take down the whole fusion request — see
_safe_call(). A provider that's unavailable (no key) or fails is simply
excluded from the blend and recorded as a ProviderError; get_fused_weather()
only returns FusionError (WEATHER_FUSION_FAILED) when EVERY provider
failed or was unavailable — never fabricated/guessed data standing in
for a real reading.
"""

from __future__ import annotations

from typing import List, Optional, Tuple, Union

from backend.core.weather_types import (
    ProviderWeatherSample, ProviderError, WeatherFusionResult, FusionError,
)
from backend.core import noaa_provider
from backend.core import open_meteo_provider
from backend.core import openweather_provider
from backend.core import weatherapi_provider

from backend.logger import log as unified_log
from logger import get_logger

logger = get_logger(__name__)

WEATHER_FUSION_FAILED = "WEATHER_FUSION_FAILED"

# 3.5°F per 1000ft, converted to °C per 1000ft.
ELEVATION_CORRECTION_C_PER_1000FT = 3.5 * 5.0 / 9.0

AGREEMENT_HIGH_C = 2.0
AGREEMENT_MEDIUM_C = 5.0

# Distance weighting curve — see module docstring.
DISTANCE_FULL_WEIGHT_KM = 25.0
DISTANCE_ZERO_WEIGHT_KM = 200.0
MIN_WEIGHT = 0.05

_ALL_PROVIDER_NAMES = ("noaa", "open_meteo", "openweathermap", "weatherapi")


def _safe_call(provider_name: str, fn, lat: float, lon: float) -> Union[ProviderWeatherSample, ProviderError]:
    """Tolerant of ANY exception a provider module might raise (network
    timeout, malformed response, DNS failure, ...) — one provider
    misbehaving must never take down the whole fusion request."""
    try:
        return fn(lat, lon)
    except Exception as e:
        logger.exception(f"weather_fusion: {provider_name} provider raised: {e}")
        return ProviderError(provider_name, "PROVIDER_EXCEPTION", str(e))


def collect_samples(lat: float, lon: float) -> Tuple[List[ProviderWeatherSample], List[ProviderError]]:
    """
    Calls every provider that's applicable/configured for (lat, lon),
    tolerating individual failures. Returns (samples, errors) — errors
    includes both real request failures AND "not configured"/"out of
    coverage" as ProviderError entries, so a caller (diagnostics) can
    always account for all four providers by name, whether or not they
    contributed a sample.
    """
    samples: List[ProviderWeatherSample] = []
    errors: List[ProviderError] = []

    if noaa_provider.is_usa_coordinate(lat, lon):
        result = _safe_call("noaa", noaa_provider.get_point_forecast, lat, lon)
        (samples if isinstance(result, ProviderWeatherSample) else errors).append(result)
    else:
        errors.append(ProviderError("noaa", noaa_provider.NOAA_OUT_OF_COVERAGE, "NOAA only covers US coordinates."))

    result = _safe_call("open_meteo", open_meteo_provider.get_forecast, lat, lon)
    (samples if isinstance(result, ProviderWeatherSample) else errors).append(result)

    if openweather_provider.is_configured():
        result = _safe_call("openweathermap", openweather_provider.get_current, lat, lon)
        (samples if isinstance(result, ProviderWeatherSample) else errors).append(result)
    else:
        errors.append(ProviderError("openweathermap", openweather_provider.OPENWEATHERMAP_NOT_CONFIGURED, "OpenWeatherMap API key is not configured."))

    if weatherapi_provider.is_configured():
        result = _safe_call("weatherapi", weatherapi_provider.get_current, lat, lon)
        (samples if isinstance(result, ProviderWeatherSample) else errors).append(result)
    else:
        errors.append(ProviderError("weatherapi", weatherapi_provider.WEATHERAPI_NOT_CONFIGURED, "WeatherAPI key is not configured."))

    return samples, errors


def _elevation_corrected_temperature(sample: ProviderWeatherSample, target_elevation: Optional[float]) -> float:
    if sample.elevationMeters is None or target_elevation is None:
        return sample.temperatureC
    delta_ft = (target_elevation - sample.elevationMeters) * 3.28084
    correction_c = (delta_ft / 1000.0) * ELEVATION_CORRECTION_C_PER_1000FT
    # Standard lapse-rate direction: the target is COLDER than the
    # station by this amount if it's HIGHER up — so subtract when
    # delta_ft is positive (target above station), add when negative.
    return sample.temperatureC - correction_c


def _distance_weight(sample: ProviderWeatherSample) -> float:
    distance = sample.stationDistanceKm
    if distance is None:
        # Unknown distance (NOAA's grid forecast — see noaa_provider.py)
        # is treated as full weight: it's a real, authoritative forecast
        # for the exact point, just not a literal "station", not a fact
        # to penalize it for not reporting.
        return 1.0
    if distance <= DISTANCE_FULL_WEIGHT_KM:
        return 1.0
    if distance >= DISTANCE_ZERO_WEIGHT_KM:
        return MIN_WEIGHT
    span = DISTANCE_ZERO_WEIGHT_KM - DISTANCE_FULL_WEIGHT_KM
    frac = (distance - DISTANCE_FULL_WEIGHT_KM) / span
    return max(MIN_WEIGHT, 1.0 - frac * (1.0 - MIN_WEIGHT))


def _confidence(corrected_temperatures: List[float]) -> str:
    if len(corrected_temperatures) <= 1:
        return "low"
    spread = max(corrected_temperatures) - min(corrected_temperatures)
    if spread <= AGREEMENT_HIGH_C:
        return "high"
    if spread <= AGREEMENT_MEDIUM_C:
        return "medium"
    return "low"


# Preferred order for conditionsText — a real provider's own condition
# string beats Open-Meteo's WMO-code translation, but Open-Meteo (always
# present when it succeeds) is the guaranteed non-null fallback.
_CONDITION_PREFERENCE = ("noaa", "openweathermap", "weatherapi", "open_meteo")


def _fuse_conditions(samples: List[ProviderWeatherSample]) -> str:
    by_provider = {s.providerName: s for s in samples}
    for name in _CONDITION_PREFERENCE:
        s = by_provider.get(name)
        if s and s.conditionsText:
            return s.conditionsText
    for s in samples:
        if s.conditionsText:
            return s.conditionsText
    return "Unknown"


def get_fused_weather(lat: float, lon: float, target_elevation: Optional[float] = None) -> Union[WeatherFusionResult, FusionError]:
    """
    Returns a WeatherFusionResult on success (at least one provider
    contributed a real sample) or a FusionError (WEATHER_FUSION_FAILED)
    if every provider failed or was unavailable — never fabricates a
    result to paper over that.

    `target_elevation` (meters) is the elevation to correct station
    temperatures TOWARD — backend.core.weather_router passes the
    geocoded location's own elevation (from open_meteo_provider.geocode())
    when known. If not provided, falls back to the first sample that
    reports its own elevation.
    """
    samples, errors = collect_samples(lat, lon)

    if not samples:
        message = "; ".join(f"{e.providerName}: {e.message}" for e in errors) or "No weather providers returned data."
        logger.error(f"weather_fusion: all providers failed for ({lat}, {lon}): {message}")
        unified_log("weather_fusion", "ERROR", "All weather providers failed", {
            "lat": lat, "lon": lon, "errors": [e.to_dict() for e in errors],
        })
        return FusionError(WEATHER_FUSION_FAILED, message, provider_errors=errors)

    if target_elevation is None:
        for s in samples:
            if s.elevationMeters is not None:
                target_elevation = s.elevationMeters
                break

    corrected = [
        (_elevation_corrected_temperature(s, target_elevation), _distance_weight(s), s)
        for s in samples
    ]
    total_weight = sum(w for _, w, _ in corrected)
    if total_weight <= 0:
        fused_temp = sum(t for t, _, _ in corrected) / len(corrected)
    else:
        fused_temp = sum(t * w for t, w, _ in corrected) / total_weight

    wind_values = [s.windSpeedMps for s in samples if s.windSpeedMps is not None]
    fused_wind = (sum(wind_values) / len(wind_values)) if wind_values else None

    humidity_values = [s.humidityPercent for s in samples if s.humidityPercent is not None]
    fused_humidity = (sum(humidity_values) / len(humidity_values)) if humidity_values else None

    confidence = _confidence([t for t, _, _ in corrected])
    conditions = _fuse_conditions(samples)

    result = WeatherFusionResult(
        fusedTemperatureC=round(fused_temp, 1),
        fusedConditionsText=conditions,
        fusedWindSpeedMps=round(fused_wind, 2) if fused_wind is not None else None,
        fusedHumidityPercent=round(fused_humidity, 1) if fused_humidity is not None else None,
        confidence=confidence,
        samples=samples,
    )
    unified_log("weather_fusion", "INFO", "Weather fusion succeeded", {
        "lat": lat, "lon": lon, "confidence": confidence,
        "providers_used": [s.providerName for s in samples],
    })
    return result


def providers_availability() -> dict:
    """
    {"noaa": True, "open_meteo": True, "openweathermap": bool, "weatherapi": bool}
    — "available" here means "configured/reachable in principle" (has a
    key if it needs one), NOT "succeeded on the last call"; NOAA's
    entry is always True since its gating is per-coordinate, not
    global — see diagnostics_weather_result's providersAvailable field.
    """
    return {
        "noaa": True,
        "open_meteo": True,
        "openweathermap": openweather_provider.is_configured(),
        "weatherapi": weatherapi_provider.is_configured(),
    }
