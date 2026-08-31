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
    "COMMAND_TYPE",
    "discover_plugins",
    "list_commands",
    "merge_discovered",
    "refresh_unity_cli_commands",
    "validate_registry",
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
#
# "discovered" marks a plugin ARIA found on the machine rather than one
# the user added: found, not yet adopted. "dismissed" is the tombstone
# left when a discovered plugin is removed -- see remove_plugin.
_COMMON_FIELDS = ("id", "name", "version", "enabled", "logo", "configPage",
                  "discovered", "dismissed")

# Fields that are true or false and nothing else.
_FLAG_FIELDS = ("enabled", "discovered", "dismissed")

# What a discovered plugin is configured with, whatever it turned out to
# be. Every family discovery knows about is a program with a path, so
# one field covers all of them and validation has one rule to apply
# rather than one per family nobody has written yet.
DISCOVERED_FIELD = "executable_path"

# ONE REGISTRY, TWO KINDS OF RECORD
# ---------------------------------
# plugins.json holds integrations and, since Unity CLI, the commands a
# CLI reports it can run. That is deliberate: a second registry file
# would be a second thing to keep in step, and this codebase already
# has more of those than it needs.
#
# A command carries a "type"; a plugin does not. Everything that must
# tell them apart asks that one question, and list_plugins/list_commands
# are the two doors.
COMMAND_TYPE = "unity_cli_command"

# The keys a discovered COMMAND adds to a plugin record's usual set.
# Copied by name, so a finding cannot introduce a field nothing
# validates.
_DISCOVERY_EXTRAS = ("type", "plugin", "label", "command", "args", "group")

# What a command's own fields are, and how each is checked. Held apart
# from _FIELD_RULES because they belong to every command rather than to
# one id -- a command's id is unity_cmd_<whatever the CLI said>.
_COMMAND_FIELD_RULES = {
    "label": "text",
    "command": "text",
    "args": "arguments",
    "group": "text",
    "type": "locked",
    "plugin": "locked",
}

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
    # The Unity CLI is a different tool from the Unity Editor, so it is
    # a different plugin with its own path -- unity_path runs the editor
    # in batchmode, this runs the `unity` command. Sharing one field
    # would mean whichever was configured last broke the other.
    "unity_cli": {
        "unity_cli_path": "executable",
        "unity_cli_project": "folder",
        "unity_cli_mode": "choice",
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
    # Ludo.ai's real generation models, from its OpenAPI spec. These
    # replace three invented names -- ludo-default, ludo-fast,
    # ludo-quality -- that were placeholders written before the API was
    # known and would have been refused by every endpoint.
    #
    # This is the union across endpoints, not a set any one of them
    # accepts: sprite animation takes six of these, video takes five,
    # motion transfer takes three, and each has its own default. So an
    # empty value is the sensible setting and means "let each endpoint
    # use its own default", which is what the page says it means.
    "ludo": {
        "model": ("", "blitz", "standard", "eagle", "eagle-audio",
                  "forge", "forge-pixel", "tango"),
    },
    # The three the task names, plus empty for "do not pass --mode at
    # all", which is not the same as any of them.
    "unity_cli": {
        "unity_cli_mode": ("", "EditMode", "PlayMode", "BuildMode"),
    },
}

# Where the Ludo.ai connection test goes.
#
# VERIFIED against Ludo.ai's OpenAPI spec at
# https://api.ludo.ai/api-documentation/swagger.json (Ludo.ai API 0.9.2)
# and by calling the endpoint. An earlier version of this file guessed
# /v1/models and said so; the guess was wrong and this replaces it.
#
# /auth/validate-api-key exists for exactly this purpose -- "Returns 200
# if valid, 403 if invalid" -- and, unlike every other endpoint here,
# spends no generation credits. That matters: Ludo.ai meters API calls
# against a monthly allowance, so a connection test that generated an
# image to prove the key worked would cost the user money every time
# they opened the page.
#
# Still environment-overridable, because an API version can move and a
# setting is a cheaper fix than a release.
ENV_LUDO_BASE = "ARIA_LUDO_API_BASE"
ENV_LUDO_PATH = "ARIA_LUDO_API_PATH"
LUDO_API_BASE = "https://api.ludo.ai/api"
LUDO_PROBE_PATH = "/auth/validate-api-key"

