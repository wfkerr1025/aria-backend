# backend/tests/open_meteo_geocode_tests.py
#
# Regression tests for backend.core.open_meteo_provider.geocode()'s NL
# normalization, disambiguation, and confidence-scoring behavior.
#
# Two real problems drove this: (1) a literal lookup for "Orange County,
# VA 22960" returned nothing from Open-Meteo's geocoder, even though
# "Orange, VA" — the same place — resolves cleanly (fixed by
# _simplified_geocode_variants()'s fallback queries); (2) Open-Meteo's
# geocoder returns MULTIPLE same-named candidates for an ambiguous query
# and geocode() used to blindly trust candidates[0] — a bare ZIP "22960"
# actually resolved to Plédran, France (which also carries that
# postcode) ahead of Orange, VA, purely because of result ordering
# (fixed by _extract_location_hints() + _disambiguate_candidates()).
#
# _geocode_candidates() (the actual HTTP call) is monkeypatched directly
# — no real network calls — so these tests exercise geocode()'s own
# normalization/fallback/disambiguation logic, not Open-Meteo's live API.
#
# Self-contained, plain-assert tests, matching backend/tests/weather_fusion_tests.py
# and friends — not pytest. Run directly:
#
#   python backend/tests/open_meteo_geocode_tests.py

from __future__ import annotations

import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _Patcher:
    def __init__(self):
        self._saved = []

    def set(self, obj, attr, value):
        self._saved.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def restore(self):
        for obj, attr, original in reversed(self._saved):
            setattr(obj, attr, original)


def _candidate(name="Orange", state="Virginia", country_code="US", population=4947,
               lat=38.24541, lon=-78.11083, postcodes=None, elevation=159.0):
    return {
        "name": name, "latitude": lat, "longitude": lon, "elevation": elevation,
        "admin1": state, "country": "United States" if country_code == "US" else state,
        "country_code": country_code, "population": population,
        "postcodes": postcodes or [],
    }


_ORANGE_VA = _candidate(postcodes=["22960"])
_ORANGE_CT = _candidate(state="Connecticut", country_code="US", population=7000, lat=41.3, lon=-72.9)
_PLEDRAN_FR = _candidate(name="Plédran", state="Brittany", country_code="FR", population=5550,
                          lat=48.44561, lon=-2.7461, postcodes=["22960"])

tests = []


def test(fn):
    tests.append(fn)
    return fn


# Not a test. It is the registration decorator these files used before
# they were collected by pytest, and pytest reads any module-level
# callable named test_* or test as one -- then errors on the "fn"
# parameter it cannot supply as a fixture. Three ERRORs, in three files,
# from a helper doing its job.
test.__test__ = False


# ============================================================
# PART 1 — hint extraction (NL normalization)
# ============================================================
@test
def test_extracts_full_state_name():
    from backend.core.open_meteo_provider import _extract_location_hints
    hints = _extract_location_hints("Orange County, Virginia 22960")
    assert hints.state_name == "Virginia", hints
    assert hints.zip_code == "22960", hints


@test
def test_extracts_two_letter_state_abbreviation():
    from backend.core.open_meteo_provider import _extract_location_hints
    hints = _extract_location_hints("Orange County VA")
    assert hints.state_name == "Virginia", hints
    assert hints.zip_code is None


@test
def test_lowercase_two_letter_abbreviation_is_not_treated_as_a_state():
    from backend.core.open_meteo_provider import _extract_location_hints
    # "va" lowercase must never false-positive off ordinary words —
    # only the canonical uppercase USPS form counts as a state hint.
    hints = _extract_location_hints("lava fields")
    assert hints.state_name is None, hints


@test
def test_new_york_multiword_state_name_is_not_shadowed_by_a_shorter_match():
    from backend.core.open_meteo_provider import _extract_location_hints
    hints = _extract_location_hints("Buffalo, New York")
    assert hints.state_name == "New York", hints


@test
def test_lowercase_trailing_abbreviation_is_trusted_as_a_state_hint():
    from backend.core.open_meteo_provider import _extract_location_hints
    hints = _extract_location_hints("orange county va")
    assert hints.state_name == "Virginia", hints


@test
def test_lowercase_trailing_abbreviation_before_a_zip_is_trusted():
    from backend.core.open_meteo_provider import _extract_location_hints
    hints = _extract_location_hints("richmond va 23219")
    assert hints.state_name == "Virginia", hints
    assert hints.zip_code == "23219"


@test
def test_lowercase_non_trailing_two_letter_word_is_not_treated_as_a_state():
    from backend.core.open_meteo_provider import _extract_location_hints
    # "in" sits mid-phrase here, not at the end — the trailing-token
    # heuristic must not fire on it (Seattle, the actual trailing word,
    # is 7 letters and isn't a state code either).
    hints = _extract_location_hints("restaurants in seattle")
    assert hints.state_name is None, hints


