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
# named *_tests.py, which pytest's default collection does not match, so
# they are invisible to a plain `pytest backend/tests` run and only
# run_all_tests.py catches them.
#
# A test that wants a key sets one: monkeypatch is applied in fixture
# order, so anything requested after this replaces it.

from __future__ import annotations

import pytest

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
