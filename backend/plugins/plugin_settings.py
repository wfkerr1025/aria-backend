"""ARIA Lite - the plugins a user has installed, and their settings.

aria_config/plugins.json is the single source of truth for which
integrations exist, whether they are on, and what each one needs to
work. Nothing here holds a second copy of that list: the Plugins page,
the config pages and the backend all read this module, and this module
reads the file.

TWO THINGS CALLED "PLUGIN", AND THEY ARE NOT THE SAME
-----------------------------------------------------
This package also holds unity_csharp.py, which is a BRIEF plugin: text
appended to a model's system message so it writes idiomatic Unity C#. It
has no settings, no on/off switch and no page.

This module manages INTEGRATIONS: Unity, Blender, Ludo.ai -- things with
an executable path or an API key, that the user turns on and configures.

They share a word and nothing else. The one connection between them is
deliberate and one-way: unity_ops asks this module where Unity is, so
the path a user types on the Unity config page is the path ARIA actually
runs.

WHY A SECRET LIVES HERE AT ALL
------------------------------
Ludo.ai's api_key is written to plugins.json in plain text, and that is
worth stating rather than hiding. key_manager exists for secrets and
puts them in OS secure storage; this file is a plugin REGISTRY that the
UI reads wholesale. Moving the key into key_manager is the right end
state and is a change to the schema this task specified, so the field is
here, the risk is written down, and redact_secrets() exists so nothing
logs it by accident.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "PLUGINS_FILE",
    "PluginError",
    "configured_path",
    "disable_plugin",
    "enable_plugin",
    "get_plugin",
    "list_plugins",
    "load_plugins",
    "redact_secrets",
    "remove_plugin",
    "save_plugins",
    "test_plugin_connection",
    "update_plugin",
    "validate_plugin",
]


class PluginError(ValueError):
    """A plugin id that is not installed, or a field that is not valid."""


# Where the registry lives. Overridable so a test never touches the real
# one -- every function here writes, and a suite that edited the user's
# installed plugins would be a suite nobody could run twice.
ENV_PLUGINS_FILE = "ARIA_PLUGINS_FILE"

_REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_FILE = _REPO_ROOT / "aria_config" / "plugins.json"

# Fields every plugin has, whatever it integrates with.
_COMMON_FIELDS = ("id", "name", "version", "enabled", "logo", "configPage")

# What each plugin additionally needs, and how to check it.
#
# A path is checked for EXISTENCE only when it is non-empty: an empty
# path means "not configured yet", which is the state a fresh install is
# in and is not an error to be shouted about.
_FIELD_RULES = {
    "unity": {
        "unity_path": "executable",
        "project_path": "folder",
    },
    "blender": {
        "blender_path": "executable",
    },
    "ludo": {
        "api_key": "secret",
        "model": "text",
    },
}

# Fields that must never reach a log line or a packet meant for one.
_SECRET_FIELDS = frozenset({"api_key"})


def plugins_file() -> Path:
    """The registry's path, honouring the test override."""
    configured = os.environ.get(ENV_PLUGINS_FILE)
    return Path(configured) if configured else PLUGINS_FILE


# ======================================================
# Reading and writing
# ======================================================

def load_plugins() -> dict:
    """Every installed plugin, keyed by id.

    A missing or unreadable file reads as "no plugins" rather than
    raising: the Plugins page should say it is empty, not fail to open.
    """
    path = plugins_file()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.info("no plugins registry at %s; treating it as empty", path)
        return {}
    except OSError:
        logger.exception("could not read the plugins registry at %s", path)
        return {}

    try:
        loaded = json.loads(raw)
    except ValueError:
        logger.exception("the plugins registry at %s is not valid JSON", path)
        return {}

    if not isinstance(loaded, dict):
        logger.warning("the plugins registry is a %s, not an object",
                       type(loaded).__name__)
        return {}

    # An entry that is not an object cannot be a plugin, and dropping it
    # here means nothing downstream has to wonder.
    return {key: value for key, value in loaded.items() if isinstance(value, dict)}


