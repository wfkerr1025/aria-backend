# backend/core/noaa_provider.py

"""
Batch 3.5 — NOAA (api.weather.gov), USA-only, no API key required.

Wraps the same real, live NOAA endpoints tools/get_weather.py's
get_weather_noaa() already calls for the legacy single-provider path —
this module does NOT replace that one (see its own docstring: "do not
remove existing single-provider modules"); it's the fusion engine's
lat/lon-native entrypoint for the same data source, used by
backend.core.weather_fusion instead of the location-string-based
legacy path.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from typing import Optional, Union

from backend.core.weather_types import ProviderWeatherSample, ProviderError

from logger import get_logger

logger = get_logger(__name__)

PROVIDER_NAME = "noaa"
NOAA_POINTS_BASE = "https://api.weather.gov/points"
REQUEST_TIMEOUT_SECONDS = 10

NOAA_REQUEST_FAILED = "NOAA_REQUEST_FAILED"
NOAA_NO_FORECAST_URL = "NOAA_NO_FORECAST_URL"
NOAA_NO_DATA = "NOAA_NO_DATA"
NOAA_OUT_OF_COVERAGE = "NOAA_OUT_OF_COVERAGE"


def _http_get_json(url: str, headers: Optional[dict] = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def is_usa_coordinate(lat: float, lon: float) -> bool:
    """
    Bounding-box heuristic covering the continental US, Alaska, Hawaii,
    and Puerto Rico — good enough for "should we even try NOAA" so a
    request outside its coverage fails fast with NOAA_OUT_OF_COVERAGE
    instead of a wasted round trip that would 404 anyway (NOAA's own
    /points endpoint has no other clean way to say "not covered").
    """
    conus = 24.5 <= lat <= 49.5 and -125.0 <= lon <= -66.0
    alaska = 51.0 <= lat <= 71.5 and -179.5 <= lon <= -129.0
    hawaii = 18.5 <= lat <= 22.5 and -160.5 <= lon <= -154.5
    puerto_rico = 17.5 <= lat <= 18.6 and -67.5 <= lon <= -65.2
    return conus or alaska or hawaii or puerto_rico


def _to_celsius(value: float, unit: str) -> float:
    if (unit or "F").upper() == "F":
        return (value - 32) * 5.0 / 9.0
    return float(value)


def _parse_wind_speed_mps(text: Optional[str]) -> Optional[float]:
    """NOAA's windSpeed field is a free-text string like '10 mph' or '5 to 10 mph'."""
    if not text:
        return None
    numbers = [float(n) for n in re.findall(r"[\d.]+", text)]
    if not numbers:
        return None
    mph = sum(numbers) / len(numbers)  # average a range ("5 to 10 mph") rather than picking an endpoint
    return mph * 0.44704


def get_point_forecast(lat: float, lon: float) -> Union[ProviderWeatherSample, ProviderError]:
    """Never fabricates data and never raises — every failure path returns a ProviderError."""
    if not is_usa_coordinate(lat, lon):
        return ProviderError(PROVIDER_NAME, NOAA_OUT_OF_COVERAGE, "NOAA only covers US coordinates.")

    try:
        points = _http_get_json(f"{NOAA_POINTS_BASE}/{lat},{lon}", headers={"User-Agent": "ARIA-Lite/1.0"})
    except Exception as e:
        return ProviderError(PROVIDER_NAME, NOAA_REQUEST_FAILED, f"NOAA points request failed: {e}")

    props = points.get("properties") or {}
    forecast_url = props.get("forecast")
    if not forecast_url:
        return ProviderError(PROVIDER_NAME, NOAA_NO_FORECAST_URL, "NOAA forecast URL not found for this location.")

    try:
        forecast = _http_get_json(forecast_url, headers={"User-Agent": "ARIA-Lite/1.0"})
    except Exception as e:
        return ProviderError(PROVIDER_NAME, NOAA_REQUEST_FAILED, f"NOAA forecast request failed: {e}")

    periods = (forecast.get("properties") or {}).get("periods") or []
    if not periods:
        return ProviderError(PROVIDER_NAME, NOAA_NO_DATA, "NOAA returned no forecast periods.")

    current = periods[0]
    temp = current.get("temperature")
    if temp is None:
        return ProviderError(PROVIDER_NAME, NOAA_NO_DATA, "NOAA forecast period had no temperature.")

    elevation = props.get("elevation")
    elevation_meters = elevation.get("value") if isinstance(elevation, dict) else None

    return ProviderWeatherSample(
        providerName=PROVIDER_NAME,
        temperatureC=_to_celsius(temp, current.get("temperatureUnit", "F")),
        conditionsText=current.get("shortForecast"),
        windSpeedMps=_parse_wind_speed_mps(current.get("windSpeed")),
        humidityPercent=None,  # NOAA's basic gridpoint forecast endpoint doesn't report this — left honestly null, never guessed
        timestamp=time.time(),
        # A forecast grid-cell CENTER, not a real observing station —
        # "distance from the requested point" isn't a meaningful concept
        # here the way it is for OWM/WeatherAPI's actual station data, so
        # this is deliberately None rather than a fabricated 0.
        stationDistanceKm=None,
        elevationMeters=elevation_meters,
    )
