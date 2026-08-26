# backend/core/weather_provider.py

"""
Batch 3 — single source of truth for WeatherAPI's configuration status,
mirroring backend.core.provider_config's architecture from Batch 1
(identity/enablement/hasApiKey, validated before use, structured error
on failure — never a silent guess) but scoped to the "weather" MODULE
(backend.core.module_manager / key_manager's module-key store), not the
LLM provider_config.json registry: a weather API key is a module key,
not an LLM provider key, and the two have always been kept in separate
stores (see module_manager.py's own docstring).

Before this module, "is WeatherAPI configured" had no single answer —
tools/get_weather.py's get_weather() just tries NOAA, then WeatherAPI,
then Open-Meteo, and only WeatherAPI's own call site
(get_weather_weatherapi()) ever checked _weatherapi_key(), deep inside
that fallback chain. Nothing surfaced WeatherAPI's status on its own
for diagnostics, and nothing could reject a request that specifically
needs WeatherAPI (as opposed to "whichever provider succeeds") before
attempting it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from backend.core import key_manager
from backend.core import module_manager

# The module name this status is about, in backend.core.module_manager's
# key store — "weather" is the only module ARIA-Lite ships today (see
# module_manager.KNOWN_MODULES).
WEATHER_MODULE_NAME = "weather"

# Human-readable name for the specific weather PROVIDER this module
# tracks (WeatherAPI — one of the three real providers
# tools/get_weather.py's get_weather() can route to; NOAA and
# Open-Meteo need no key and are outside this module's scope, same as
# how backend.core.provider_config only tracks LLM providers that
# actually need a key).
WEATHER_PROVIDER_NAME = "weatherapi"
WEATHER_PROVIDER_DISPLAY_NAME = "WeatherAPI"

WEATHER_PROVIDER_NOT_CONFIGURED = "WEATHER_PROVIDER_NOT_CONFIGURED"


@dataclass(frozen=True)
class WeatherProviderStatus:
    weather_provider_name: str
    weather_provider_display_name: str
    isWeatherConfigured: bool
    enabled: bool


@dataclass(frozen=True)
class WeatherProviderError:
    code: str
    message: str


def _weather_env_var() -> str:
    return module_manager.MODULE_ENV_VARS.get(WEATHER_MODULE_NAME, "WEATHERAPI_KEY")


def is_weather_configured() -> bool:
    """
    Live truth, same "keyring/store OR env var" pattern
    key_manager.list_configured_providers() already uses for LLM
    providers — sync_module_keys_to_env() (server.py/ws_server.py
    startup) already mirrors a stored key into the env var, so checking
    both catches a key set directly via a real environment variable too
    (e.g. a deployment that never goes through the settings UI at all).
    """
    if os.getenv(_weather_env_var()):
        return True
    return bool(key_manager.get_module_key(WEATHER_MODULE_NAME))


def get_weather_provider_status() -> WeatherProviderStatus:
    """
    The single source of truth for "is WeatherAPI usable right now" —
    backend.core.weather_router and diagnostics both call this instead
    of independently re-deriving it.
    """
    return WeatherProviderStatus(
        weather_provider_name=WEATHER_PROVIDER_NAME,
        weather_provider_display_name=WEATHER_PROVIDER_DISPLAY_NAME,
        isWeatherConfigured=is_weather_configured(),
        enabled=True,  # weather has no per-module "enabled" toggle today — always enabled if configured
    )


def require_weather_configured() -> "WeatherProviderError | None":
    """
    Returns a WeatherProviderError (WEATHER_PROVIDER_NOT_CONFIGURED) if
    WeatherAPI specifically is not configured, else None. Callers that
    need WeatherAPI by name (as opposed to "any weather provider that
    happens to work" — see backend.core.weather_router, which tries the
    full NOAA/WeatherAPI/Open-Meteo chain regardless) use this as a
    pre-flight gate.
    """
    status = get_weather_provider_status()
    if status.isWeatherConfigured:
        return None
    return WeatherProviderError(
        WEATHER_PROVIDER_NOT_CONFIGURED,
        "WeatherAPI is not configured. Add a WeatherAPI key in Settings, or rely on "
        "the free NOAA/Open-Meteo fallback for US/international locations.",
    )
