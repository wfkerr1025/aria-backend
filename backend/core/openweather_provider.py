# backend/core/openweather_provider.py

"""
Batch 3.5 — OpenWeatherMap (api.openweathermap.org), global, REQUIRES an
API key (OPENWEATHERMAP_API_KEY — see backend.core.module_manager's
"openweathermap" module entry, added alongside the existing "weather"/
WeatherAPI entry). Marked unavailable (never attempted at all — see
is_configured(), which backend.core.weather_fusion checks before ever
calling get_current()) when no key is configured, matching backend.
core.weather_provider's "no silent fallback" rule from Batch 3.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Optional, Union

from backend.core.weather_types import ProviderWeatherSample, ProviderError, haversine_km

from logger import get_logger

logger = get_logger(__name__)

PROVIDER_NAME = "openweathermap"
CURRENT_BASE = "https://api.openweathermap.org/data/2.5/weather"
REQUEST_TIMEOUT_SECONDS = 10

OPENWEATHERMAP_NOT_CONFIGURED = "OPENWEATHERMAP_NOT_CONFIGURED"
OPENWEATHERMAP_REQUEST_FAILED = "OPENWEATHERMAP_REQUEST_FAILED"
OPENWEATHERMAP_NO_DATA = "OPENWEATHERMAP_NO_DATA"


def _api_key() -> Optional[str]:
    # Read fresh every call, not cached at import time — same reasoning
    # as tools/get_weather.py's _weatherapi_key(): a key entered later
    # via the settings UI (backend.core.module_manager) must be picked
    # up without a process restart. Env-var-only, matching every other
    # provider wrapper in this codebase — the module-key store is
    # synced into os.environ ONCE at process startup (module_manager.
    # sync_module_keys_to_env()), the single established integration
    # point.
    return os.getenv("OPENWEATHERMAP_API_KEY")


def is_configured() -> bool:
    """
    Live truth for "is OpenWeatherMap available", NOT just "is the env
    var set right now" — same "env var OR stored key" pattern
    backend.core.weather_provider.is_weather_configured() already uses,
    so diagnostics can never disagree with the real stored key just
    because sync_module_keys_to_env() happens to not have run yet.
    """
    if _api_key():
        return True
    from backend.core import module_manager
    return bool(module_manager.get_module_key("openweathermap"))


def get_current(lat: float, lon: float) -> Union[ProviderWeatherSample, ProviderError]:
    """Never fabricates data and never raises — every failure path returns a ProviderError."""
    api_key = _api_key()
    if not api_key:
        return ProviderError(PROVIDER_NAME, OPENWEATHERMAP_NOT_CONFIGURED, "OpenWeatherMap API key is not configured.")

    params = urllib.parse.urlencode({"lat": lat, "lon": lon, "appid": api_key, "units": "metric"})
    try:
        req = urllib.request.Request(f"{CURRENT_BASE}?{params}")
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return ProviderError(PROVIDER_NAME, OPENWEATHERMAP_REQUEST_FAILED, f"OpenWeatherMap request failed: {e}")

    if str(data.get("cod")) != "200":
        return ProviderError(PROVIDER_NAME, OPENWEATHERMAP_REQUEST_FAILED, data.get("message") or "Unknown OpenWeatherMap error")

    main = data.get("main") or {}
    temperature_c = main.get("temp")
    if temperature_c is None:
        return ProviderError(PROVIDER_NAME, OPENWEATHERMAP_NO_DATA, "OpenWeatherMap returned no temperature.")

    weather_list = data.get("weather") or []
    conditions = weather_list[0].get("description") if weather_list else None
    if conditions:
        conditions = conditions[:1].upper() + conditions[1:]

    coord = data.get("coord") or {}
    distance_km = haversine_km(lat, lon, coord.get("lat"), coord.get("lon"))

    return ProviderWeatherSample(
        providerName=PROVIDER_NAME,
        temperatureC=float(temperature_c),
        conditionsText=conditions,
        windSpeedMps=(data.get("wind") or {}).get("speed"),  # units=metric already gives m/s
        humidityPercent=main.get("humidity"),
        timestamp=time.time(),
        stationDistanceKm=distance_km,
        elevationMeters=None,  # OWM's current-weather endpoint doesn't report station elevation
    )
