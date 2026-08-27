"""Reading a provider's API key out of the environment or the key store.

The key store is keyed by whatever string the module was created with,
and that string comes from the user typing a module name into the
settings UI. A key entered as "LANGSEARCH" is stored under "LANGSEARCH";
key_manager.get_module_key does an exact-case dict lookup, so a provider
asking for "langsearch" gets None.

Which is a silent failure of exactly the worst kind. A provider with no
key returns [] and calls nothing -- correct behaviour, deliberately
chosen -- so a key that is present but unfindable is indistinguishable
from a key that was never entered. The settings screen says "Key set",
the search says "I could not retrieve current data for this query", and
nothing anywhere disagrees with either.

So the lookup is case-insensitive over the store's own entry names. Not
by lowercasing what gets saved -- that would rewrite the store under
users who already have keys in it -- but by matching against what is
actually there. The exact name is tried first, so nothing changes for a
store whose casing already lines up.
"""

from __future__ import annotations

import os

from logger import get_logger

logger = get_logger(__name__)

__all__ = ["module_key"]


def _clean(value) -> str | None:
    text = str(value).strip() if value else ""
    return text or None


def module_key(module_name: str, env_var: str) -> str | None:
    """This provider's key, or None. Read fresh, never cached.

    The environment wins, so a key exported for a test run or a one-off
    does not have to be entered into the store to take effect.
    """
    from_env = _clean(os.environ.get(env_var))
    if from_env:
        return from_env

    try:
        from backend.core import key_manager
    except ImportError:  # pragma: no cover - running outside the backend
        return None

    try:
        stored = _clean(key_manager.get_module_key(module_name))
        if stored:
            return stored

        # The name as stored, whatever case the user typed it in.
        wanted = module_name.strip().lower()
        for entry in key_manager.list_module_keys():
            if entry.strip().lower() == wanted and entry != module_name:
                logger.info(
                    "provider key for %s found under stored name %r",
                    module_name, entry,
                )
                return _clean(key_manager.get_module_key(entry))
    except Exception:  # pragma: no cover - a key store fault is not a search fault
        logger.exception("%s: could not read the stored key", module_name)
        return None

    return None
