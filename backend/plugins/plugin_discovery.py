"""ARIA Lite - finding integrations that are already on this machine.

A user who has Unity installed should not have to type the path to
Unity. This module looks in the places these programs actually install
themselves, and in ARIA's own plugins/ folder, and reports what it
found. It changes nothing: merging what it finds into the registry is
plugin_settings.merge_discovered()'s job, and turning any of it on is
the user's.

WHAT THIS IS ALLOWED TO DO
--------------------------
Read-only, and narrowly. It globs a handful of known install roots,
stats the files it finds, and reads manifest.json out of plugins/*.
It never executes a discovered binary -- not to ask its version, not
to check it runs. A discovery pass happens when a page opens, and a
page opening must not start a program.

The scan is bounded by explicit glob patterns rather than a walk, so
it cannot wander off into the rest of the disk however the machine is
laid out.

WHY IT DOES NOT USE plugins/dynamic_plugin_discovery.py
-------------------------------------------------------
That engine already scans plugins/* and is the right thing for what it
does -- but it importlib.import_module()s each plugin's code as part of
discovering it. That is correct for plugin_manager at startup, where
the import is the point and the sandbox is around it. It is wrong here:
this runs every time the Plugins page opens, and opening a page must
not execute plugin code. So this module reads the manifest and stops.

The two are not rivals. That one loads plugins; this one notices them.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "FAMILIES",
    "discover",
    "discover_installed_programs",
    "discover_plugin_folders",
    "program_roots",
    "project_root",
]

# Point the scan somewhere else. Used by the tests, which must never
# depend on what happens to be installed on the machine running them.
# os.pathsep-separated, exactly like PATH.
ENV_PROGRAM_ROOTS = "ARIA_DISCOVERY_PROGRAM_ROOTS"
ENV_PROJECT_ROOT = "ARIA_DISCOVERY_PROJECT_ROOT"

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Where a discovered plugin's logo would be, if one has been drawn. A
# family with no logo file gets "" and the card falls back to a labelled
# square, which is what that fallback is for.
_LOGO_DIR = _REPO_ROOT / "webui" / "assets" / "plugin_logos"
_LOGO_HREF = "assets/plugin_logos/{id}.png"

# A version-looking token inside a folder name: 2022.3.10f1, 4.1, 5.3.
# Unity Hub, Blender, Epic and Godot all put the version in the folder,
# and reading it there costs nothing. Running the program to ask it
# would cost starting the program.
_VERSION = re.compile(r"(\d+(?:\.\d+)+[A-Za-z0-9]*)")


@dataclass(frozen=True)
class Family:
    """One program ARIA knows how to look for.

    install_globs are relative to each program root, and name the
    directory a single installation lives in. executable_globs are
    relative to that directory. Both are globs because these programs
    put their version in the path, and the version is not knowable in
    advance.
    """

    id: str
    name: str
    install_globs: Tuple[str, ...]
    executable_globs: Tuple[str, ...]


# The four the task names. Adding a fifth is one entry here.
#
# Unreal has two editor binaries across the versions people still run
# (UnrealEditor.exe from UE5, UE4Editor.exe before it) and both are
# listed rather than guessing which is installed.
FAMILIES: Tuple[Family, ...] = (
    Family(
        id="unity",
        name="Unity",
        install_globs=("Unity/Hub/Editor/*",),
        executable_globs=("Editor/Unity.exe", "Unity.exe", "Editor/Unity"),
    ),
    Family(
        id="blender",
        name="Blender",
        install_globs=("Blender Foundation/*",),
        executable_globs=("blender.exe", "blender"),
    ),
    Family(
        id="unreal",
        name="Unreal Engine",
        install_globs=("Epic Games/*",),
        executable_globs=(
            "Engine/Binaries/Win64/UnrealEditor.exe",
            "Engine/Binaries/Win64/UE4Editor.exe",
            "Engine/Binaries/Linux/UnrealEditor",
        ),
    ),
    Family(
        id="godot",
        name="Godot",
        install_globs=("Godot/*", "Godot"),
        executable_globs=("Godot*.exe", "godot*.exe", "Godot*", "godot*"),
    ),
)


def project_root() -> Path:
    """Where plugins/ lives. Overridable so tests get their own."""
    configured = os.environ.get(ENV_PROJECT_ROOT)
    return Path(configured) if configured else _REPO_ROOT


def program_roots() -> List[Path]:
    """The directories installed programs are looked for under.

    The task names C:/Program Files, and that is the default -- but it
    is read from the environment first, because a 32-bit install lands
    in Program Files (x86) and plenty of people move Program Files to
    another drive entirely. Hard-coding one literal path would find
    nothing on those machines and report "nothing installed", which is
    a wrong answer rather than an empty one.
    """
    configured = os.environ.get(ENV_PROGRAM_ROOTS)
    if configured:
        return [Path(part) for part in configured.split(os.pathsep) if part.strip()]

    roots: List[Path] = []
    for variable in ("ProgramW6432", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        value = os.environ.get(variable)
        if value:
            roots.append(Path(value))

    if not roots:
        roots.append(Path("C:/Program Files"))

    # Same directory twice would report every install twice.
    unique: List[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def _version_from(text: str) -> str:
    """The version in a folder name, or 0.0.0 when there is not one."""
    found = _VERSION.search(str(text or ""))
    return found.group(1) if found else "0.0.0"


def _version_key(version: str) -> Tuple:
    """Sortable form of a version, so "2022.3.10f1" beats "2021.1.1f1".

    Numeric runs compare as numbers; anything else compares as text.
    Not a full semver implementation, and does not need to be -- it
    only has to order the installs of one program on one machine.
    """
    parts = re.split(r"[._-]", str(version or ""))
    key = []
    for part in parts:
        digits = re.match(r"(\d+)(.*)", part)
        if digits:
            key.append((1, int(digits.group(1)), digits.group(2)))
        else:
            key.append((0, 0, part))
    return tuple(key)


def _logo_for(plugin_id: str) -> str:
    """The logo href, or "" when nobody has drawn one."""
    if (_LOGO_DIR / f"{plugin_id}.png").is_file():
        return _LOGO_HREF.format(id=plugin_id)
    return ""


def _find_executable(install: Path, family: Family) -> Optional[Path]:
    """The program inside one installation directory.

    Globs in the order the family lists them, so the modern binary is
    preferred over the legacy one when both happen to be present.
    """
    for pattern in family.executable_globs:
        for candidate in sorted(install.glob(pattern)):
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                # A path that cannot be stat'd is not a find. Permission
                # denied on one directory must not end the whole scan.
                logger.debug("could not stat %s", candidate)
    return None


def discover_installed_programs() -> List[dict]:
    """Every known program found installed on this machine.

    One entry per family, not per installation: the registry is keyed
    by id, and three Unity versions are still one Unity integration.
    The newest is chosen, and how many were seen is reported so the UI
    can say so rather than silently picking.
    """
    roots = program_roots()
    logger.debug("scanning %d program root(s) for %d families",
                 len(roots), len(FAMILIES))

    found: List[dict] = []
    for family in FAMILIES:
        installs: List[Tuple[Path, Path]] = []

        for root in roots:
            for pattern in family.install_globs:
                try:
                    matches = sorted(root.glob(pattern))
                except OSError:
                    logger.debug("could not read %s/%s", root, pattern)
                    continue

                for install in matches:
                    try:
                        if not install.is_dir():
                            continue
                    except OSError:
                        continue

                    executable = _find_executable(install, family)
                    if executable is not None:
                        installs.append((install, executable))

        if not installs:
            continue

        installs.sort(key=lambda pair: _version_key(_version_from(pair[0].name)))
        install, executable = installs[-1]
        version = _version_from(install.name)

        found.append({
            "id": family.id,
            "name": f"{family.name} Integration",
            "version": version,
            "logo": _logo_for(family.id),
            "configPage": f"{family.id}-config",
            "settings": {"executable_path": str(executable)},
            "executable_path": str(executable),
            "install_path": str(install),
            "installs_found": len(installs),
            "source": "installed",
            "discovered": True,
        })
        logger.info("discovered %s %s at %s", family.name, version, executable)

    return found


def discover_plugin_folders() -> List[dict]:
    """Every ARIA plugin folder under plugins/.

    A folder counts when it holds a manifest.json that parses. A
    manifest that does not parse is skipped and logged with the reason
    -- one broken plugin folder must not hide the others.

    Nothing here is imported. See the module docstring.
    """
    folder = project_root() / "plugins"
    if not folder.is_dir():
        logger.debug("no plugins folder at %s", folder)
        return []

    found: List[dict] = []
    for item in sorted(folder.iterdir()):
        try:
            if not item.is_dir() or item.name.startswith((".", "_")):
                continue
        except OSError:
            continue

        manifest_file = item / "manifest.json"
        if not manifest_file.is_file():
            continue

        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except ValueError as error:
            logger.warning("%s has a manifest that is not valid JSON: %s",
                           item.name, error)
            continue
        except OSError as error:
            logger.warning("could not read %s: %s", manifest_file, error)
            continue

        if not isinstance(manifest, dict):
            logger.warning("%s has a manifest that is not an object", item.name)
            continue

        plugin_id = str(manifest.get("id") or item.name).strip().lower()
        if not plugin_id:
            continue

        found.append({
            "id": plugin_id,
            "name": str(manifest.get("name") or item.name),
            "version": str(manifest.get("version") or "0.0.0"),
            "logo": _logo_for(plugin_id),
            "configPage": f"{plugin_id}-config",
            "settings": {},
            "executable_path": "",
            "manifest_path": str(manifest_file),
            "source": "folder",
            "discovered": True,
        })

    return found


def _merge_one(into: dict, extra: dict) -> dict:
    """Fold two findings for the same id into one.

    A folder scan knows the name and version a plugin calls itself; an
    installed-program scan knows where the binary is. When both find
    the same id -- unreal is both a plugins/ folder and an installed
    engine -- the user wants one card carrying both facts, not two
    cards fighting over one registry key.

    Neither side overwrites a value the other already filled in.
    """
    merged = dict(into)
    for key, value in extra.items():
        if key == "settings":
            settings = dict(merged.get("settings") or {})
            for name, setting in (value or {}).items():
                settings.setdefault(name, setting)
            merged["settings"] = settings
            continue
        if not merged.get(key) and value:
            merged[key] = value
    return merged


def discover() -> List[dict]:
    """Everything found, one entry per plugin id.

    This is the whole engine. It reads; it decides nothing. Every entry
    carries discovered=True, and what happens to them next is
    plugin_settings.merge_discovered()'s call.
    """
    by_id: Dict[str, dict] = {}

    for finding in discover_plugin_folders() + discover_installed_programs():
        plugin_id = finding["id"]
        if plugin_id in by_id:
            by_id[plugin_id] = _merge_one(by_id[plugin_id], finding)
        else:
            by_id[plugin_id] = finding

    findings = [by_id[key] for key in sorted(by_id)]
    logger.info("discovery found %d plugin(s): %s",
                len(findings), ", ".join(sorted(by_id)) or "none")
    return findings
