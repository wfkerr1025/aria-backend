# backend/tests/weather_fusion_tests.py
#
# Regression tests for Batch 3.5 — multi-provider weather fusion
# (backend.core.weather_fusion), and its four provider modules:
#   - backend.core.noaa_provider        (USA only, no key)
#   - backend.core.open_meteo_provider  (global baseline model, no key)
#   - backend.core.openweather_provider (global, key-gated)
#   - backend.core.weatherapi_provider  (global, optional, key-gated)
#
# Every provider function is monkeypatched directly (no real network
# calls) — these tests exercise weather_fusion.py's OWN logic: which
# providers get called for a given coordinate, distance/elevation
# weighting, confidence scoring, and conditions-text preference —  not
# the providers' real HTTP behavior.
#
# Self-contained, plain-assert tests, matching backend/tests/weather_and_tools_truth_tests.py
# and backend/tests/backend_watchdog_tests.py — not pytest. Run directly:
#
#   python backend/tests/weather_fusion_tests.py

from __future__ import annotations

import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _Patcher:
    """Tiny monkeypatch-and-restore helper — records (obj, attr,
    original) triples and restores every one of them on exit, so a test
    that patches several provider functions can't accidentally leave
    one behind for the next test."""

    def __init__(self):
        self._saved = []

    def set(self, obj, attr, value):
        self._saved.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def restore(self):
        for obj, attr, original in reversed(self._saved):
            setattr(obj, attr, original)


def _sample(provider, temp_c, conditions=None, wind=2.0, humidity=50.0, distance_km=None, elevation_m=None):
    from backend.core.weather_types import ProviderWeatherSample
    return ProviderWeatherSample(
        providerName=provider, temperatureC=temp_c, conditionsText=conditions,
        windSpeedMps=wind, humidityPercent=humidity, timestamp=0,
        stationDistanceKm=distance_km, elevationMeters=elevation_m,
    )


def _patch_all_providers(p, *, usa=True, noaa=None, open_meteo=None, owm_configured=False, owm=None, wa_configured=False, wa=None):
    """
    Wires every one of the four provider modules to fixed, deterministic
    stand-ins for one get_fused_weather() call. `noaa`/`open_meteo`/
    `owm`/`wa` are ProviderWeatherSample or ProviderError instances (or
    None to leave that provider's function unpatched — only meaningful
    for open_meteo, which weather_fusion always calls).
    """
    from backend.core import noaa_provider, open_meteo_provider, openweather_provider, weatherapi_provider

    p.set(noaa_provider, "is_usa_coordinate", lambda lat, lon: usa)
    if noaa is not None:
        p.set(noaa_provider, "get_point_forecast", lambda lat, lon: noaa)
    if open_meteo is not None:
        p.set(open_meteo_provider, "get_forecast", lambda lat, lon: open_meteo)
    p.set(openweather_provider, "is_configured", lambda: owm_configured)
    if owm is not None:
        p.set(openweather_provider, "get_current", lambda lat, lon: owm)
    p.set(weatherapi_provider, "is_configured", lambda: wa_configured)
    if wa is not None:
        p.set(weatherapi_provider, "get_current", lambda lat, lon: wa)


tests = []


def test(fn):
    tests.append(fn)
    return fn


# ============================================================
# PART 1 — USA coordinates: NOAA + Open-Meteo + OWM available -> high confidence
# ============================================================
@test
def test_usa_all_three_providers_agree_yields_high_confidence_and_clustered_temp():
    from backend.core import weather_fusion

    p = _Patcher()
    try:
        _patch_all_providers(
            p, usa=True,
            noaa=_sample("noaa", 20.0, "Sunny"),
            open_meteo=_sample("open_meteo", 21.0, "Clear sky", distance_km=0.0),
            owm_configured=True, owm=_sample("openweathermap", 20.5, "Clear"),
        )
        result = weather_fusion.get_fused_weather(38.0, -78.0)
    finally:
        p.restore()

    assert not hasattr(result, "provider_errors") or True  # (WeatherFusionResult has no such attr; sanity no-op)
    assert result.confidence == "high", result
    assert 19.5 <= result.fusedTemperatureC <= 21.5, result.fusedTemperatureC
    assert len(result.samples) == 3
    assert {s.providerName for s in result.samples} == {"noaa", "open_meteo", "openweathermap"}


