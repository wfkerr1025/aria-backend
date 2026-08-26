# backend/core/key_manager.py
#
# Secret storage for ARIA-Lite:
#   - Cloud LLM provider API keys → OS secure storage (Windows Credential
#     Manager / macOS Keychain / Linux libsecret) via the `keyring`
#     package. The key value itself never touches disk in plaintext.
#   - "Module" keys (weather, and any user-added module) → a local JSON
#     file, values AES-encrypted (via `cryptography`'s Fernet, which is
#     AES-128-CBC + HMAC-SHA256 authenticated encryption — not raw AES,
#     but AES-based and safer against implementation mistakes than
#     hand-rolled AES-CBC/GCM). The AES master key that protects that
#     file is itself stored in OS secure storage via keyring, not on
#     disk — so the encrypted file alone is useless without OS-level
#     access to this machine/user account, consistent with how the LLM
#     provider keys are protected.
#
# Verified against the real Windows Credential Manager backend
# (keyring.backends.Windows.WinVaultKeyring) on this machine. The
# macOS Keychain and Linux libsecret backends are keyring's own
# built-in backends (selected automatically by `keyring.get_keyring()`
# per-OS) — implemented against keyring's documented cross-platform
# API, but not exercised on those platforms here.

from __future__ import annotations

import json
import os

import keyring
from cryptography.fernet import Fernet, InvalidToken

from backend.logger import log as unified_log
from logger import get_logger

logger = get_logger(__name__)

# ============================================================
# Cloud LLM provider keys (OS secure storage)
# ============================================================

# keyring "service" name each provider key is stored under. One service,
# one entry per provider (the provider name is the keyring "username").
_LLM_KEYRING_SERVICE = "aria-lite-llm"

# Provider name → the environment variable each wrapper in
# backend/llm/providers/*_wrapper.py actually reads (os.getenv(...)).
# Keeping this mapping here (rather than editing 13 wrapper files) means
# sync_provider_keys_to_env() below is the ONLY integration point — the
# wrappers themselves are untouched.
PROVIDER_ENV_VARS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "azure": "AZURE_OPENAI_KEY",
    "cohere": "COHERE_API_KEY",
    "custom_http": "CUSTOM_HTTP_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "grok": "XAI_API_KEY",
    "huggingface": "HUGGINGFACE_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "replicate": "REPLICATE_API_KEY",
    "together": "TOGETHER_API_KEY",
}

KNOWN_PROVIDERS = tuple(PROVIDER_ENV_VARS.keys())


def set_provider_key(provider: str, api_key: str) -> bool:
    if provider not in PROVIDER_ENV_VARS:
        logger.warning(f"set_provider_key() — unknown provider: {provider}")
        return False
    if not api_key:
        logger.warning(f"set_provider_key() — empty key rejected for {provider}")
        return False

    keyring.set_password(_LLM_KEYRING_SERVICE, provider, api_key)
    os.environ[PROVIDER_ENV_VARS[provider]] = api_key
    _reload_providers_if_available()
    logger.info(f"set_provider_key() — stored key for provider={provider}")
    unified_log("key_manager", "INFO", f"Provider API key set: {provider}", {"provider": provider})
    return True


def get_provider_key(provider: str) -> str | None:
    if provider not in PROVIDER_ENV_VARS:
        return None
    return keyring.get_password(_LLM_KEYRING_SERVICE, provider)


def delete_provider_key(provider: str) -> bool:
    if provider not in PROVIDER_ENV_VARS:
        return False
    try:
        keyring.delete_password(_LLM_KEYRING_SERVICE, provider)
    except keyring.errors.PasswordDeleteError:
        # Nothing was stored — deleting a not-present key is a no-op success,
        # not an error, from the caller's perspective.
        pass
    os.environ.pop(PROVIDER_ENV_VARS[provider], None)
    _reload_providers_if_available()
    logger.info(f"delete_provider_key() — removed key for provider={provider}")
    unified_log("key_manager", "INFO", f"Provider API key deleted: {provider}", {"provider": provider})
    return True


def _reload_providers_if_available() -> None:
    # Deferred import — backend.llm.providers.provider_registry imports
    # every wrapper (which import `requests`, `openai`, etc.) at module
    # load time; importing it lazily here, only when a key actually
    # changes, keeps key_manager itself lightweight and avoids a
    # potential circular import if a wrapper ever needs key_manager
    # directly in the future.
    from backend.llm.providers import provider_registry
    provider_registry.reload_providers()


def list_configured_providers() -> dict[str, bool]:
    """
    { provider_name: bool } — True if a key is available for that
    provider, from EITHER OS secure storage OR a plain environment
    variable (the wrappers have always supported the latter; this just
    reports on it too, so a provider configured the old way via env var
    still shows as available).
    """
    result = {}
    for provider, env_var in PROVIDER_ENV_VARS.items():
        has_keyring_key = bool(keyring.get_password(_LLM_KEYRING_SERVICE, provider))
        has_env_key = bool(os.getenv(env_var))
        result[provider] = has_keyring_key or has_env_key
    return result