# HOW THE KEY IS SENT, AND WHY IT IS NOT WHAT THE DOCS PAGE SAYS
#
# Ludo.ai's own integration page states:
#     Authentication: ApiKey YOUR_API_KEY
# That is wrong for the REST API. The OpenAPI spec names the header
# "Authorization", and the server agrees. Measured against the live
# endpoint with a dummy key:
#
#     no header                        -> 403 "API Key is required"
#     Authorization: ApiKey <dummy>    -> 403 "Error: Unauthorized"
#     Authentication: ApiKey <dummy>   -> 403 "API Key is required"
#
# The third case is the tell: with "Authentication" the server never
# saw a key at all. So "Authorization" it is. (The docs page may still
# be right about MCP, which is a different server at mcp.ludo.ai and
# not something this module talks to.)
#
# The scheme is "ApiKey", not "Bearer" -- also from the spec, and also
# something an earlier version of this file got wrong.
LUDO_AUTH_HEADER = "Authorization"
LUDO_AUTH_SCHEME = "ApiKey"

# What the server says when it did not receive a key at all. If ARIA
# ever sees this, the fault is on this side, and the message says so
# rather than blaming the user's key.
_LUDO_NO_KEY = "API Key is required"

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
    plugins = [plugin for plugin in load_plugins().values()
               if not plugin.get("dismissed", False)
               # A command lives in the same file but is not a plugin,
               # and would otherwise render as a card on the Plugins
               # page with no logo and no config page.
               and not plugin.get("type")]
    ordered = sorted(plugins,
                     key=lambda plugin: str(plugin.get("name") or plugin.get("id") or ""))
    return [redact_secrets(plugin) for plugin in ordered]


def list_commands(plugin: str = "") -> list:
    """Every CLI command in the registry, ordered by name.

    The other door out of the same file. Pass a plugin id to get only
    that plugin's commands; today only unity_cli registers any, and the
    argument is what stops the next one needing a second function.
    """
    wanted = str(plugin or "")
    commands = [record for record in load_plugins().values()
                if record.get("type") == COMMAND_TYPE
                and not record.get("dismissed", False)
                and (not wanted or record.get("plugin") == wanted)]

    return sorted(commands,
                  key=lambda record: str(record.get("name") or record.get("id") or ""))


# ======================================================
# Validation
# ======================================================

def _rules_for(plugin_id: str) -> dict:
    """Which fields this plugin has, and how each is checked.

    The three built-in integrations have their rules written above. A
    discovered plugin's id is not knowable in advance -- it is whatever
    was found on the machine -- so it gets the one field discovery
    produces, and only when the stored record actually has it. Handing
    every plugin an executable_path it does not own would let a form
    save a field that nothing reads.
    """
    rules = dict(_FIELD_RULES.get(str(plugin_id or ""), {}))

    try:
        stored = load_plugins().get(str(plugin_id or "")) or {}
    except Exception:  # pragma: no cover - a read fault is not a rule
        stored = {}

    # A command's fields belong to every command rather than to one id,
    # so they come from what the record IS, not from what it is called.
    if stored.get("type") == COMMAND_TYPE:
        rules.update(_COMMAND_FIELD_RULES)

    if DISCOVERED_FIELD in stored:
        rules.setdefault(DISCOVERED_FIELD, "executable")

    return rules