@test
def test_trailing_two_letter_word_that_is_not_a_real_state_code_is_ignored():
    from backend.core.open_meteo_provider import _extract_location_hints
    hints = _extract_location_hints("call me at ab")
    assert hints.state_name is None, hints


@test
def test_no_hints_found_returns_all_none():
    from backend.core.open_meteo_provider import _extract_location_hints
    hints = _extract_location_hints("Nowhereville")
    assert hints.state_name is None
    assert hints.zip_code is None


# ============================================================
# PART 2 — disambiguation / confidence scoring
# ============================================================
@test
def test_zip_plus_state_hint_wins_over_population_and_ordering():
    from backend.core.open_meteo_provider import _disambiguate_candidates, LocationHints
    # Plédran, France listed FIRST and more populous, and shares the
    # EXACT same ZIP as Orange, VA — a real, live cross-country postal
    # code collision. ZIP alone can't break this tie (see the next
    # test); the state hint together with the ZIP must.
    best, confidence = _disambiguate_candidates(
        [_PLEDRAN_FR, _ORANGE_VA], LocationHints(state_name="Virginia", zip_code="22960")
    )
    assert best["name"] == "Orange" and best["admin1"] == "Virginia", best
    assert confidence == "high"


@test
def test_zip_alone_shared_by_two_countries_is_only_medium_confidence():
    from backend.core.open_meteo_provider import _disambiguate_candidates, LocationHints
    # With no state hint to break the tie, a shared ZIP across two
    # countries is genuinely ambiguous — picking by population is the
    # least-bad guess, but it must not be reported as "high" confidence.
    best, confidence = _disambiguate_candidates(
        [_PLEDRAN_FR, _ORANGE_VA], LocationHints(state_name=None, zip_code="22960")
    )
    assert best is not None
    assert confidence == "medium"


@test
def test_state_hint_picks_the_correct_same_named_city():
    from backend.core.open_meteo_provider import _disambiguate_candidates, LocationHints
    best, confidence = _disambiguate_candidates(
        [_ORANGE_CT, _ORANGE_VA], LocationHints(state_name="Virginia", zip_code=None)
    )
    assert best["admin1"] == "Virginia", best
    assert confidence == "high"


@test
def test_multiple_state_matches_break_tie_by_population_as_medium_confidence():
    from backend.core.open_meteo_provider import _disambiguate_candidates, LocationHints
    small_va_town = dict(_ORANGE_VA, name="Tinytown", population=50)
    best, confidence = _disambiguate_candidates(
        [small_va_town, _ORANGE_VA], LocationHints(state_name="Virginia", zip_code=None)
    )
    assert best["name"] == "Orange", best
    assert confidence == "medium"


@test
def test_state_hint_with_no_matching_candidate_falls_back_to_population_as_low_confidence():
    from backend.core.open_meteo_provider import _disambiguate_candidates, LocationHints
    best, confidence = _disambiguate_candidates(
        [_ORANGE_CT, _PLEDRAN_FR], LocationHints(state_name="Virginia", zip_code=None)
    )
    # Neither candidate is in Virginia — still returns the best real
    # answer available (never refuses), but marks it low-confidence.
    assert best is not None
    assert confidence == "low"


@test
def test_single_candidate_no_hints_is_high_confidence():
    from backend.core.open_meteo_provider import _disambiguate_candidates, LocationHints
    best, confidence = _disambiguate_candidates([_ORANGE_VA], LocationHints(state_name=None, zip_code=None))
    assert best["name"] == "Orange"
    assert confidence == "high"


@test
def test_multiple_candidates_no_hints_picks_most_populous_as_medium_confidence():
    from backend.core.open_meteo_provider import _disambiguate_candidates, LocationHints
    best, confidence = _disambiguate_candidates(
        [_ORANGE_VA, _ORANGE_CT], LocationHints(state_name=None, zip_code=None)
    )
    assert best["name"] == "Orange" and best["admin1"] == "Connecticut", best  # higher population
    assert confidence == "medium"


@test
def test_empty_candidates_returns_none_and_low():
    from backend.core.open_meteo_provider import _disambiguate_candidates, LocationHints
    best, confidence = _disambiguate_candidates([], LocationHints(state_name=None, zip_code=None))
    assert best is None
    assert confidence == "low"


# ============================================================
# PART 3 — geocode() end-to-end (candidates monkeypatched, no network)
# ============================================================
@test
def test_literal_query_resolving_never_tries_a_fallback():
    from backend.core import open_meteo_provider

    calls = []

    def fake_candidates(name, count=8):
        calls.append(name)
        return [_ORANGE_VA]

    p = _Patcher()
    try:
        p.set(open_meteo_provider, "_geocode_candidates", fake_candidates)
        result = open_meteo_provider.geocode("Orange, VA")
    finally:
        p.restore()

    assert calls == ["Orange, VA"], calls
    assert result["name"] == "Orange"
    assert result["geocodeConfidence"] == "high"


