# backend/tests/conftest.py
#
# No test reads the machine's key store, and no test calls a provider's
# live API.
#
# Both suites that exercise web_search say so in their own headers -- "a
# suite that depends on Yahoo being up, or on a key existing on the
# machine running it, is a suite that fails for reasons that have nothing
# to do with the code" -- and enforced it per-provider, by stubbing each
# one's api_key inside a fixture.
#
# That held exactly as long as the provider list did. Adding LangSearch
# as a fallback tier meant a test that stubbed every specialist and
# asserted a hard miss reached a provider nobody had thought to stub,
# found a real key in the developer's store, and made a live billed API
# call -- returning ten real results where the test expected none. Three
# tests failed saying "assert 10 == 0", which is a good outcome; the bad
# outcome was the quota being spent quietly by a test run.
#
# So the guard is here, once, rather than in each fixture.
#
# It stubs the providers' own api_key, NOT key_manager. Blanking
# key_manager.get_module_key looked equivalent and was not: it breaks the
# suites whose subject is the key store itself -- round-tripping a module
# key, migrating the legacy flat file format -- which then fail for a
# reason that has nothing to do with what they test. Those live in files
# named *_tests.py -- which pytest's default collection did not match,
# so for a long time they were invisible to `pytest backend/tests` and
# this guard was protecting suites that were not running. pytest.ini now
# names both patterns, so they run, and so does this.
#
# A test that wants a key sets one: monkeypatch is applied in fixture
# order, so anything requested after this replaces it.

from __future__ import annotations

import pytest

from backend.core import turn_orchestrator
from tools.providers import (
    exchange_rate_api, news_mediastack, news_nytimes, web_langsearch,
)

# Every provider that reads a user-entered key, and the environment
# variable that would supply one.
KEYED_PROVIDERS = (web_langsearch, news_nytimes, news_mediastack, exchange_rate_api)


@pytest.fixture(autouse=True)
def no_machine_keys(monkeypatch):
    """Every keyed provider is unconfigured unless a test says otherwise."""
    for module in KEYED_PROVIDERS:
        monkeypatch.delenv(module.ENV_VAR, raising=False)
        monkeypatch.setattr(module, "api_key", lambda: None)


@pytest.fixture(autouse=True)
def no_live_classifier(monkeypatch):
    """The search classifier consults no model during the suite.

    orchestrate_turn builds a real generator for it, so without this the
    suite makes a live model call on every turn -- and whether that load
    succeeds depends on how busy the machine is. Two runs of the same
    characterization suite disagreed with each other for exactly that
    reason, which is the failure mode these tests exist to detect, not to
    exhibit.

    Only the building is suppressed: a generator a test passes in
    explicitly is still used, so tests of the classifier itself work
    unchanged. Tests of the builder restore the real one.
    """
    monkeypatch.setattr(
        turn_orchestrator, "_classifier_generator",
        lambda request, default_local_model, supplied: supplied,
    )


@pytest.fixture(autouse=True)
def quiet_safety_gate(monkeypatch):
    """The safety gate answers "safe" unless a test says otherwise.

    The gate projects a model's RAM against the machine's CURRENT free
    memory, so its verdict depends on what else happens to be running.
    That was harmless while the suite routed turns to fake model ids the
    gate had no config for and skipped -- and stopped being harmless the
    moment turns started resolving to real installed models, which is
    what the routing layer does. Whole files then passed or failed with
    the developer's browser.

    This suite has already lost time to exactly that: an evidence test
    that put a 12B in front of the live gate passed three runs out of
    five on unchanged code. A characterization suite whose verdict moves
    with machine load is not characterizing the code.

    Only the ambient case is quieted. Every test whose subject IS the
    gate monkeypatches evaluate_safety itself, and a fixture applied here
    is replaced by one applied in the test body -- so refusal, bypass and
    warning-shape tests all still exercise the real branch.
    """
    from backend.core.safety_manager import PerformanceProfile, ResourceSnapshot, SafetyDecision

    def _safe(model_cfg):
        return SafetyDecision(
            safe_to_run=True, requires_warning=False, severity="ok",
            message="", profile=None, snapshot=None,
        )

    monkeypatch.setattr(turn_orchestrator, "evaluate_safety", _safe)