@test
def test_usa_conditions_text_prefers_noaa_over_open_meteo():
    from backend.core import weather_fusion

    p = _Patcher()
    try:
        _patch_all_providers(
            p, usa=True,
            noaa=_sample("noaa", 20.0, "Sunny"),
            open_meteo=_sample("open_meteo", 20.0, "Clear sky", distance_km=0.0),
        )
        result = weather_fusion.get_fused_weather(38.0, -78.0)
    finally:
        p.restore()

    assert result.fusedConditionsText == "Sunny"


# ============================================================
# PART 2 — international coordinates: Open-Meteo + OWM -> medium/high confidence
# ============================================================
@test
def test_international_open_meteo_and_owm_agree_yields_at_least_medium_confidence():
    from backend.core import weather_fusion

    p = _Patcher()
    try:
        _patch_all_providers(
            p, usa=False,  # NOAA out of coverage — must be skipped, not attempted
            open_meteo=_sample("open_meteo", 15.0, "Overcast", distance_km=0.0),
            owm_configured=True, owm=_sample("openweathermap", 16.5, "Cloudy"),
        )
        result = weather_fusion.get_fused_weather(51.5, -0.12)  # London
    finally:
        p.restore()

    assert result.confidence in ("high", "medium"), result
    assert {s.providerName for s in result.samples} == {"open_meteo", "openweathermap"}
    assert "noaa" not in {s.providerName for s in result.samples}


@test
def test_noaa_never_called_outside_usa_coverage():
    """NOAA must be skipped entirely (not attempted at all) for a
    non-US coordinate — is_usa_coordinate() gates it before any HTTP
    call would even be made."""
    from backend.core import weather_fusion, noaa_provider

    p = _Patcher()
    called = {"n": 0}

    def exploding_noaa(lat, lon):
        called["n"] += 1
        raise AssertionError("NOAA must never be called outside its coverage area")

    try:
        p.set(noaa_provider, "is_usa_coordinate", lambda lat, lon: False)
        p.set(noaa_provider, "get_point_forecast", exploding_noaa)
        from backend.core import open_meteo_provider
        p.set(open_meteo_provider, "get_forecast", lambda lat, lon: _sample("open_meteo", 15.0, "Overcast", distance_km=0.0))
        from backend.core import openweather_provider, weatherapi_provider
        p.set(openweather_provider, "is_configured", lambda: False)
        p.set(weatherapi_provider, "is_configured", lambda: False)

        weather_fusion.get_fused_weather(51.5, -0.12)
    finally:
        p.restore()

    assert called["n"] == 0


# ============================================================
# PART 3 — missing keys: OWM/WeatherAPI unavailable, fusion still works
# ============================================================
@test
def test_missing_keys_providers_marked_unavailable_fusion_still_succeeds():
    from backend.core import weather_fusion

    p = _Patcher()
    try:
        _patch_all_providers(
            p, usa=True,
            noaa=_sample("noaa", 12.0, "Cloudy"),
            open_meteo=_sample("open_meteo", 12.5, "Overcast", distance_km=0.0),
            owm_configured=False,  # no key
            wa_configured=False,   # no key
        )
        samples, errors = weather_fusion.collect_samples(38.0, -78.0)
    finally:
        p.restore()

    error_codes = {e.providerName: e.code for e in errors}
    assert error_codes.get("openweathermap") == "OPENWEATHERMAP_NOT_CONFIGURED"
    assert error_codes.get("weatherapi") == "WEATHERAPI_NOT_CONFIGURED"
    assert {s.providerName for s in samples} == {"noaa", "open_meteo"}, "fusion must still succeed with the remaining configured sources"


