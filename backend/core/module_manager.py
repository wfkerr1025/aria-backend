# backend/core/module_manager.py
#
# Registry of "module" keys — API keys for feature modules (weather,
# and any user-added module) that aren't LLM providers, so they're kept
# out of key_manager.PROVIDER_ENV_VARS. Storage itself (AES-encrypted
# JSON file, master key in OS secure storage) lives in key_manager.py;
# this module just owns the list of known modules and exposes a small,
# stable API for the IPC layer / settings UI.

from __future__ import annotations

import os

from backend.core import key_manager
from logger import get_logger

logger = get_logger(__name__)

# Modules ARIA-Lite ships with a concept of today. Not exhaustive — any
# module_name can be used with set_module_key()/get_module_key() below;
# this list only decides what shows up in the settings UI even before a
# key has been entered for it, so "add a new module" doesn't require a
# code change here, just calling set_module_key() with a new name.
KNOWN_MODULES = ("weather", "openweathermap")

# module name -> the environment variable its tool file actually reads
# (tools/get_weather.py's WEATHERAPI_KEY; backend.core.openweather_
# provider's OPENWEATHERMAP_API_KEY — Batch 3.5). Mirrors key_manager.
# PROVIDER_ENV_VARS' pattern for LLM providers — same reasoning: keeps
# the tool file itself unmodified beyond reading the env var fresh per
# call (see tools/get_weather.py's _weatherapi_key() and
# backend.core.openweather_provider._api_key()) rather than needing
# every tool file to import module_manager directly.
MODULE_ENV_VARS = {
    "weather": "WEATHERAPI_KEY",
    "openweathermap": "OPENWEATHERMAP_API_KEY",
}


def list_modules() -> list[dict]:
    """
    [{ name, configured }] for every module actually in the key store —
    never includes key values.

    Self-discovered modules (KNOWN_MODULES) are materialized into the
    store as real, empty entries ({"module_name": ..., "api_key": None} —
    see key_manager.ensure_module_entry()) before the store is read, so a
    known-but-unconfigured module behaves EXACTLY like a user-added one
    from here on: it's a real entry, not a value computed on the fly by
    unioning KNOWN_MODULES with whatever has a key. Idempotent — calling
    this repeatedly never creates duplicate or extra entries.
    """
    for name in KNOWN_MODULES:
        key_manager.ensure_module_entry(name)

    configured = set(key_manager.list_module_keys())
    names = key_manager.list_module_entries()

    # The key store is shared. Plugin secrets live there too, under a
    # "plugin:" prefix (see plugin_settings.PLUGIN_KEY_PREFIX), and
    # listing them here would put "plugin:ludo" on the Modules page as
    # though somebody had installed a module by that name.
    names = [name for name in names if ":" not in str(name)]

    return [{"name": name, "configured": name in configured} for name in names]


def set_module_key(module_name: str, api_key: str) -> dict:
    ok = key_manager.set_module_key(module_name, api_key)
    if not ok:
        return {"ok": False, "reason": "module_name and api_key are both required"}
    env_var = MODULE_ENV_VARS.get(module_name)
    if env_var:
        os.environ[env_var] = api_key
    return {"ok": True, "module": module_name}


def get_module_key(module_name: str) -> str | None:
    return key_manager.get_module_key(module_name)


def delete_module_key(module_name: str) -> dict:
    ok = key_manager.delete_module_key(module_name)
    if not ok:
        return {"ok": False, "reason": f"No key stored for module '{module_name}'"}
    env_var = MODULE_ENV_VARS.get(module_name)
    if env_var:
        os.environ.pop(env_var, None)
    return {"ok": True, "module": module_name}


def sync_module_keys_to_env() -> None:
    """
    Populate os.environ from the encrypted module-key store for every
    module that has one — call once at process startup (see server.py /
    ws_server.py, alongside key_manager.sync_provider_keys_to_env()) so
    a weather key entered through the settings UI in an earlier run is
    live from the very first request. A real environment variable
    already set takes precedence and is never overwritten.
    """
    synced = []
    for module_name, env_var in MODULE_ENV_VARS.items():
        if os.getenv(env_var):
            continue
        stored = key_manager.get_module_key(module_name)
        if stored:
            os.environ[env_var] = stored
            synced.append(module_name)

    if synced:
        logger.info(f"sync_module_keys_to_env() — synced {len(synced)} module(s): {synced}")