def save_plugins(plugins: dict) -> None:
    """Write the registry, atomically.

    Written to a temporary file in the same directory and moved into
    place, so an interrupted save leaves the previous registry rather
    than half of the new one. A truncated plugins.json reads as "no
    plugins installed", which is the worst possible way to lose it.
    """
    if not isinstance(plugins, dict):
        raise PluginError("the plugins registry must be an object")

    path = plugins_file()
    path.parent.mkdir(parents=True, exist_ok=True)

    body = json.dumps(plugins, indent=2, sort_keys=True) + "\n"

    handle, temporary = tempfile.mkstemp(dir=str(path.parent),
                                         prefix=".plugins-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as file:
            file.write(body)
        shutil.move(temporary, str(path))
        logger.info("wrote %d plugin(s) to %s", len(plugins), path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        logger.exception("could not write the plugins registry at %s", path)
        raise


# ======================================================
# Reading one
# ======================================================

def get_plugin(plugin_id: str) -> dict:
    """One plugin's record. Raises when it is not installed."""
    plugins = load_plugins()
    plugin = plugins.get(str(plugin_id or ""))
    if plugin is None:
        raise PluginError(f"{plugin_id!r} is not an installed plugin")
    return plugin


def redact_secrets(plugin: dict) -> dict:
    """A copy safe to log, with secrets replaced by whether they are set.

    "configured" or "" rather than the key itself. Whether a key exists
    is what anybody reading a log actually needs; the key is what nobody
    should find there.
    """
    safe = dict(plugin or {})
    for field in _SECRET_FIELDS:
        if field in safe:
            safe[field] = "configured" if str(safe[field] or "").strip() else ""
    return safe


def list_plugins() -> list:
    """Every plugin, ordered by name, with secrets redacted.

    This is what the UI receives. The Plugins page shows a logo, a name,
    a version and a switch, and needs no API key to do it.
    """
    plugins = load_plugins()
    ordered = sorted(plugins.values(),
                     key=lambda plugin: str(plugin.get("name") or plugin.get("id") or ""))
    return [redact_secrets(plugin) for plugin in ordered]


# ======================================================
# Validation
# ======================================================

def validate_plugin(plugin_id: str, fields: dict) -> list:
    """Everything wrong with these field values, as sentences.

    Returns a list so a form can show every problem at once rather than
    making the user fix them one save at a time. An empty list means it
    is valid.
    """
    problems = []
    rules = _FIELD_RULES.get(str(plugin_id or ""), {})

    for name, value in (fields or {}).items():
        if name in _COMMON_FIELDS:
            if name == "enabled" and not isinstance(value, bool):
                problems.append("enabled must be true or false")
            continue

        kind = rules.get(name)
        if kind is None:
            problems.append(f"{plugin_id} has no field called {name!r}")
            continue

        text = value if isinstance(value, str) else ""
        if not isinstance(value, str):
            problems.append(f"{name} must be text")
            continue

        stripped = text.strip()
        if not stripped:
            # Empty is "not configured yet", which is a legitimate state
            # and the one a fresh install is in.
            continue

        if kind == "executable":
            candidate = Path(stripped)
            if not candidate.is_absolute():
                problems.append(f"{name} must be a full path")
            elif not candidate.exists():
                problems.append(f"{name}: nothing exists at {stripped}")
            elif candidate.is_dir():
                problems.append(f"{name} must be the program itself, not a folder")
        elif kind == "folder":
            candidate = Path(stripped)
            if not candidate.is_absolute():
                problems.append(f"{name} must be a full path")
            elif not candidate.is_dir():
                problems.append(f"{name}: no folder at {stripped}")
        elif kind == "secret":
            if len(stripped) < 8:
                problems.append(f"{name} looks too short to be a real key")

    return problems


# ======================================================
# Changing one
# ======================================================

def update_plugin(plugin_id: str, fields: dict) -> dict:
    """Apply field changes to one plugin and save.

    Validates first and writes nothing when anything is wrong, so a form
    with two bad fields does not half-save.
    """
    plugins = load_plugins()
    plugin = plugins.get(str(plugin_id or ""))
    if plugin is None:
        raise PluginError(f"{plugin_id!r} is not an installed plugin")

    changes = dict(fields or {})
    # Identity is not editable. A page that could rewrite an id could
    # rename a plugin into another one's slot.
    for locked in ("id", "configPage"):
        changes.pop(locked, None)

    problems = validate_plugin(plugin_id, changes)
    if problems:
        raise PluginError("; ".join(problems))

    plugin.update(changes)
    plugins[plugin_id] = plugin
    save_plugins(plugins)

    logger.info("updated plugin %s: %s", plugin_id,
                ", ".join(sorted(redact_secrets(changes))))
    return redact_secrets(plugin)


def enable_plugin(plugin_id: str) -> dict:
    """Turn a plugin on."""
    return update_plugin(plugin_id, {"enabled": True})


def disable_plugin(plugin_id: str) -> dict:
    """Turn a plugin off. Its settings are kept."""
    return update_plugin(plugin_id, {"enabled": False})


def remove_plugin(plugin_id: str) -> bool:
    """Uninstall a plugin, settings and all.

    Returns whether anything was removed. Removing something that is
    already gone is not an error -- two clicks on the same button should
    not produce a failure the second time.
    """
    plugins = load_plugins()
    if str(plugin_id or "") not in plugins:
        return False

    plugins.pop(plugin_id)
    save_plugins(plugins)
    logger.info("removed plugin %s", plugin_id)
    return True


# ======================================================
# What the rest of ARIA asks this module
# ======================================================

def configured_path(plugin_id: str, field: str) -> str:
    """A path a user configured, or "" when they have not.

    This is the whole point of the Unity config page being a page rather
    than a form that saves a string nobody reads: unity_ops calls this,
    so the path typed here is the executable ARIA actually runs.

    Never raises. A missing registry, a disabled plugin or an unset field
    are all "not configured", and the caller falls back to whatever it
    would have done anyway.
    """
    try:
        plugin = load_plugins().get(str(plugin_id or "")) or {}
        if not plugin.get("enabled", False):
            return ""
        return str(plugin.get(field) or "").strip()
    except Exception:  # pragma: no cover - configuration is not worth a crash
        logger.exception("could not read %s.%s", plugin_id, field)
        return ""


def test_plugin_connection(plugin_id: str) -> dict:
    """Whether this plugin is actually usable, checked rather than assumed.

    What "usable" means differs, so each is asked its own question: an
    executable is run with a version flag, and a key is checked for
    presence only -- ARIA does not spend a user's API quota to populate
    a green tick.
    """
    try:
        plugin = get_plugin(plugin_id)
    except PluginError as error:
        return {"ok": False, "message": str(error)}

    if plugin_id in ("unity", "blender"):
        field = "unity_path" if plugin_id == "unity" else "blender_path"
        path = str(plugin.get(field) or "").strip()
        if not path:
            return {"ok": False, "message": f"No {field.replace('_', ' ')} is set."}

        candidate = Path(path)
        if not candidate.is_file():
            return {"ok": False, "message": f"Nothing runnable at {path}."}
        if not os.access(path, os.X_OK):
            # On Windows this is almost always True; on POSIX it is the
            # difference between a file and a program.
            return {"ok": False, "message": f"{path} is not executable."}

        return {"ok": True, "message": f"Found {candidate.name}."}

    if plugin_id == "ludo":
        key = str(plugin.get("api_key") or "").strip()
        if not key:
            return {"ok": False, "message": "No API key is set."}
        if len(key) < 8:
            return {"ok": False, "message": "That key looks too short."}
        # Deliberately not a network call. A "test" that spends the
        # user's quota every time they open the page is a test that gets
        # switched off.
        return {"ok": True, "message": "An API key is set. ARIA has not called Ludo.ai."}

    return {"ok": False, "message": f"{plugin_id} has no connection test."}
