# backend/core/open_meteo_provider.py

"""
Batch 3.5 — Open-Meteo (api.open-meteo.com), global, no API key
required. This is the fusion engine's BASELINE MODEL provider (see
backend.core.weather_fusion's module docstring) — always available,
everywhere, which is exactly why station data from the other three
providers is blended TOWARD it rather than the reverse.

Also the DEFAULT (keyless) geocoder for the whole fusion pipeline —
backend.core.weather_router calls geocode() to turn a free-text
location into coordinates before calling weather_fusion.
get_fused_weather(lat, lon) — via Open-Meteo's separate, free
geocoding-api.open-meteo.com service, so turning a location into
coordinates never requires a paid API key any more than the weather
lookup itself does.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

from backend.core.weather_types import ProviderWeatherSample, ProviderError

from logger import get_logger

logger = get_logger(__name__)

PROVIDER_NAME = "open_meteo"
FORECAST_BASE = "https://api.open-meteo.com/v1/forecast"
GEOCODE_BASE = "https://geocoding-api.open-meteo.com/v1/search"
REQUEST_TIMEOUT_SECONDS = 10

OPEN_METEO_REQUEST_FAILED = "OPEN_METEO_REQUEST_FAILED"
OPEN_METEO_NO_DATA = "OPEN_METEO_NO_DATA"

# WMO weather-interpretation codes -> short human condition text. The
# only one of the four providers that reports a numeric code with no
# text of its own (NOAA/OWM/WeatherAPI all return real condition
# strings) — without this table, a fused result with only Open-Meteo
# available could never have a truthful, non-null conditionsText.
WMO_CONDITIONS = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow fall", 73: "Moderate snow fall", 75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}


def _http_get_json(url: str) -> dict:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ============================================================
# NL normalization — US state + ZIP hint extraction
#
# General-purpose signal extraction from a free-text location string,
# used ONLY to disambiguate/score whatever candidates the geocoder
# actually returns (see _disambiguate_candidates() below) — never to
# rewrite or validate the query itself. Finding no hints is not an
# error; it just means disambiguation falls back to population-based
# ranking, same as it always did.
# ============================================================
_US_STATE_ABBREVIATIONS = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
}
# Longest-name-first so "New York" is tried before any shorter accidental
# substring could shadow it.
_US_STATE_NAMES_LONGEST_FIRST = sorted(set(_US_STATE_ABBREVIATIONS.values()), key=len, reverse=True)


@dataclass(frozen=True)
class LocationHints:
    # Full state name (e.g. "Virginia"), matched against a candidate's
    # own "admin1" field — never a raw abbreviation, since Open-Meteo
    # itself never abbreviates.
    state_name: Optional[str]
    zip_code: Optional[str]


def _extract_location_hints(location: str) -> LocationHints:
    zip_match = re.search(r"\b(\d{5})(?:-\d{4})?\b", location)
    zip_code = zip_match.group(1) if zip_match else None

    state_name = None
    for name in _US_STATE_NAMES_LONGEST_FIRST:
        if re.search(rf"\b{re.escape(name)}\b", location, re.IGNORECASE):
            state_name = name
            break
    if state_name is None:
        # A bare two-letter code anywhere in the string only counts in
        # its canonical UPPERCASE form ("VA", not "va") — matching
        # case-insensitively at any position would make ordinary
        # lowercase words like "in" or "or" false-positive as states.
        for abbr, name in _US_STATE_ABBREVIATIONS.items():
            if re.search(rf"\b{abbr}\b", location):
                state_name = name
                break
    if state_name is None:
        # Narrower case-insensitive fallback: an abbreviation is only
        # trusted lowercase when it's the LAST token (optionally before
        # a trailing ZIP) — "orange county va" and "richmond va 23219"
        # both end this way, whereas an ordinary sentence with "in"/"or"
        # as its last word is vanishingly unlikely on an
        # already-extracted location phrase.
        m = re.search(r"\b([A-Za-z]{2})\s*(?:\d{5}(?:-\d{4})?)?\s*$", location.strip())
        if m and m.group(1).upper() in _US_STATE_ABBREVIATIONS:
            state_name = _US_STATE_ABBREVIATIONS[m.group(1).upper()]

    return LocationHints(state_name=state_name, zip_code=zip_code)


def _geocode_candidates(name: str, count: int = 8) -> List[Dict[str, Any]]:
    q = urllib.parse.quote(name)
    data = _http_get_json(f"{GEOCODE_BASE}?name={q}&count={count}")
    return data.get("results") or []


# Ordered weakest-to-strongest so callers combining this with another
# confidence value (e.g. backend.core.weather_router folding this into
# the fused weather confidence) can rank/min() them consistently.
CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}


def _disambiguate_candidates(candidates: List[Dict[str, Any]], hints: LocationHints):
    """
    Picks ONE best match out of however many same-named places the
    geocoder returned, using real signals from the query (ZIP, state)
    instead of blindly trusting candidates[0] — the literal bug that
    let a bare "22960" resolve to Plédran, France (which also happens
    to carry that postcode) ahead of Orange, VA. Never invents a
    result — only ever chooses among what the geocoder actually
    returned, or reports (None, "low") if it returned nothing.

    Confidence:
      high   — a ZIP hint matched a candidate that ALSO matches a state
               hint (or ZIP matched exactly one candidate with no state
               hint to further confirm it), OR exactly one candidate
               matched a state hint alone, OR there was only one
               candidate and no hint to contradict it.
      medium — multiple candidates matched a state hint (population
               breaks the tie), or multiple candidates share the same
               ZIP with no state hint to break the tie (a real, if
               rare, cross-country postal-code collision — see this
               module's test suite), or there was no hint at all and
               population had to pick among several plausible matches.
      low    — a state hint was present but agreed with NONE of the
               candidates the geocoder returned (falls back to the
               most populous candidate anyway — never refuses a real
               answer just because a hint didn't line up).
    """
    if not candidates:
        return None, "low"

    if hints.zip_code:
        zip_matches = [c for c in candidates if hints.zip_code in (c.get("postcodes") or [])]
        if zip_matches:
            if hints.state_name:
                state_and_zip = [
                    c for c in zip_matches
                    if (c.get("admin1") or "").strip().lower() == hints.state_name.lower()
                ]
                if state_and_zip:
                    return state_and_zip[0], "high"
            if len(zip_matches) == 1:
                return zip_matches[0], "high"
            # Multiple candidates share this exact ZIP and no state hint
            # could tell them apart — genuinely ambiguous, not just a
            # formality, so this is deliberately "medium", not "high".
            return max(zip_matches, key=lambda c: c.get("population") or 0), "medium"

    if hints.state_name:
        state_matches = [
            c for c in candidates
            if (c.get("admin1") or "").strip().lower() == hints.state_name.lower()
            and (c.get("country_code") or "").upper() == "US"
        ]
        if len(state_matches) == 1:
            return state_matches[0], "high"
        if len(state_matches) > 1:
            return max(state_matches, key=lambda c: c.get("population") or 0), "medium"
        return max(candidates, key=lambda c: c.get("population") or 0), "low"

    if len(candidates) == 1:
        return candidates[0], "high"

    return max(candidates, key=lambda c: c.get("population") or 0), "medium"


def _simplified_geocode_variants(location: str, hints: LocationHints):
    """
    Open-Meteo's geocoder matches place names, not "City, County, State
    ZIP" style queries a person would naturally type — "Orange County,
    VA 22960" returns nothing even though "Orange, VA" (the same place)
    resolves cleanly. Yields fallback query strings, tried only if the
    literal `location` finds nothing, in order from least to most
    aggressively simplified. Never changes what a query that already
    resolves resolves to.
    """
    no_zip = re.sub(r"\b\d{5}(-\d{4})?\b", "", location)
    no_county = re.sub(r"\bcounty\b", "", no_zip, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*,\s*,", ",", no_county)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,")

    # Open-Meteo's geocoder needs an explicit comma between locality and
    # state — a comma-less "Orange VA" is parsed as a NAME PREFIX search
    # (it matches "Orange Vale"/"Orange Valley..." in California,
    # Jamaica, Texas — never Orange, Virginia), while comma-separated
    # "Orange, VA" resolves correctly. If cleanup left a state token
    # dangling at the end with no comma before it (exactly what
    # stripping "County" out of "Orange County VA" produces), insert one.
    if hints.state_name:
        abbr = next((a for a, n in _US_STATE_ABBREVIATIONS.items() if n == hints.state_name), None)
        for token in filter(None, [abbr, hints.state_name]):
            m = re.search(rf"(?<!,)\s+({re.escape(token)})\s*$", cleaned, flags=re.IGNORECASE)
            if m:
                cleaned = cleaned[: m.start()] + ", " + m.group(1) + cleaned[m.end():]
                break

    if cleaned and cleaned.lower() != location.strip().lower():
        yield cleaned

    # Last-resort fallback: "<first segment>, <state>" — collapses any
    # multi-part "City, County, State ZIP" shape (which no free geocoder
    # parses) down to the one shape every geocoder understands. Gated on
    # there being a genuine middle segment to collapse away (2+ commas,
    # e.g. "Springfield, Sangamon County, Illinois") — for an already
    # plain "City, State" query (1 comma) the lighter cleanup above is
    # either already sufficient or nothing needs stripping at all, so
    # this would just be a redundant, differently-spelled repeat of it.
    if hints.state_name and location.count(",") >= 2:
        first_segment = re.split(r",", location)[0]
        first_segment = re.sub(r"\bcounty\b", "", first_segment, flags=re.IGNORECASE).strip()
        if first_segment:
            candidate = f"{first_segment}, {hints.state_name}"
            if candidate.lower() != location.strip().lower() and candidate.lower() != cleaned.lower():
                yield candidate


def geocode(location: str) -> Optional[Dict[str, Any]]:
    """
    {"lat", "lon", "name", "region", "country", "elevation",
    "geocodeConfidence"} for the best match, or None if the location
    couldn't be resolved at all. Never raises — a geocoding failure is
    the CALLER's problem to report (backend.core.weather_router), not
    this function's; it just reports "nothing found" the same way for
    "bad query" and "network down" alike.

    geocodeConfidence ("high"/"medium"/"low") reflects how sure this
    match is, not the weather DATA itself — see _disambiguate_candidates().
    weather_router.py folds it into the fused WeatherPacket.confidence so
    an ambiguous location can never produce an artificially-confident
    weather answer.
    """
    hints = _extract_location_hints(location)

    try:
        candidates = _geocode_candidates(location)
        if not candidates:
            for variant in _simplified_geocode_variants(location, hints):
                candidates = _geocode_candidates(variant)
                if candidates:
                    break
    except Exception as e:
        logger.warning(f"open_meteo_provider.geocode() failed for {location!r}: {e}")
        return None

    best, confidence = _disambiguate_candidates(candidates, hints)
    if best is None:
        return None

    lat, lon = best.get("latitude"), best.get("longitude")
    if lat is None or lon is None:
        return None

    return {
        "lat": lat, "lon": lon,
        "name": best.get("name"), "region": best.get("admin1"), "country": best.get("country"),
        "elevation": best.get("elevation"),
        "geocodeConfidence": confidence,
    }


def _extract_current_humidity(data: dict) -> Optional[float]:
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    humidities = hourly.get("relativehumidity_2m") or []
    current_time = (data.get("current_weather") or {}).get("time")
    if current_time and current_time in times:
        idx = times.index(current_time)
        if idx < len(humidities):
            return humidities[idx]
    return humidities[0] if humidities else None


def get_forecast(lat: float, lon: float) -> Union[ProviderWeatherSample, ProviderError]:
    """Never fabricates data and never raises — every failure path returns a ProviderError."""
    params = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "current_weather": "true",
        "hourly": "relativehumidity_2m", "timezone": "auto",
    })
    try:
        data = _http_get_json(f"{FORECAST_BASE}?{params}")
    except Exception as e:
        return ProviderError(PROVIDER_NAME, OPEN_METEO_REQUEST_FAILED, f"Open-Meteo request failed: {e}")

    current = data.get("current_weather") or {}
    temperature_c = current.get("temperature")
    if temperature_c is None:
        return ProviderError(PROVIDER_NAME, OPEN_METEO_NO_DATA, "Open-Meteo returned no current_weather.")

    weathercode = current.get("weathercode")
    conditions = WMO_CONDITIONS.get(weathercode, f"Weather code {weathercode}") if weathercode is not None else None

    wind_kph = current.get("windspeed")  # Open-Meteo's default windspeed_unit is km/h
    wind_mps = (wind_kph / 3.6) if wind_kph is not None else None

    return ProviderWeatherSample(
        providerName=PROVIDER_NAME,
        temperatureC=float(temperature_c),
        conditionsText=conditions,
        windSpeedMps=wind_mps,
        humidityPercent=_extract_current_humidity(data),
        timestamp=time.time(),
        # A model grid point evaluated EXACTLY at (lat, lon) — "distance
        # from the requested point" is definitionally zero, not unknown;
        # this is what makes Open-Meteo the fusion engine's full-weight
        # baseline regardless of how far any real station happens to be.
        stationDistanceKm=0.0,
        elevationMeters=data.get("elevation"),
    )
