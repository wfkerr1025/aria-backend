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
        "model": "choice",
    },
}

# Fields that must never reach a log line or a packet meant for one.
_SECRET_FIELDS = frozenset({"api_key"})

# What a plugin's dropdown fields may be set to.
#
# Held here rather than in the page, so the list the user picks from and
# the list the backend accepts are the same list. A dropdown whose
# options the validator has never heard of is a form that can only be
# saved by not using it.
#
# Ludo.ai's real model names are not something ARIA can look up offline,
# so these are placeholders and are marked as such in the UI. An empty
# value is always allowed and means "their default".
FIELD_CHOICES = {
    "ludo": {
        "model": ("", "ludo-default", "ludo-fast", "ludo-quality"),
    },
}

# Where the Ludo.ai connection test goes.
#
# THE HOST IS VERIFIED, THE PATH IS NOT.
# api.ludo.ai resolves and answers -- that much was checked from this
# machine. What is not verified is the route, because Ludo.ai's API
# documentation is not something ARIA has, and probing a third party's
# server until something answers is not a reasonable way to find out.
#
# So the path below is a conventional guess, and both halves are
# environment-configurable: when the real endpoint is known it is one
# variable, not a code change.
#
# The root path "/" was measured returning 200 with an empty body for
# any request, which is why it is NOT used here. A probe that answers
# "connected" to a key of "xxxxxxxx" is not a test of anything, and the
# failure it hides is the one the user most needs to see.
ENV_LUDO_BASE = "ARIA_LUDO_API_BASE"
ENV_LUDO_PATH = "ARIA_LUDO_API_PATH"
LUDO_API_BASE = "https://api.ludo.ai"
LUDO_PROBE_PATH = "/v1/models"

# Short on purpose. This runs when somebody presses a button and watches
# the page, so an unreachable host must say so quickly rather than
# holding the UI for the length of a TCP timeout.
LUDO_TIMEOUT_SECONDS = 6


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
        elif kind == "choice":
            allowed = FIELD_CHOICES.get(str(plugin_id or ""), {}).get(name, ())
            if allowed and stripped not in allowed:
                problems.append(
                    f"{name} must be one of: "
                    + ", ".join(choice or "(default)" for choice in allowed))

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

    if plugin_id == "unity":
        return _test_unity()
    if plugin_id == "blender":
        return _test_executable(plugin, "blender_path", "Blender")
    if plugin_id == "ludo":
        return _test_ludo(plugin)

    return {"ok": False, "message": f"{plugin_id} has no connection test."}


def _test_unity() -> dict:
    """Whether ARIA can actually find Unity.

    Asks unity_ops rather than reading the field, because unity_ops is
    what will run it: it checks the environment variable, then this
    plugin's path, then a Unity Hub install, then PATH. A test that only
    looked at the field would say "not configured" on a machine where
    Unity is found perfectly well, and would say "found" for a path the
    thing that runs Unity would never consult.
    """
    try:
        from backend.core import unity_ops

        editor = unity_ops.editor_path()
    except Exception as error:
        # UnityUnavailable carries the sentence to show, and anything
        # else is reported rather than swallowed.
        return {"ok": False, "message": str(error)}

    return {"ok": True, "message": f"Found {editor.name} at {editor}."}


def _test_executable(plugin: dict, field: str, label: str) -> dict:
    """Whether a configured program is there and runnable."""
    path = str(plugin.get(field) or "").strip()
    if not path:
        return {"ok": False, "message": f"No {label} path is set."}

    candidate = Path(path)
    if not candidate.is_file():
        return {"ok": False, "message": f"Nothing runnable at {path}."}
    if not os.access(path, os.X_OK):
        return {"ok": False, "message": f"{path} is not executable."}

    return {"ok": True, "message": f"Found {candidate.name}."}


def _test_ludo(plugin: dict) -> dict:
    """Ask Ludo.ai whether this key works.

    This one leaves the machine, which nothing else in this module does,
    so it is worth being explicit about what that means: it runs only
    when somebody presses the button, it sends the key to the configured
    host and nothing else, it has a six-second budget, and it never logs
    the key or the response body.

    THE PATH IS A GUESS, AND A 404 SAYS SO
    --------------------------------------
    The host answers; the route is unverified (see LUDO_PROBE_PATH). So
    a 404 means "a server took this request and does not have this
    path", which is emphatically not "your key is wrong" -- and the
    message says the former rather than blaming the key, because a user
    who deletes a working key on ARIA's bad advice has been actively
    harmed by a test that was supposed to help.
    """
    key = str(plugin.get("api_key") or "").strip()
    if not key:
        return {"ok": False, "message": "No API key is set."}
    if len(key) < 8:
        return {"ok": False, "message": "That key looks too short."}

    base = str(os.environ.get(ENV_LUDO_BASE) or LUDO_API_BASE).rstrip("/")
    path = str(os.environ.get(ENV_LUDO_PATH) or LUDO_PROBE_PATH)
    if not path.startswith("/"):
        path = "/" + path
    url = base + path

    try:
        from tools.http_fetch import http_fetch

        # Reusing the project's client rather than reaching for requests:
        # its own docstring says a caller that does that duplicates the
        # error handling and misses the next fix to it.
        answer = http_fetch(url, headers={"Authorization": f"Bearer {key}"},
                            timeout=LUDO_TIMEOUT_SECONDS)
    except Exception as error:
        logger.exception("the Ludo.ai connection test could not run")
        return {"ok": False, "message": f"The test could not run: {error}"}

    if answer.get("status") == "ok":
        return {"ok": True, "message": f"Ludo.ai answered at {url}."}

    code = answer.get("code")
    if code in (401, 403):
        return {"ok": False, "message": "Ludo.ai rejected that key."}
    if code == 404:
        return {"ok": False,
                "message": (f"Reached Ludo.ai, but it has no {path}. Your key was "
                            f"not checked and may be perfectly good -- ARIA does "
                            f"not know Ludo.ai's API route. Set {ENV_LUDO_PATH} "
                            f"(and {ENV_LUDO_BASE} if needed) to the real one.")}
    if code == 429:
        return {"ok": False, "message": "Ludo.ai rate-limited the test. Try again shortly."}
    if code:
        return {"ok": False, "message": f"Ludo.ai answered HTTP {code}."}

    # No code at all means it never got that far -- no network, DNS, TLS.
    return {"ok": False, "message": f"Could not reach {base}."}