def validate_plugin(plugin_id: str, fields: dict) -> list:
    """Everything wrong with these field values, as sentences.

    Returns a list so a form can show every problem at once rather than
    making the user fix them one save at a time. An empty list means it
    is valid.
    """
    problems = []
    rules = _rules_for(plugin_id)

    for name, value in (fields or {}).items():
        if name in _COMMON_FIELDS:
            if name in _FLAG_FIELDS and not isinstance(value, bool):
                problems.append(f"{name} must be true or false")
            continue

        kind = rules.get(name)
        if kind is None:
            problems.append(f"{plugin_id} has no field called {name!r}")
            continue

        if kind == "locked":
            # Present so the field is known rather than rejected as
            # unknown, and refused so a form cannot retype a command as
            # something else or reassign it to another plugin.
            problems.append(f"{name} cannot be changed")
            continue

        if kind == "arguments":
            # A list of strings, and nothing cleverer. These become
            # separate argv entries, which is what makes shell quoting
            # something this codebase never has to get right.
            if not isinstance(value, list):
                problems.append(f"{name} must be a list of arguments")
            elif any(not isinstance(entry, str) for entry in value):
                problems.append(f"every entry in {name} must be text")
            elif any("\n" in entry or "\r" in entry for entry in value):
                problems.append(f"{name} cannot contain line breaks")
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
            elif not os.access(stripped, os.X_OK):
                # On Windows this passes for any readable file, which is
                # the correct answer there -- Windows decides by
                # extension, not by a permission bit. On POSIX it is the
                # difference between a program and a text file somebody
                # pasted the path of.
                problems.append(f"{name}: {stripped} is not executable")
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
        elif kind == "text":
            # A command's label and template. One line each: these are
            # shown on a tile and split into argv, and a newline in
            # either is a mistake rather than a value.
            if "\n" in stripped or "\r" in stripped:
                problems.append(f"{name} must be a single line")

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
    #
    # Nor are the discovery flags. "discovered" records how a plugin got
    # here, which only a scan knows, and "dismissed" is a tombstone only
    # remove_plugin writes -- a form that could set it would hide a
    # plugin from the page with no way back to it.
    for locked in ("id", "configPage", "discovered", "dismissed"):
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
    """Turn a plugin on.

    "discovered" is left alone, deliberately. It records HOW this
    plugin got here -- ARIA found it by scanning -- and that does not
    stop being true when the user switches it on. An earlier draft
    cleared the flag here, to stop a card showing a "Discovered" badge
    and an enabled dot at once; the cost was that removing an adopted
    plugin no longer left a tombstone, so the next scan re-offered
    something the user had just thrown away.

    The badge is a question for the page, which shows it only while a
    discovered plugin is still off. Provenance belongs in the record.
    """
    return update_plugin(plugin_id, {"enabled": True})


def disable_plugin(plugin_id: str) -> dict:
    """Turn a plugin off. Its settings are kept."""
    return update_plugin(plugin_id, {"enabled": False})


def remove_plugin(plugin_id: str) -> bool:
    """Uninstall a plugin, settings and all.

    Returns whether anything was removed. Removing something that is
    already gone is not an error -- two clicks on the same button should
    not produce a failure the second time.

    A DISCOVERED PLUGIN LEAVES A TOMBSTONE
    --------------------------------------
    Deleting the record of a plugin that was found by scanning the disk
    does not remove it from the disk, so the next scan finds it again
    and it reappears -- which is precisely the "keeps coming back after
    you removed it" behaviour the Plugins page was rewritten to stop.

    So removing a discovered plugin leaves an entry marked dismissed:
    hidden from the page, and known to the merge so it is not offered
    again. An explicit rescan clears these -- pressing a button that
    says "look again" is a different instruction from opening a page.
    """
    plugins = load_plugins()
    plugin = plugins.get(str(plugin_id or ""))
    if plugin is None:
        return False

    if plugin.get("discovered", False):
        plugins[plugin_id] = {
            "id": plugin_id,
            "name": plugin.get("name", plugin_id),
            "enabled": False,
            "discovered": True,
            "dismissed": True,
        }
        save_plugins(plugins)
        logger.info("dismissed discovered plugin %s", plugin_id)
        return True

    plugins.pop(plugin_id)
    save_plugins(plugins)
    logger.info("removed plugin %s", plugin_id)
    return True