def sync_provider_keys_to_env() -> None:
    """
    Populate os.environ from OS secure storage for every provider that
    has a stored key — call once at process startup (see server.py /
    ws_server.py) so the existing, unmodified provider wrappers (which
    only ever call os.getenv(...)) transparently pick up keys entered
    through the new settings UI / key_manager API, without editing any
    of the 13 wrapper files. A real environment variable already set
    takes precedence (never overwritten) — OS storage only fills gaps.
    """
    synced = []
    for provider, env_var in PROVIDER_ENV_VARS.items():
        if os.getenv(env_var):
            continue
        stored = keyring.get_password(_LLM_KEYRING_SERVICE, provider)
        if stored:
            os.environ[env_var] = stored
            synced.append(provider)

    if synced:
        logger.info(f"sync_provider_keys_to_env() — synced {len(synced)} provider(s): {synced}")
        unified_log("key_manager", "INFO", "Synced provider keys from secure storage to environment", {
            "providers": synced,
        })


# ============================================================
# Module keys (AES-encrypted local file, master key in OS storage)
# ============================================================

_MASTER_KEYRING_SERVICE = "aria-lite-master"
_MASTER_KEYRING_USERNAME = "module_key_master"

_MODULE_KEYS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "module_keys.enc.json"
)


def _get_or_create_master_key() -> bytes:
    existing = keyring.get_password(_MASTER_KEYRING_SERVICE, _MASTER_KEYRING_USERNAME)
    if existing:
        return existing.encode("utf-8")

    new_key = Fernet.generate_key()
    keyring.set_password(_MASTER_KEYRING_SERVICE, _MASTER_KEYRING_USERNAME, new_key.decode("utf-8"))
    logger.info("_get_or_create_master_key() — generated a new AES master key in OS secure storage")
    unified_log("key_manager", "INFO", "Generated new module-key AES master key")
    return new_key


def _load_module_keys_raw() -> dict:
    """
    {module_name: {"module_name": <name>, "api_key": <encrypted str | None>}}

    Transparently upgrades the legacy flat format ({module_name:
    <encrypted str>}, from before self-discovered KNOWN_MODULES entries
    got real key-store entries — see module_manager.py's list_modules())
    to the current nested shape, persisting the upgrade back to disk so
    it only happens once per file.
    """
    if not os.path.exists(_MODULE_KEYS_PATH):
        return {}
    with open(_MODULE_KEYS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    migrated = False
    for name, value in list(data.items()):
        if not isinstance(value, dict):
            data[name] = {"module_name": name, "api_key": value}
            migrated = True
    if migrated:
        logger.info("_load_module_keys_raw() — migrated legacy flat module-key entries to the nested format")
        _save_module_keys_raw(data)

    return data


def _save_module_keys_raw(data: dict) -> None:
    os.makedirs(os.path.dirname(_MODULE_KEYS_PATH), exist_ok=True)
    with open(_MODULE_KEYS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def ensure_module_entry(module_name: str) -> None:
    """
    Materialize an empty {"module_name": ..., "api_key": None} entry in
    the key store for `module_name` if it doesn't already have one — see
    module_manager.KNOWN_MODULES / list_modules(). This is what makes a
    self-discovered (built-in, not-yet-configured) module a REAL entry in
    the store rather than a value module_manager.list_modules() only
    ever computed on the fly — the frontend's RealModuleRow then treats
    it exactly like any user-added module, because as far as the store
    is concerned, it is one.
    """
    data = _load_module_keys_raw()
    if module_name in data:
        return
    data[module_name] = {"module_name": module_name, "api_key": None}
    _save_module_keys_raw(data)
    logger.info(f"ensure_module_entry() — created empty entry for module={module_name}")


def set_module_key(module_name: str, api_key: str) -> bool:
    if not module_name or not api_key:
        logger.warning("set_module_key() — module_name and api_key are both required")
        return False

    fernet = Fernet(_get_or_create_master_key())
    data = _load_module_keys_raw()
    data[module_name] = {
        "module_name": module_name,
        "api_key": fernet.encrypt(api_key.encode("utf-8")).decode("utf-8"),
    }
    _save_module_keys_raw(data)

    logger.info(f"set_module_key() — stored encrypted key for module={module_name}")
    unified_log("key_manager", "INFO", f"Module key set: {module_name}", {"module": module_name})
    return True


def get_module_key(module_name: str) -> str | None:
    data = _load_module_keys_raw()
    entry = data.get(module_name)
    encrypted = entry.get("api_key") if entry else None
    if not encrypted:
        return None

    fernet = Fernet(_get_or_create_master_key())
    try:
        return fernet.decrypt(encrypted.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        logger.error(f"get_module_key() — decryption failed for module={module_name} (corrupt data or master key mismatch)")
        unified_log("key_manager", "ERROR", f"Module key decryption failed: {module_name}", {"module": module_name})
        return None


def delete_module_key(module_name: str) -> bool:
    """
    Removes module_name's ENTIRE key-store entry (not just its api_key) —
    it disappears from list_module_entries() entirely. If module_name is
    one of module_manager.KNOWN_MODULES, the next list_modules() call
    re-materializes an empty entry for it via ensure_module_entry(); a
    user-added module just stays gone.
    """
    data = _load_module_keys_raw()
    if module_name not in data:
        return False
    del data[module_name]
    _save_module_keys_raw(data)
    logger.info(f"delete_module_key() — removed module={module_name}")
    unified_log("key_manager", "INFO", f"Module key deleted: {module_name}", {"module": module_name})
    return True


def list_module_keys() -> list[str]:
    """Module names that have a REAL (non-null) key configured. Never returns key values."""
    return sorted(name for name, entry in _load_module_keys_raw().items() if entry.get("api_key"))


def list_module_entries() -> list[str]:
    """
    ALL module names currently in the key store, configured or not —
    includes both real user-added/configured modules and self-discovered
    KNOWN_MODULES entries materialized by ensure_module_entry().
    """
    return sorted(_load_module_keys_raw().keys())
