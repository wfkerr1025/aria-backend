"""A plugin's secret does not live in the file that gets committed.

THE FAILURE THIS EXISTS FOR
---------------------------
aria_config/plugins.json is BOTH the shipped default registry and the
live one, and it held Ludo.ai's API key in plain text. A routine
`git add -A` staged it:

    +    "api_key": "<the live Ludo.ai key>",
    -    "api_key": "",

It was caught before the commit, but only because somebody looked. The
key is now in key_manager -- Fernet-encrypted on disk with the master
key in OS secure storage -- and the file keeps an empty string.

WHAT THESE TESTS ARE REALLY CHECKING
------------------------------------
Not "can we store a key" -- keyring does that. They check the three
ways the value could get back into the file: a save that writes it, a
migration that copies without clearing, and a redaction that reads the
wrong source and reports every configured plugin as unconfigured.
"""

from __future__ import annotations

import json

import pytest

from backend.plugins import plugin_settings


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A registry file with a key still in it -- the pre-migration state."""
    path = tmp_path / "plugins.json"
    path.write_text(json.dumps({
        "ludo": {"id": "ludo", "name": "Ludo.ai", "version": "1.0.0",
                 "enabled": True, "logo": "", "configPage": "ludo-config",
                 "api_key": "a-key-that-should-not-be-here"},
    }, indent=2), encoding="utf-8")
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(path))
    return path


@pytest.fixture
def store(no_real_key_store):
    """The key store, as a dictionary.

    conftest swaps it out for every test in the suite; this is just a
    readable name for the dictionary it swapped in.
    """
    return no_real_key_store


# ======================================================
# The migration
# ======================================================

def test_a_key_in_the_file_is_moved_out_of_it(registry, store):
    value = plugin_settings.secret_for("ludo")

    assert value == "a-key-that-should-not-be-here"
    assert store["plugin:ludo"] == value
    assert json.loads(registry.read_text())["ludo"]["api_key"] == ""


def test_the_migration_happens_once(registry, store):
    plugin_settings.secret_for("ludo")
    store["plugin:ludo"] = "changed-since"

    # A second read must come from the store, not from the file again.
    assert plugin_settings.secret_for("ludo") == "changed-since"


def test_the_value_never_appears_in_the_file_afterwards(registry, store):
    plugin_settings.secret_for("ludo")

    assert "a-key-that-should-not-be-here" not in registry.read_text()


def test_a_plugin_with_no_key_migrates_nothing(registry, store):
    plugin_settings.update_plugin("ludo", {"api_key": ""})
    store.clear()

    assert plugin_settings.secret_for("ludo") == ""
    assert plugin_settings.has_secret("ludo") is False


# ======================================================
# Saving
# ======================================================

def test_saving_a_key_does_not_write_it_to_the_file(registry, store):
    plugin_settings.update_plugin("ludo", {"api_key": "brand-new-secret-value"})

    assert "brand-new-secret-value" not in registry.read_text()
    assert json.loads(registry.read_text())["ludo"]["api_key"] == ""
    assert store["plugin:ludo"] == "brand-new-secret-value"


def test_saving_a_key_alongside_another_field_keeps_both(registry, store):
    plugin_settings.update_plugin("ludo", {
        "api_key": "another-secret-value", "model": "blitz"})

    saved = json.loads(registry.read_text())["ludo"]
    assert saved["model"] == "blitz"
    assert saved["api_key"] == ""
    assert plugin_settings.secret_for("ludo") == "another-secret-value"


def test_a_rejected_form_stores_no_secret(registry, store):
    """Validation runs first, so a form with one bad field saves
    nothing at all -- including the good half."""
    with pytest.raises(plugin_settings.PluginError):
        plugin_settings.update_plugin("ludo", {
            "api_key": "a-good-enough-secret", "model": "not-a-real-model"})

    assert "plugin:ludo" not in store


def test_clearing_a_key_removes_it_from_the_store(registry, store):
    plugin_settings.update_plugin("ludo", {"api_key": "temporary-secret"})
    plugin_settings.update_plugin("ludo", {"api_key": ""})

    assert store.get("plugin:ludo") is None
    assert plugin_settings.has_secret("ludo") is False


# ======================================================
# What the UI is told
# ======================================================

def test_the_ui_is_told_configured_never_the_value(registry, store):
    plugin_settings.update_plugin("ludo", {"api_key": "a-secret-value"})

    shown = [p for p in plugin_settings.list_plugins() if p["id"] == "ludo"][0]
    assert shown["api_key"] == "configured"


def test_configured_is_read_from_the_store_not_the_file(registry, store):
    """The file's copy is always empty now. Reading it would report
    every configured plugin as unconfigured -- the page would show a
    working key as missing."""
    store["plugin:ludo"] = "a-secret-value"
    plugins = plugin_settings.load_plugins()
    plugins["ludo"]["api_key"] = ""
    plugin_settings.save_plugins(plugins)

    shown = plugin_settings.redact_secrets(plugin_settings.load_plugins()["ludo"])
    assert shown["api_key"] == "configured"


def test_an_unconfigured_plugin_says_so(registry, store):
    plugin_settings.update_plugin("ludo", {"api_key": ""})

    shown = plugin_settings.redact_secrets(plugin_settings.load_plugins()["ludo"])
    assert shown["api_key"] == ""


# ======================================================
# The store is shared, so the namespace matters
# ======================================================

def test_a_plugin_secret_is_namespaced(registry, store):
    """Without the prefix a plugin called "ludo" and a module called
    "ludo" would be the same entry in the same store."""
    plugin_settings.update_plugin("ludo", {"api_key": "a-secret-value"})

    assert "plugin:ludo" in store
    assert "ludo" not in store
    assert plugin_settings.PLUGIN_KEY_PREFIX == "plugin:"


def test_plugin_secrets_are_not_listed_as_modules(monkeypatch):
    """list_modules() reads every entry in the shared store, so without
    a filter "plugin:ludo" would appear on the Modules page as though
    somebody had installed a module by that name."""
    from backend.core import module_manager

    monkeypatch.setattr(module_manager.key_manager, "list_module_entries",
                        lambda: ["weather", "plugin:ludo", "openweathermap"])
    monkeypatch.setattr(module_manager.key_manager, "list_module_keys",
                        lambda: ["plugin:ludo"])
    monkeypatch.setattr(module_manager.key_manager, "ensure_module_entry",
                        lambda name: None)

    names = [m["name"] for m in module_manager.list_modules()]
    assert "plugin:ludo" not in names
    assert "weather" in names


# ======================================================
# Everything reads through the one door
# ======================================================

def test_the_ludo_client_reads_the_store(registry, store, monkeypatch):
    monkeypatch.delenv("ARIA_LUDO_API_KEY", raising=False)
    from backend.ludo import ludo_client

    plugin_settings.update_plugin("ludo", {"api_key": "the-clients-secret"})

    assert ludo_client.api_key() == "the-clients-secret"


def test_the_environment_still_wins(registry, store, monkeypatch):
    """A shell overriding one run is the same order every path in this
    codebase uses."""
    from backend.ludo import ludo_client

    plugin_settings.update_plugin("ludo", {"api_key": "the-stored-secret"})
    monkeypatch.setenv("ARIA_LUDO_API_KEY", "the-environments-secret")

    assert ludo_client.api_key() == "the-environments-secret"


def test_the_connection_test_reads_the_store(registry, store, monkeypatch):
    """The Test button used to read plugin["api_key"] straight from the
    dict it was handed. That dict now always carries an empty string."""
    plugin_settings.update_plugin("ludo", {"api_key": "a-secret-value"})
    asked = {}

    def fake_fetch(url, headers=None, **kwargs):
        asked["auth"] = (headers or {}).get("Authorization", "")
        return {"status": "ok", "code": 204, "data": None}

    monkeypatch.setattr("tools.http_fetch.http_fetch", fake_fetch)
    plugin_settings.test_plugin_connection("ludo")

    assert "a-secret-value" in asked.get("auth", "")


def test_only_declared_secret_fields_are_accepted():
    """A caller asking to store some other field as a secret is a
    mistake, and a silent one if it were allowed."""
    for bad in ("model", "output_dir", "blender_path"):
        with pytest.raises(plugin_settings.PluginError):
            plugin_settings.secret_for("ludo", bad)
        with pytest.raises(plugin_settings.PluginError):
            plugin_settings.set_secret("ludo", "x", bad)


def test_a_broken_key_store_does_not_lose_the_plugin(registry, monkeypatch):
    """If key_manager cannot be reached, the migration still returns
    the value it found -- refusing would break a Ludo turn to make a
    filing point."""
    import backend.core.key_manager as real

    def explode(*args, **kwargs):
        raise RuntimeError("no keyring on this machine")

    monkeypatch.setattr(real, "get_module_key", explode)
    monkeypatch.setattr(real, "set_module_key", explode)

    assert plugin_settings.secret_for("ludo") == "a-key-that-should-not-be-here"


# ======================================================
# The file itself
# ======================================================

def test_the_shipped_registry_carries_no_key():
    """The real file, as committed. This is the test that would have
    failed before the move, and the one that fails if a key ever gets
    written back."""
    real = json.loads(plugin_settings.PLUGINS_FILE.read_text(encoding="utf-8"))

    for name, entry in real.items():
        if not isinstance(entry, dict):
            continue
        for field in plugin_settings._SECRET_FIELDS:
            assert not str(entry.get(field) or "").strip(), (
                f"{name}.{field} holds a secret in a file that gets committed")


def test_the_encrypted_store_is_not_in_the_repository():
    """Encrypted or not, it is a secret and has no business in git."""
    import subprocess

    # The real location, spelled out. Asking key_manager would ask the
    # temp path conftest redirects it to, which git rightly says is
    # outside the repository -- a test that can only ever fail.
    root = plugin_settings.PLUGINS_FILE.parent.parent
    result = subprocess.run(
        ["git", "check-ignore", "backend/config/module_keys.enc.json"],
        capture_output=True, text=True, cwd=str(root))

    assert result.returncode == 0, "the key store is not gitignored"
