from __future__ import annotations
import os
import re
import json
import math
import time
import urllib.parse
import urllib.request
from typing import Dict, Any, Optional

from logger import get_logger

logger = get_logger(__name__)


# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------
# FIXED: was a module-level constant frozen at import time
# (WEATHERAPI_KEY = os.getenv(...)), so a key entered later at runtime
# via the Settings UI (backend.core.module_manager, which sets
# os.environ["WEATHERAPI_KEY"] — see module_manager.sync_module_keys_
# to_env()) would never be picked up without a process restart, the
# same class of bug the LLM provider wrappers had. Read fresh each call.
def _weatherapi_key() -> Optional[str]:
    return os.getenv("WEATHERAPI_KEY")


OPENMETEO_BASE = "https://api.open-meteo.com/v1/forecast"
NOAA_POINTS_BASE = "https://api.weather.gov/points"


# ----------------------------------------------------------------------
# HELPER: Simple HTTP GET with JSON decode
# ----------------------------------------------------------------------
def http_get_json(url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = resp.read().decode("utf-8")
    return json.loads(data)


# ----------------------------------------------------------------------
# HELPER: Very simple "is this US?" heuristic
# ----------------------------------------------------------------------
def is_us_location(location: str) -> bool:
    # crude but effective for "Orange County, VA", "Richmond, VA", "Virginia, USA", etc.
    loc = location.lower()
    return any(
        token in loc
        for token in [
            " usa",
            " united states",
            ", va",
            ", virginia",
            ", ca",
            ", ny",
            ", tx",
            ", fl",
            ", pa",
            ", oh",
            ", mi",
            ", il",
            ", ga",
            ", nc",
            ", sc",
            ", al",
            ", ms",
            ", la",
            ", tn",
            ", ky",
            ", in",
            ", wi",
            ", mn",
            ", ia",
            ", mo",
            ", ks",
            ", ne",
            ", sd",
            ", nd",
            ", mt",
            ", wy",
            ", co",
            ", nm",
            ", az",
            ", ut",
            ", nv",
            ", or",
            ", wa",
            ", id",
            ", me",
            ", nh",
            ", vt",
            ", ma",
            ", ri",
            ", ct",
            ", md",
            ", de",
            ", dc",
        ]
    )


# ----------------------------------------------------------------------
# HELPER: Geocode via WeatherAPI (lat/lon) for global + US
# ----------------------------------------------------------------------
def geocode_with_weatherapi(location: str) -> Optional[Dict[str, Any]]:
    api_key = _weatherapi_key()
    if not api_key:
        return None

    q = urllib.parse.quote(location)
    url = f"https://api.weatherapi.com/v1/search.json?key={api_key}&q={q}"

    try:
        results = http_get_json(url)
    except Exception:
        return None

    if not isinstance(results, list) or not results:
        return None

    # take first match
    r = results[0]
    return {
        "lat": r.get("lat"),
        "lon": r.get("lon"),
        "name": r.get("name"),
        "region": r.get("region"),
        "country": r.get("country"),
    }


# ----------------------------------------------------------------------
# PROVIDER: NOAA (api.weather.gov) for US
# ----------------------------------------------------------------------
def get_weather_noaa(location: str) -> Dict[str, Any]:
    # we need lat/lon first; use WeatherAPI geocoding if available
    geo = geocode_with_weatherapi(location)
    if not geo or geo.get("lat") is None or geo.get("lon") is None:
        return {
            "status": "error",
            "provider": "noaa",
            "location": location,
            "error": "Unable to geocode location for NOAA",
        }

    lat = geo["lat"]
    lon = geo["lon"]

    # 1) points endpoint → forecast URL
    url_points = f"{NOAA_POINTS_BASE}/{lat},{lon}"
    try:
        points = http_get_json(url_points, headers={"User-Agent": "ARIA-Lite/1.0"})
    except Exception as e:
        return {
            "status": "error",
            "provider": "noaa",
            "location": location,
            "error": f"NOAA points request failed: {e}",
        }

    props = points.get("properties", {})
    forecast_url = props.get("forecast")
    if not forecast_url:
        return {
            "status": "error",
            "provider": "noaa",
            "location": location,
            "error": "NOAA forecast URL not found",
        }

    # 2) forecast endpoint
    try:
        forecast = http_get_json(forecast_url, headers={"User-Agent": "ARIA-Lite/1.0"})
    except Exception as e:
        return {
            "status": "error",
            "provider": "noaa",
            "location": location,
            "error": f"NOAA forecast request failed: {e}",
        }

    periods = forecast.get("properties", {}).get("periods", [])
    current = periods[0] if periods else {}

    return {
        "status": "ok",
        "provider": "noaa",
        "location": location,
        "resolved": {
            "name": geo.get("name"),
            "region": geo.get("region"),
            "country": geo.get("country"),
            "lat": lat,
            "lon": lon,
        },
        "data": {
            "temperature_c": current.get("temperature"),
            "temperature_unit": current.get("temperatureUnit"),
            "condition": current.get("shortForecast"),
            "wind_speed": current.get("windSpeed"),
            "wind_direction": current.get("windDirection"),
            "detailed": current.get("detailedForecast"),
        },
    }


# ----------------------------------------------------------------------
# PROVIDER: WeatherAPI.com (global)
# ----------------------------------------------------------------------
def get_weather_weatherapi(location: str) -> Dict[str, Any]:
    api_key = _weatherapi_key()
    if not api_key:
        return {
            "status": "error",
            "provider": "weatherapi",
            "location": location,
            "error": "WEATHERAPI_KEY not configured",
        }

    q = urllib.parse.quote(location)
    url = f"https://api.weatherapi.com/v1/current.json?key={api_key}&q={q}"

    try:
        data = http_get_json(url)
    except Exception as e:
        return {
            "status": "error",
            "provider": "weatherapi",
            "location": location,
            "error": f"WeatherAPI request failed: {e}",
        }

    if "error" in data:
        return {
            "status": "error",
            "provider": "weatherapi",
            "location": location,
            "error": data["error"].get("message", "Unknown WeatherAPI error"),
            "code": data["error"].get("code"),
        }

    loc = data.get("location", {})
    cur = data.get("current", {})

    return {
        "status": "ok",
        "provider": "weatherapi",
        "location": location,
        "resolved": {
            "name": loc.get("name"),
            "region": loc.get("region"),
            "country": loc.get("country"),
            "lat": loc.get("lat"),
            "lon": loc.get("lon"),
        },
        "data": {
            "temperature_c": cur.get("temp_c"),
            "temperature_f": cur.get("temp_f"),
            "condition": cur.get("condition", {}).get("text"),
            "wind_kph": cur.get("wind_kph"),
            "wind_mph": cur.get("wind_mph"),
            "humidity": cur.get("humidity"),
            "feelslike_c": cur.get("feelslike_c"),
            "feelslike_f": cur.get("feelslike_f"),
        },
    }


# ----------------------------------------------------------------------
# PROVIDER: Open-Meteo (global, no key, fallback)
# ----------------------------------------------------------------------
def get_weather_openmeteo(location: str) -> Dict[str, Any]:
    # need lat/lon; reuse WeatherAPI geocoding if key exists
    geo = geocode_with_weatherapi(location)
    if not geo or geo.get("lat") is None or geo.get("lon") is None:
        return {
            "status": "error",
            "provider": "openmeteo",
            "location": location,
            "error": "Unable to geocode location for Open-Meteo",
        }

    lat = geo["lat"]
    lon = geo["lon"]

    params = {
        "latitude": lat,
        "longitude": lon,
        "current_weather": "true",
    }
    qs = urllib.parse.urlencode(params)
    url = f"{OPENMETEO_BASE}?{qs}"

    try:
        data = http_get_json(url)
    except Exception as e:
        return {
            "status": "error",
            "provider": "openmeteo",
            "location": location,
            "error": f"Open-Meteo request failed: {e}",
        }

    cur = data.get("current_weather", {})

    return {
        "status": "ok",
        "provider": "openmeteo",
        "location": location,
        "resolved": {
            "name": geo.get("name"),
            "region": geo.get("region"),
            "country": geo.get("country"),
            "lat": lat,
            "lon": lon,
        },
        "data": {
            "temperature_c": cur.get("temperature"),
            "windspeed": cur.get("windspeed"),
            "winddirection": cur.get("winddirection"),
            "weathercode": cur.get("weathercode"),
        },
    }


# ----------------------------------------------------------------------
# PUBLIC ENTRYPOINT: ARIA Lite tool interface
# ----------------------------------------------------------------------
def get_weather(options: Dict[str, Any]) -> Dict[str, Any]:
    """
    ARIA Lite tool entrypoint.

    options: { "location": "<string>" }
    """
    start = time.monotonic()
    location = options.get("location") or options.get("q") or ""
    location = location.strip()

    logger.debug("get_weather invoked: location=%s", location)

    if not location:
        result = {
            "status": "error",
            "tool": "get_weather",
            "error": "Location is required",
        }
        return _log_weather_result(result, start)

    # 1) If US → prefer NOAA
    if is_us_location(location):
        noaa_result = get_weather_noaa(location)
        if noaa_result.get("status") == "ok":
            return _log_weather_result(noaa_result, start)
        # fall through to global providers if NOAA fails

    # 2) Try WeatherAPI (global)
    wa_result = get_weather_weatherapi(location)
    if wa_result.get("status") == "ok":
        return _log_weather_result(wa_result, start)

    # 3) Fallback: Open-Meteo (global, no key)
    om_result = get_weather_openmeteo(location)
    if om_result.get("status") == "ok":
        return _log_weather_result(om_result, start)

    # 4) If everything fails, return a unified error
    result = {
        "status": "error",
        "tool": "get_weather",
        "location": location,
        "error": "All providers failed",
        "providers": {
            "noaa": noaa_result if "noaa_result" in locals() else None,
            "weatherapi": wa_result,
            "openmeteo": om_result,
        },
    }
    return _log_weather_result(result, start)


# ----------------------------------------------------------------------
# HELPER: Log tool invocation result + duration
# ----------------------------------------------------------------------
def _log_weather_result(result: Dict[str, Any], start: float) -> Dict[str, Any]:
    elapsed_ms = (time.monotonic() - start) * 1000
    if result.get("status") == "error":
        logger.error(
            "get_weather failed: location=%s elapsed_ms=%.2f error=%s",
            result.get("location"), elapsed_ms, result.get("error"),
        )
    else:
        logger.info(
            "get_weather completed: provider=%s location=%s elapsed_ms=%.2f",
            result.get("provider"), result.get("location"), elapsed_ms,
        )
    return result