@test
def test_county_and_zip_query_falls_back_to_simplified_variant():
    from backend.core import open_meteo_provider

    calls = []

    def fake_candidates(name, count=8):
        calls.append(name)
        if name == "Orange County, VA 22960":
            return []
        return [_ORANGE_VA]

    p = _Patcher()
    try:
        p.set(open_meteo_provider, "_geocode_candidates", fake_candidates)
        result = open_meteo_provider.geocode("Orange County, VA 22960")
    finally:
        p.restore()

    assert calls[0] == "Orange County, VA 22960"
    assert len(calls) > 1, "expected a simplified fallback query after the literal one found nothing"
    assert result is not None
    assert result["name"] == "Orange"


@test
def test_geocode_resolves_the_correct_orange_when_geocoder_returns_both_and_zip_present():
    """
    The exact real-world scenario this patch was written for: a query
    carrying both a state-ish token and a ZIP, where the geocoder's own
    result ordering would have picked the wrong place.
    """
    from backend.core import open_meteo_provider

    def fake_candidates(name, count=8):
        return [_PLEDRAN_FR, _ORANGE_VA]  # France listed first, as it is live

    p = _Patcher()
    try:
        p.set(open_meteo_provider, "_geocode_candidates", fake_candidates)
        result = open_meteo_provider.geocode("Orange County, VA 22960")
    finally:
        p.restore()

    assert result is not None
    assert result["region"] == "Virginia", result
    assert result["geocodeConfidence"] == "high"


@test
def test_ambiguous_result_with_no_hints_still_resolves_at_medium_confidence():
    from backend.core import open_meteo_provider

    def fake_candidates(name, count=8):
        return [_ORANGE_VA, _ORANGE_CT]

    p = _Patcher()
    try:
        p.set(open_meteo_provider, "_geocode_candidates", fake_candidates)
        result = open_meteo_provider.geocode("Orange")
    finally:
        p.restore()

    assert result is not None
    assert result["geocodeConfidence"] == "medium"


@test
def test_last_resort_state_reconstruction_variant_used_when_lighter_cleanup_fails():
    """
    The lighter cleanup (strip ZIP + the word "county") still leaves
    "Springfield, Sangamon , Illinois" — a shape no real geocoder
    understands, since "Sangamon" alone isn't a place Open-Meteo can
    resolve. The last-resort variant collapses this to just
    "<first segment>, <state>" = "Springfield, Illinois".
    """
    from backend.core import open_meteo_provider

    calls = []

    def fake_candidates(name, count=8):
        calls.append(name)
        if name == "Springfield, Illinois":
            return [dict(_ORANGE_VA, name="Springfield", admin1="Illinois")]
        return []

    p = _Patcher()
    try:
        p.set(open_meteo_provider, "_geocode_candidates", fake_candidates)
        result = open_meteo_provider.geocode("Springfield, Sangamon County, Illinois 62701")
    finally:
        p.restore()

    assert "Springfield, Illinois" in calls, calls
    assert result is not None
    assert result["name"] == "Springfield"


@test
def test_fallback_variant_strips_county_word_and_zip_not_real_words():
    from backend.core.open_meteo_provider import _simplified_geocode_variants, _extract_location_hints
    hints = _extract_location_hints("Orange County, VA 22960")
    variants = list(_simplified_geocode_variants("Orange County, VA 22960", hints))
    assert variants, "expected at least one fallback variant"
    assert "22960" not in variants[0]
    assert "county" not in variants[0].lower()
    assert "orange" in variants[0].lower()
    assert "va" in variants[0].lower()


@test
def test_no_fallback_variant_when_nothing_to_simplify():
    from backend.core.open_meteo_provider import _simplified_geocode_variants, _extract_location_hints
    hints = _extract_location_hints("Richmond, VA")
    variants = list(_simplified_geocode_variants("Richmond, VA", hints))
    assert variants == []


@test
def test_all_queries_failing_returns_none():
    from backend.core import open_meteo_provider

    p = _Patcher()
    try:
        p.set(open_meteo_provider, "_geocode_candidates", lambda name, count=8: [])
        result = open_meteo_provider.geocode("Nowhereville County, XY 00000")
    finally:
        p.restore()

    assert result is None


@test
def test_geocode_query_exception_is_caught_and_returns_none():
    from backend.core import open_meteo_provider

    def raising(name, count=8):
        raise RuntimeError("simulated network failure")

    p = _Patcher()
    try:
        p.set(open_meteo_provider, "_geocode_candidates", raising)
        result = open_meteo_provider.geocode("Orange County, VA 22960")
    finally:
        p.restore()

    assert result is None


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