# ======================================================
# Discovery
#
# plugin_discovery finds things; this decides what happens to them.
# The split matters: the scanner reads the disk and returns facts, and
# every judgement about the registry -- what is new, what is a name
# collision, what the user already threw away -- is made here, where
# the registry is.
# ======================================================

def validate_registry(plugins: dict) -> list:
    """Everything structurally wrong with a whole registry.

    Ids are unique for free, being dictionary keys, so this checks that
    the keys agree with the records and that no two plugins share a
    name. Two plugins called "Unity Integration" is a page with two
    identical cards and a user who cannot tell which one they are
    configuring.
    """
    problems = []
    names = {}

    for key, plugin in (plugins or {}).items():
        if not isinstance(plugin, dict):
            problems.append(f"{key} is not a plugin record")
            continue

        stored_id = str(plugin.get("id") or "")
        if stored_id and stored_id != key:
            problems.append(f"{key} holds a plugin whose id is {stored_id!r}")

        if plugin.get("dismissed", False):
            # A tombstone is not a plugin and does not compete for a name.
            continue

        if plugin.get("type"):
            # Nor is a command. The unique-name rule exists so a user
            # cannot end up with two identical plugin cards; commands
            # are keyed by id and shown in their own section, and a
            # command called "build" beside a plugin called "build"
            # confuses nobody.
            continue

        name = str(plugin.get("name") or "").strip().casefold()
        if not name:
            continue
        if name in names:
            problems.append(
                f"{key} and {names[name]} are both called {plugin.get('name')!r}")
        else:
            names[name] = key

    return problems


def merge_discovered(findings: list, *, respect_dismissed: bool = True) -> dict:
    """Add what discovery found, without disturbing what is already there.

    Returns what happened, as {"added": [...], "skipped": [{id, reason}]},
    so the caller can say "found 2, added 1, you already have Unity"
    rather than a number that hides the interesting half.

    THREE REASONS TO SKIP, AND NONE OF THEM IS AN ERROR
    ---------------------------------------------------
    Already installed: the record stands, untouched -- settings, on/off
    state and all. This is the rule that stops a rescan wiping the path
    a user typed.

    Dismissed: they removed it. An automatic scan does not argue.

    Name taken: some other id already calls itself that.

    Nothing is written when nothing was added, so opening the Plugins
    page on a settled machine does not rewrite the registry every time.
    """
    plugins = load_plugins()
    added, skipped = [], []

    for finding in findings or []:
        if not isinstance(finding, dict):
            continue

        plugin_id = str(finding.get("id") or "").strip()
        if not plugin_id:
            skipped.append({"id": "", "reason": "it has no id"})
            continue

        existing = plugins.get(plugin_id)
        if existing is not None:
            if existing.get("dismissed", False):
                if respect_dismissed:
                    skipped.append({"id": plugin_id, "reason": "you removed this"})
                    continue
                # An explicit rescan: the tombstone goes and the finding
                # is treated as new.
                plugins.pop(plugin_id)
            else:
                skipped.append({"id": plugin_id, "reason": "already installed"})
                continue

        name = str(finding.get("name") or plugin_id)
        taken = {str(other.get("name") or "").strip().casefold()
                 for key, other in plugins.items()
                 if key != plugin_id and not other.get("dismissed", False)}
        if name.strip().casefold() in taken:
            skipped.append({"id": plugin_id,
                            "reason": f"another plugin is already called {name!r}"})
            continue

        record = {
            "id": plugin_id,
            "name": name,
            "version": str(finding.get("version") or "0.0.0"),
            "logo": str(finding.get("logo") or ""),
            "configPage": str(finding.get("configPage") or f"{plugin_id}-config"),
            # Found, never adopted. Discovery does not switch things on:
            # a scan is not consent, and an integration that started
            # itself because a folder existed would be a surprise.
            "enabled": False,
            "discovered": True,
        }

        # Discovery reports its settings as a dict; the registry stores
        # them flat, the way every hand-written entry already is.
        for name_, value in (finding.get("settings") or {}).items():
            record[str(name_)] = value

        # What a command entry carries beyond a plugin's fields. Copied
        # by name rather than wholesale, so a finding cannot smuggle in
        # a key the validator has never heard of.
        for extra in _DISCOVERY_EXTRAS:
            if extra in finding:
                record[extra] = finding[extra]

        # executable_path is discovery's generic "where the program is"
        # field, and it is only added when the finding has no field of
        # its own. Unity CLI stores unity_cli_path and a command stores
        # no path at all; giving either an empty executable_path would
        # put a box on their pages that nothing reads.
        if not (finding.get("settings") or record.get("type")):
            record.setdefault(DISCOVERED_FIELD,
                              str(finding.get(DISCOVERED_FIELD) or ""))

        plugins[plugin_id] = record
        added.append(redact_secrets(record))

    if added:
        save_plugins(plugins)
        logger.info("discovery added %d plugin(s): %s",
                    len(added), ", ".join(plugin["id"] for plugin in added))
    else:
        logger.debug("discovery added nothing (%d skipped)", len(skipped))

    return {"added": added, "skipped": skipped}


