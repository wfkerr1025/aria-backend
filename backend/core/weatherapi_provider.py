# backend/core/weatherapi_provider.py

"""
Batch 3.5 — WeatherAPI (api.weatherapi.com), global, OPTIONAL (requires
WEATHERAPI_KEY — the exact same key backend.core.weather_provider
already tracks for Batch 3's single-provider path; this module is the
fusion engine's lat/lon-native entrypoint for that same provider/key,
not a second, separate credential).
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

PROVIDER_NAME = "weatherapi"
CURRENT_BASE = "https://api.weatherapi.com/v1/current.json"
REQUEST_TIMEOUT_SECONDS = 10

WEATHERAPI_NOT_CONFIGURED = "WEATHERAPI_NOT_CONFIGURED"
WEATHERAPI_REQUEST_FAILED = "WEATHERAPI_REQUEST_FAILED"
WEATHERAPI_NO_DATA = "WEATHERAPI_NO_DATA"


def _api_key() -> Optional[str]:
    # Env-var-only, matching every other provider wrapper in this
    # codebase (tools/get_weather.py's _weatherapi_key(), the 13 LLM
    # provider wrappers) — the module-key store is synced into
    # os.environ ONCE at process startup (module_manager.
    # sync_module_keys_to_env(), see ws_server.py/server.py), which is
    # the single, established integration point; wrappers themselves
    # never read the store directly.
    return os.getenv("WEATHERAPI_KEY")


def is_configured() -> bool:
    """
    Live truth for "is WeatherAPI available", NOT just "is the env var
    set right now" — same "env var OR stored key" pattern
    backend.core.weather_provider.is_weather_configured() already uses,
    so backend.core.weather_fusion.providers_availability() (diagnostics)
    can never disagree with backend.core.weather_provider's own status
    just because sync_module_keys_to_env() happens to not have run yet
    in whatever process is asking.
    """
    if _api_key():
        return True
    from backend.core import module_manager
    return bool(module_manager.get_module_key("weather"))


def get_current(lat: float, lon: float) -> Union[ProviderWeatherSample, ProviderError]:
    """Never fabricates data and never raises — every failure path returns a ProviderError."""
    api_key = _api_key()
    if not api_key:
        return ProviderError(PROVIDER_NAME, WEATHERAPI_NOT_CONFIGURED, "WeatherAPI key is not configured.")

    q = urllib.parse.quote(f"{lat},{lon}")
    try:
        req = urllib.request.Request(f"{CURRENT_BASE}?key={api_key}&q={q}")
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return ProviderError(PROVIDER_NAME, WEATHERAPI_REQUEST_FAILED, f"WeatherAPI request failed: {e}")

    if "error" in data:
        return ProviderError(PROVIDER_NAME, WEATHERAPI_REQUEST_FAILED, (data["error"] or {}).get("message") or "Unknown WeatherAPI error")

    current = data.get("current") or {}
    temperature_c = current.get("temp_c")
    if temperature_c is None:
        return ProviderError(PROVIDER_NAME, WEATHERAPI_NO_DATA, "WeatherAPI returned no current temperature.")

    loc = data.get("location") or {}
    distance_km = haversine_km(lat, lon, loc.get("lat"), loc.get("lon"))

    wind_kph = current.get("wind_kph")

    return ProviderWeatherSample(
        providerName=PROVIDER_NAME,
        temperatureC=float(temperature_c),
        conditionsText=(current.get("condition") or {}).get("text"),
        windSpeedMps=(wind_kph / 3.6) if wind_kph is not None else None,
        humidityPercent=current.get("humidity"),
        timestamp=time.time(),
        stationDistanceKm=distance_km,
        elevationMeters=None,  # WeatherAPI's current.json doesn't report station elevation
    )
