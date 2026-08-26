# backend/core/weather_types.py

"""
Batch 3.5 — shared data shapes for the multi-provider weather fusion
engine (backend.core.weather_fusion) and its four provider modules
(noaa_provider, open_meteo_provider, openweather_provider,
weatherapi_provider).

Split into its own module specifically so those provider modules and
weather_fusion.py can both import these types without a circular
import — weather_fusion.py imports all four provider modules, so the
types themselves cannot live inside weather_fusion.py or inside any one
provider module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ProviderWeatherSample:
    """
    One provider's normalized current-conditions reading. Every provider
    module (noaa_provider.get_point_forecast(), open_meteo_provider.
    get_forecast(), openweather_provider.get_current(),
    weatherapi_provider.get_current()) returns either this or a
    ProviderError — never a partial/guessed sample.
    """
    providerName: str
    temperatureC: float
    conditionsText: Optional[str]
    windSpeedMps: Optional[float]
    humidityPercent: Optional[float]
    timestamp: float
    stationDistanceKm: Optional[float] = None
    elevationMeters: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "providerName": self.providerName,
            "temperatureC": self.temperatureC,
            "conditionsText": self.conditionsText,
            "windSpeedMps": self.windSpeedMps,
            "humidityPercent": self.humidityPercent,
            "timestamp": self.timestamp,
            "stationDistanceKm": self.stationDistanceKm,
            "elevationMeters": self.elevationMeters,
        }


@dataclass(frozen=True)
class ProviderError:
    """A single provider's failure — never fabricated data, never a
    guessed sample standing in for one."""
    providerName: str
    code: str
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {"providerName": self.providerName, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class WeatherFusionResult:
    """backend.core.weather_fusion.get_fused_weather()'s success shape."""
    fusedTemperatureC: float
    fusedConditionsText: str
    fusedWindSpeedMps: Optional[float]
    fusedHumidityPercent: Optional[float]
    confidence: str  # "high" | "medium" | "low"
    samples: List[ProviderWeatherSample] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fusedTemperatureC": self.fusedTemperatureC,
            "fusedConditionsText": self.fusedConditionsText,
            "fusedWindSpeedMps": self.fusedWindSpeedMps,
            "fusedHumidityPercent": self.fusedHumidityPercent,
            "confidence": self.confidence,
            "samples": [s.to_dict() for s in self.samples],
        }


@dataclass(frozen=True)
class FusionError:
    """backend.core.weather_fusion.get_fused_weather()'s failure shape —
    every provider failed or was unavailable; never fabricated data."""
    code: str
    message: str
    provider_errors: List[ProviderError] = field(default_factory=list)


def haversine_km(lat1: Optional[float], lon1: Optional[float], lat2: Optional[float], lon2: Optional[float]) -> Optional[float]:
    """Great-circle distance in km, or None if any coordinate is missing."""
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return None
    r = 6371.0088  # mean Earth radius, km
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))