def refresh_unity_cli_commands(*, force: bool = False) -> dict:
    """Ask the Unity CLI what it can do, and record the answer.

    Merged the same way plugins are, through the same function, so
    commands inherit the same three promises for free: an existing
    entry is never overwritten, a removed one is not resurrected by an
    automatic pass, and nothing is written when nothing changed.

    A user who edited a command's arguments and then pressed Rescan
    keeps their edit -- that is merge_discovered's "already installed"
    rule doing its job on a record it was not written for.
    """
    from backend.unity import unity_cli_engine as engine

    plugin = load_plugins().get(engine.PLUGIN_ID) or {}
    if not plugin:
        return {"success": False, "commands": [], "added": [], "skipped": [],
                "error": "The Unity CLI plugin is not installed."}
    if not plugin.get("enabled", False):
        # Listing commands means running the CLI. A disabled plugin is
        # one the user has not adopted, and adopting it is the consent.
        return {"success": False, "commands": list_commands(engine.PLUGIN_ID),
                "added": [], "skipped": [],
                "error": "Enable the Unity CLI plugin before listing its commands."}

    try:
        outcome = engine.discover_commands()
    except Exception as error:  # pragma: no cover - a subprocess fault
        logger.exception("could not list Unity CLI commands")
        return {"success": False, "commands": list_commands(engine.PLUGIN_ID),
                "added": [], "skipped": [], "error": str(error)}

    merged = merge_discovered(outcome["found"], respect_dismissed=not force)

    return {
        "success": outcome["success"],
        "commands": list_commands(engine.PLUGIN_ID),
        "added": merged["added"],
        "skipped": merged["skipped"],
        "output": outcome.get("output", ""),
        "error": outcome.get("error"),
    }


def discover_plugins(*, force: bool = False) -> dict:
    """Scan, merge, and report what is now on the Plugins page.

    force=True is the "Rescan for Plugins" button: it clears the
    tombstones left by removing a discovered plugin, because pressing a
    button that says look again is an instruction to look again. The
    automatic pass that runs when the page opens does not.

    Never raises. Discovery is a convenience -- a scan that fails
    because one directory could not be read should leave the page
    showing the plugins the user already has, not an error.
    """
    try:
        from . import plugin_discovery

        findings = plugin_discovery.discover()
    except Exception as error:  # pragma: no cover - a scan fault
        logger.exception("plugin discovery could not run")
        return {"discovered": list_plugins(), "found": [], "added": [],
                "skipped": [], "error": str(error)}

    outcome = merge_discovered(findings, respect_dismissed=not force)

    return {
        # Everything the page should now show, so it does not need a
        # second round trip to find out.
        "discovered": list_plugins(),
        "found": findings,
        "added": outcome["added"],
        "skipped": outcome["skipped"],
    }


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
    if plugin_id == "unity_cli":
        return _test_unity_cli(plugin)

    # Anything discovery found is a program with a path, so the same
    # check the Blender button runs works for all of them. Without this
    # every discovered plugin would offer a Test button that answered
    # "no test available" -- a control that exists only to decline.
    if DISCOVERED_FIELD in plugin:
        return _test_executable(plugin, DISCOVERED_FIELD,
                                str(plugin.get("name") or plugin_id))

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