@test
def test_providers_availability_reflects_configuration_truthfully():
    from backend.core import weather_fusion, openweather_provider, weatherapi_provider

    p = _Patcher()
    try:
        p.set(openweather_provider, "is_configured", lambda: False)
        p.set(weatherapi_provider, "is_configured", lambda: True)
        avail = weather_fusion.providers_availability()
    finally:
        p.restore()

    assert avail["noaa"] is True
    assert avail["open_meteo"] is True
    assert avail["openweathermap"] is False
    assert avail["weatherapi"] is True


# ============================================================
# PART 4 — disagreement: a distant outlier pulls toward the majority, not away
# ============================================================
@test
def test_distant_outlier_station_pulled_toward_majority_not_averaged_equally():
    from backend.core import weather_fusion

    p = _Patcher()
    try:
        _patch_all_providers(
            p, usa=False,
            open_meteo=_sample("open_meteo", 20.0, "Clear sky", distance_km=0.0),
            owm_configured=True,
            owm=_sample("openweathermap", 35.0, "Hot", distance_km=300.0),  # far outlier station
        )
        result = weather_fusion.get_fused_weather(10.0, 20.0)
    finally:
        p.restore()

    naive_average = (20.0 + 35.0) / 2.0  # 27.5
    assert result.fusedTemperatureC < naive_average, (
        f"a distant outlier must be down-weighted, pulling the result toward the close/model "
        f"sample rather than a plain average — got {result.fusedTemperatureC}, naive average would be {naive_average}"
    )
    assert abs(result.fusedTemperatureC - 20.0) < abs(result.fusedTemperatureC - 35.0), (
        "the fused result must land closer to the near/model sample than to the far outlier"
    )
    assert result.confidence == "low", "a >5C disagreement must never report high/medium confidence"


@test
def test_close_stations_agreeing_are_not_treated_as_outliers():
    from backend.core import weather_fusion

    p = _Patcher()
    try:
        _patch_all_providers(
            p, usa=True,
            noaa=_sample("noaa", 20.0, "Sunny", distance_km=None),
            open_meteo=_sample("open_meteo", 20.3, "Clear sky", distance_km=0.0),
            owm_configured=True,
            owm=_sample("openweathermap", 19.8, "Clear", distance_km=8.0),  # close station
        )
        result = weather_fusion.get_fused_weather(38.0, -78.0)
    finally:
        p.restore()

    assert result.confidence == "high"


# ============================================================
# PART 5 — elevation correction
# ============================================================
@test
def test_elevation_correction_adjusts_a_high_altitude_station_toward_a_lower_target():
    from backend.core import weather_fusion

    p = _Patcher()
    try:
        _patch_all_providers(
            p, usa=True,
            # A station 1000ft (~304.8m) higher than the target should
            # read ~3.5°F (~1.94°C) colder than the target's true
            # temperature — correcting it back down should INCREASE its
            # contributed temperature relative to its raw reading.
            noaa=_sample("noaa", 15.0, "Clear", elevation_m=1304.8),
            open_meteo=_sample("open_meteo", 17.0, "Clear sky", distance_km=0.0, elevation_m=1000.0),
        )
        result = weather_fusion.get_fused_weather(38.0, -78.0, target_elevation=1000.0)
    finally:
        p.restore()

    # Elevation-corrected NOAA reading should be pulled up from 15.0
    # toward ~16.94 (15.0 + ~1.94), moving the fused blend above the
    # raw (uncorrected) average of (15+17)/2=16.0.
    raw_average = (15.0 + 17.0) / 2.0
    assert result.fusedTemperatureC > raw_average, (
        f"elevation correction should pull the high-altitude station's contribution up toward the "
        f"target's lower elevation, not leave it at the raw uncorrected average ({raw_average})"
    )


