# backend/tests/test_provider_key_lookup.py
#
# Reading a provider's key out of the key store.
#
# The store is keyed by whatever string the module was created with, and
# that string comes from a user typing a module name into the settings
# screen. A key entered as "LANGSEARCH" is stored under "LANGSEARCH";
# key_manager.get_module_key does an exact-case dict lookup; a provider
# asking for "langsearch" got None.
#
# This is the failure that made a fully working search stack answer "I
# could not retrieve current data for this query" on every general
# question. A provider with no key returns [] and calls nothing -- the
# correct, deliberate behaviour -- so a key that is present but
# unfindable looked exactly like a key that was never entered. The
# settings screen said "Key set". The search said it had nothing. Nothing
# anywhere disagreed with either.

from __future__ import annotations

import pytest

from tools.providers import provider_keys
from tools.providers import (
    exchange_rate_api, news_mediastack, news_nytimes, web_langsearch,
)

# Every provider that reads a user-entered module key. The names are
# read off the modules rather than restated here: a test that keeps its
# own copy of a constant is a test that can agree with itself while
# disagreeing with the code.
KEYED_PROVIDERS = [
    (module, module.MODULE_NAME, module.ENV_VAR)
    for module in (web_langsearch, news_nytimes, news_mediastack, exchange_rate_api)
]

# Captured at import, before conftest's autouse guard replaces them. That
# guard stubs every provider's api_key so no test reaches a live API by
# accident; this file's whole subject is the real lookup, so it puts the
# real one back.
REAL_API_KEY = {module: module.api_key for module, _, _ in KEYED_PROVIDERS}


@pytest.fixture(autouse=True)
def real_key_lookup(monkeypatch):
    """The genuine api_key, restored after the session-wide guard."""
    for module, _, _ in KEYED_PROVIDERS:
        monkeypatch.setattr(module, "api_key", REAL_API_KEY[module])


@pytest.fixture
def store(monkeypatch):
    """A fake key store, addressed exactly as key_manager addresses it."""
    entries: dict[str, str] = {}

    class FakeKeyManager:
        @staticmethod
        def get_module_key(name):
            return entries.get(name)

        @staticmethod
        def list_module_keys():
            return sorted(entries)

    import backend.core.key_manager as real
    for name in ("get_module_key", "list_module_keys"):
        monkeypatch.setattr(real, name, getattr(FakeKeyManager, name))
    return entries


@pytest.mark.parametrize("module,name,env_var", KEYED_PROVIDERS)
def test_a_key_stored_in_any_casing_is_found(store, module, name, env_var):
    store[name.upper()] = "stored-key"

    assert module.api_key() == "stored-key"


@pytest.mark.parametrize("stored_as", ["langsearch", "LANGSEARCH", "LangSearch", "Langsearch"])
def test_every_casing_a_user_might_type(store, stored_as):
    store[stored_as] = "stored-key"

    assert web_langsearch.api_key() == "stored-key"


def test_the_exact_name_is_preferred(store):
    store["langsearch"] = "exact-match"
    store["LANGSEARCH"] = "other-entry"

    # Not merely "a key whose name looks similar": if the store holds the
    # name the provider actually asked for, that is the one it gets.
    assert web_langsearch.api_key() == "exact-match"


def test_an_unrelated_module_is_not_borrowed_from(store):
    store["OPENWEATHERMAP"] = "weather-key"

    assert web_langsearch.api_key() is None


def test_an_empty_store_is_no_key(store):
    assert web_langsearch.api_key() is None


def test_a_blank_stored_value_is_no_key(store):
    store["LANGSEARCH"] = "   "

    # A whitespace key would be sent as an Authorization header and
    # rejected -- a request nobody needed to make.
    assert web_langsearch.api_key() is None


@pytest.mark.parametrize("module,name,env_var", KEYED_PROVIDERS)
def test_the_environment_wins_over_the_store(store, monkeypatch, module, name, env_var):
    store[name.upper()] = "stored-key"
    monkeypatch.setenv(env_var, "env-key")

    # So a key exported for a test run or a one-off takes effect without
    # having to be entered into the store.
    assert module.api_key() == "env-key"


def test_a_key_store_fault_is_not_a_search_failure(monkeypatch):
    import backend.core.key_manager as real

    def boom(name):
        raise RuntimeError("key store unreadable")

    monkeypatch.setattr(real, "get_module_key", boom)

    assert web_langsearch.api_key() is None
    assert web_langsearch.lookup("pytest release notes") == []


def test_module_key_is_read_fresh_every_time(store):
    assert web_langsearch.api_key() is None

    store["LANGSEARCH"] = "added-later"

    # No caching: a key entered in settings takes effect on the next
    # query, without a restart.
    assert web_langsearch.api_key() == "added-later"


def test_every_keyed_provider_uses_the_shared_helper():
    import inspect

    for module, _, _ in KEYED_PROVIDERS:
        source = inspect.getsource(module.api_key)
        # Four hand-rolled copies of this lookup is how one of them came
        # to be case-sensitive without the others noticing.
        assert "module_key(MODULE_NAME, ENV_VAR)" in source
        assert "get_module_key" not in source


def test_the_helper_reports_the_name_it_matched(store, monkeypatch):
    logged = []
    monkeypatch.setattr(provider_keys.logger, "info",
                        lambda msg, *args: logged.append(msg % args))
    store["LANGSEARCH"] = "stored-key"

    web_langsearch.api_key()

    # A key found under a different name than the one asked for is worth
    # saying out loud; silence here is what made this hard to see.
    assert any("LANGSEARCH" in line and "langsearch" in line for line in logged)


def test_a_matching_name_is_not_announced_every_time(store, monkeypatch):
    logged = []
    monkeypatch.setattr(provider_keys.logger, "info",
                        lambda msg, *args: logged.append(msg % args))
    store["langsearch"] = "stored-key"

    web_langsearch.api_key()

    # The line means "this needed a fallback". On every ordinary lookup
    # it would be noise, and noise is what a real signal hides in.
    assert logged == []