def _test_unity_cli(plugin: dict) -> dict:
    """Run the Unity CLI and see whether it answers.

    This is the one place the CLI's version is detected, and it saves
    it -- so the card stops saying v0.0.0 once somebody has confirmed
    the tool works. Discovery deliberately does not do this; see
    plugin_discovery.discover_unity_cli.

    The version is saved directly rather than through update_plugin,
    because "version" is identity that a form may not edit and this is
    not a form: it is ARIA recording what the program said about
    itself.
    """
    from backend.unity import unity_cli_engine as engine

    outcome = engine.test_cli()
    if not outcome.get("ok"):
        return outcome

    try:
        # The version test_cli already learned. Asking the engine again
        # would start the program a second time for one string.
        version = str(outcome.get("version") or "0.0.0")
        if version != "0.0.0" and str(plugin.get("version") or "") != version:
            plugins = load_plugins()
            record = plugins.get(engine.PLUGIN_ID)
            if record is not None:
                record["version"] = version
                save_plugins(plugins)
                logger.info("recorded Unity CLI version %s", version)
    except Exception:  # pragma: no cover - a version is not worth failing over
        logger.exception("could not record the Unity CLI version")

    return outcome


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

    It calls /auth/validate-api-key, which exists to answer this
    question and costs no credits. Ludo.ai meters API calls against a
    monthly allowance, so a test that proved the key worked by
    generating something would bill the user for pressing a button.

    A 403 has two meanings and they are told apart, because they point
    at different people. "Unauthorized" means Ludo.ai read the key and
    rejected it -- the user's problem. "API Key is required" means the
    request arrived without a key at all, which can only be ARIA's
    fault, and saying "your key was rejected" there would send someone
    off to regenerate a key that was fine.
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
        answer = http_fetch(
            url,
            headers={LUDO_AUTH_HEADER: f"{LUDO_AUTH_SCHEME} {key}"},
            timeout=LUDO_TIMEOUT_SECONDS)
    except Exception as error:
        logger.exception("the Ludo.ai connection test could not run")
        return {"ok": False, "message": f"The test could not run: {error}"}

    if answer.get("status") == "ok":
        return {"ok": True, "message": "Ludo.ai accepted that key."}

    code = answer.get("code")
    body = str(answer.get("error") or "")

    if _LUDO_NO_KEY.lower() in body.lower():
        # The key never reached Ludo.ai. That is this end's fault, and
        # telling the user their key was rejected would send them to
        # regenerate a key that was fine.
        return {"ok": False,
                "message": ("Ludo.ai says no key was sent, which is ARIA's "
                            "fault rather than the key's. The request went to "
                            f"{url}.")}
    if code in (401, 403):
        return {"ok": False, "message": "Ludo.ai rejected that key."}
    if code == 404:
        return {"ok": False,
                "message": (f"Reached Ludo.ai, but it has no {path}. The key was "
                            f"not checked. Ludo.ai's API may have moved -- set "
                            f"{ENV_LUDO_PATH} (and {ENV_LUDO_BASE} if needed).")}
    if code == 429:
        return {"ok": False, "message": "Ludo.ai rate-limited the test. Try again shortly."}
    if code:
        return {"ok": False, "message": f"Ludo.ai answered HTTP {code}."}

    # No code at all means the request never completed -- DNS, TCP, TLS,
    # a proxy. http_fetch caught the exception and put the reason in
    # "error", and an earlier version of this threw that away and said
    # only "Could not reach", which is how a TLS failure and an unplugged
    # cable came to look identical. The reason is the whole diagnosis, so
    # it is shown.
    if body:
        return {"ok": False, "message": f"Could not reach {base}: {body}"}
    return {"ok": False, "message": f"Could not reach {base}."}