# ============================================================
# PART 6 — total failure: no fabricated data
# ============================================================
@test
def test_total_failure_returns_structured_error_never_fabricated_data():
    from backend.core import weather_fusion
    from backend.core.weather_types import ProviderError, FusionError

    p = _Patcher()
    try:
        _patch_all_providers(
            p, usa=True,
            noaa=ProviderError("noaa", "NOAA_REQUEST_FAILED", "timeout"),
            open_meteo=ProviderError("open_meteo", "OPEN_METEO_REQUEST_FAILED", "timeout"),
            owm_configured=False,
            wa_configured=False,
        )
        result = weather_fusion.get_fused_weather(38.0, -78.0)
    finally:
        p.restore()

    assert isinstance(result, FusionError), result
    assert result.code == weather_fusion.WEATHER_FUSION_FAILED
    assert len(result.provider_errors) == 4, "every one of the four providers must be accounted for, even the unconfigured ones"


@test
def test_a_single_provider_exception_does_not_take_down_the_whole_request():
    """One provider raising an unexpected exception must be caught and
    turned into a ProviderError — never propagate out of get_fused_weather()
    and never prevent the OTHER providers' real data from being used."""
    from backend.core import weather_fusion

    p = _Patcher()

    def exploding_noaa(lat, lon):
        raise RuntimeError("simulated network stack failure")

    try:
        _patch_all_providers(p, usa=True, open_meteo=_sample("open_meteo", 20.0, "Clear sky", distance_km=0.0))
        from backend.core import noaa_provider
        p.set(noaa_provider, "get_point_forecast", exploding_noaa)

        result = weather_fusion.get_fused_weather(38.0, -78.0)
    finally:
        p.restore()

    assert result.fusedTemperatureC == 20.0
    assert {s.providerName for s in result.samples} == {"open_meteo"}


# ============================================================
# PART 7 — provider modules never fabricate data on their own
# ============================================================
@test
def test_open_meteo_wmo_code_translation_never_returns_null_for_a_known_code():
    from backend.core import open_meteo_provider
    assert open_meteo_provider.WMO_CONDITIONS.get(0) == "Clear sky"
    assert open_meteo_provider.WMO_CONDITIONS.get(95) is not None


@test
def test_noaa_out_of_coverage_returns_structured_error_not_a_guess():
    from backend.core import noaa_provider
    from backend.core.weather_types import ProviderError

    result = noaa_provider.get_point_forecast(51.5, -0.12)  # London — not US
    assert isinstance(result, ProviderError)
    assert result.code == noaa_provider.NOAA_OUT_OF_COVERAGE


@test
def test_openweather_not_configured_returns_structured_error_not_a_guess():
    from backend.core import openweather_provider
    from backend.core.weather_types import ProviderError

    p = _Patcher()
    try:
        p.set(openweather_provider, "_api_key", lambda: None)
        result = openweather_provider.get_current(38.0, -78.0)
    finally:
        p.restore()

    assert isinstance(result, ProviderError)
    assert result.code == openweather_provider.OPENWEATHERMAP_NOT_CONFIGURED


@test
def test_weatherapi_not_configured_returns_structured_error_not_a_guess():
    from backend.core import weatherapi_provider
    from backend.core.weather_types import ProviderError

    p = _Patcher()
    try:
        p.set(weatherapi_provider, "_api_key", lambda: None)
        result = weatherapi_provider.get_current(38.0, -78.0)
    finally:
        p.restore()

    assert isinstance(result, ProviderError)
    assert result.code == weatherapi_provider.WEATHERAPI_NOT_CONFIGURED


# ============================================================
# RUNNER
# ============================================================
def main() -> int:
    failures = []
    for t in tests:
        name = t.__name__
        try:
            t()
            print(f"PASS  {name}")
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            failures.append(name)
        except Exception as e:
            print(f"ERROR {name}: {e}")
            traceback.print_exc()
            failures.append(name)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1

    print("All tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
