"""Finding integrations that are already installed, and adopting them.

NOTHING HERE LOOKS AT THE REAL MACHINE
--------------------------------------
Every test builds its own fake Program Files and points the scanner at
it. A test that asserted "discovery finds Unity" against whatever is
installed on the machine running it would pass on the author's laptop,
fail in CI, and tell nobody anything either way. What is worth testing
is that a Unity-shaped tree is recognised as Unity -- and that is a
question about the code, which is answerable.

(For the record: the machine this was written on has none of the four
installed under Program Files, so the real scan correctly returns
nothing. That is the right answer, and it is also why it cannot be the
test.)
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from backend import ipc_router, ipc_schema as schema
from backend.plugins import plugin_discovery, plugin_settings


# ======================================================
# Fixtures
# ======================================================

def _make(root: Path, relative: str) -> Path:
    """Create a file, and every directory above it."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("binary", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A Program Files with one of each of the four families in it."""
    root = tmp_path / "Program Files"

    _make(root, "Unity/Hub/Editor/2021.3.5f1/Editor/Unity.exe")
    _make(root, "Unity/Hub/Editor/2022.3.10f1/Editor/Unity.exe")
    _make(root, "Blender Foundation/Blender 4.1/blender.exe")
    _make(root, "Epic Games/UE_5.3/Engine/Binaries/Win64/UnrealEditor.exe")
    _make(root, "Godot/Godot_v4.2/Godot_v4.2-stable_win64.exe")

    monkeypatch.setenv(plugin_discovery.ENV_PROGRAM_ROOTS, str(root))
    # Somewhere with no plugins/ folder, so these tests see installed
    # programs only and the folder tests below see folders only.
    monkeypatch.setenv(plugin_discovery.ENV_PROJECT_ROOT, str(tmp_path / "elsewhere"))
    return root


@pytest.fixture
def nothing_installed(tmp_path, monkeypatch):
    monkeypatch.setenv(plugin_discovery.ENV_PROGRAM_ROOTS, str(tmp_path / "empty"))
    monkeypatch.setenv(plugin_discovery.ENV_PROJECT_ROOT, str(tmp_path / "empty"))
    return tmp_path


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A registry of this session's own, holding the shipped three."""
    path = tmp_path / "plugins.json"
    path.write_text(json.dumps({
        "unity": {"id": "unity", "name": "Unity Integration", "version": "1.0.0",
                  "enabled": True, "logo": "assets/plugin_logos/unity.png",
                  "configPage": "unity-config",
                  "unity_path": "", "project_path": ""},
        "ludo": {"id": "ludo", "name": "Ludo.ai Integration", "version": "1.0.0",
                 "enabled": True, "logo": "assets/plugin_logos/ludo.png",
                 "configPage": "ludo-config", "api_key": "", "model": ""},
    }, indent=2), encoding="utf-8")

    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(path))
    return path


def _by_id(findings):
    return {finding["id"]: finding for finding in findings}


# ======================================================
# PART 1 - the scan
# ======================================================

def test_the_scan_finds_all_four_families(installed):
    found = _by_id(plugin_discovery.discover())

    assert set(found) == {"unity", "blender", "unreal", "godot"}


def test_it_finds_the_executable_not_just_the_folder(installed):
    found = _by_id(plugin_discovery.discover())

    for plugin_id in ("unity", "blender", "unreal", "godot"):
        executable = Path(found[plugin_id]["executable_path"])
        assert executable.is_file(), f"{plugin_id} pointed at something that is not a file"


def test_the_version_comes_from_the_install_folder(installed):
    found = _by_id(plugin_discovery.discover())

    assert found["unity"]["version"] == "2022.3.10f1"
    assert found["blender"]["version"] == "4.1"
    assert found["unreal"]["version"] == "5.3"
    assert found["godot"]["version"] == "4.2"


def test_the_newest_install_wins(installed):
    """Two Unity versions are one Unity integration, and it should be
    the one they most likely want."""
    unity = _by_id(plugin_discovery.discover())["unity"]

    assert unity["installs_found"] == 2
    assert "2022.3.10f1" in unity["executable_path"]
    assert "2021.3.5f1" not in unity["executable_path"]


def test_a_folder_without_the_program_in_it_is_not_a_find(tmp_path, monkeypatch):
    """An uninstall that left the directory behind is not an install."""
    root = tmp_path / "Program Files"
    (root / "Blender Foundation" / "Blender 4.1").mkdir(parents=True)

    monkeypatch.setenv(plugin_discovery.ENV_PROGRAM_ROOTS, str(root))
    monkeypatch.setenv(plugin_discovery.ENV_PROJECT_ROOT, str(tmp_path / "none"))

    assert plugin_discovery.discover() == []


def test_nothing_installed_is_an_empty_answer_not_a_failure(nothing_installed):
    assert plugin_discovery.discover() == []


def test_a_missing_program_files_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv(plugin_discovery.ENV_PROGRAM_ROOTS,
                       str(tmp_path / "no" / "such" / "place"))
    monkeypatch.setenv(plugin_discovery.ENV_PROJECT_ROOT, str(tmp_path / "none"))

    assert plugin_discovery.discover() == []


def test_every_finding_is_marked_discovered(installed):
    for finding in plugin_discovery.discover():
        assert finding["discovered"] is True


def test_a_finding_carries_everything_a_card_needs(installed):
    for finding in plugin_discovery.discover():
        for field in ("id", "name", "version", "logo", "configPage",
                      "settings", "executable_path", "discovered"):
            assert field in finding, f"a finding has no {field}"
        assert isinstance(finding["settings"], dict)


def test_the_scan_never_runs_what_it_finds(installed, monkeypatch):
    """Discovery happens when a page opens. Opening a page must not
    start Unity."""
    import subprocess

    def refuse(*args, **kwargs):
        raise AssertionError("discovery executed something")

    for name in ("run", "Popen", "call", "check_output", "check_call"):
        monkeypatch.setattr(subprocess, name, refuse)
    monkeypatch.setattr(os, "system", refuse)

    assert len(plugin_discovery.discover()) == 4


# ======================================================
# PART 1 - the plugins/ folder scan
# ======================================================

@pytest.fixture(autouse=True)
def no_real_unity_cli(monkeypatch):
    """Discovery must not read the developer's own machine.

    The Unity CLI is NOT found through ENV_PROGRAM_ROOTS -- it is
    found the way command-line tools are, on PATH or beside a Unity
    Hub install (see discover_unity_cli's docstring). So on a machine
    that has one -- this one does, at
    C:/Users/.../AppData/Local/Unity/bin/unity.exe -- it appeared in
    every result here, and whether the assertions passed depended on
    what had run before them and left the environment in the right
    state.

    A test that reads the real machine is not a test of discovery, it
    is a test of the machine. Neutralised for the whole module, since
    the leak reached tests that use no fixture at all.
    """
    monkeypatch.setattr(plugin_discovery, "discover_unity_cli", lambda: [])


@pytest.fixture
def plugin_folders(tmp_path, monkeypatch):
    root = tmp_path / "project"
    for name, manifest in (
        ("alpha", {"name": "Alpha", "version": "2.0.0"}),
        ("beta", {"name": "Beta", "version": "0.3.1"}),
    ):
        folder = root / "plugins" / name
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (folder / "plugin.py").write_text("raise RuntimeError('never imported')\n",
                                          encoding="utf-8")

    monkeypatch.setenv(plugin_discovery.ENV_PROJECT_ROOT, str(root))
    monkeypatch.setenv(plugin_discovery.ENV_PROGRAM_ROOTS, str(tmp_path / "empty"))

    return root


def test_plugin_folders_are_read_from_their_manifests(plugin_folders):
    found = _by_id(plugin_discovery.discover())

    assert set(found) == {"alpha", "beta"}
    assert found["alpha"]["name"] == "Alpha"
    assert found["beta"]["version"] == "0.3.1"


def test_a_plugin_folder_is_never_imported(plugin_folders):
    """Each plugin.py above raises on import. Reaching the assert at all
    is the test: a scan that imported them would not get here.

    This is the difference between this module and
    plugins/dynamic_plugin_discovery.py, which imports by design.
    """
    assert len(plugin_discovery.discover()) == 2


def test_a_broken_manifest_is_skipped_not_fatal(plugin_folders):
    """PART 5: manifest must be valid JSON. One unreadable folder must
    not hide the readable ones."""
    broken = plugin_folders / "plugins" / "broken"
    broken.mkdir()
    (broken / "manifest.json").write_text("{ this is not json", encoding="utf-8")

    found = _by_id(plugin_discovery.discover())

    assert "broken" not in found
    assert set(found) == {"alpha", "beta"}


def test_a_manifest_that_is_a_list_is_skipped(plugin_folders):
    odd = plugin_folders / "plugins" / "odd"
    odd.mkdir()
    (odd / "manifest.json").write_text("[1, 2, 3]", encoding="utf-8")

    assert "odd" not in _by_id(plugin_discovery.discover())


def test_a_folder_without_a_manifest_is_not_a_plugin(plugin_folders):
    (plugin_folders / "plugins" / "notaplugin").mkdir()

    assert "notaplugin" not in _by_id(plugin_discovery.discover())


def test_one_id_found_twice_becomes_one_entry(tmp_path, monkeypatch):
    """unreal is both a plugins/ folder and an installed engine. The
    user wants one card carrying both facts, not two fighting over one
    registry key."""
    project = tmp_path / "project"
    folder = project / "plugins" / "unreal"
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(
        json.dumps({"name": "Unreal", "version": "9.9.9"}), encoding="utf-8")

    programs = tmp_path / "Program Files"
    _make(programs, "Epic Games/UE_5.3/Engine/Binaries/Win64/UnrealEditor.exe")

    monkeypatch.setenv(plugin_discovery.ENV_PROJECT_ROOT, str(project))
    monkeypatch.setenv(plugin_discovery.ENV_PROGRAM_ROOTS, str(programs))

    found = plugin_discovery.discover()

    assert len(found) == 1
    # The folder's own name and version win; the executable is the fact
    # only the install scan had.
    assert found[0]["name"] == "Unreal"
    assert found[0]["version"] == "9.9.9"
    assert found[0]["executable_path"].endswith("UnrealEditor.exe")


# ======================================================
# PART 2 - merging into the registry
# ======================================================

def test_discovered_plugins_reach_the_registry(registry, installed):
    plugin_settings.discover_plugins()

    stored = plugin_settings.load_plugins()
    assert "godot" in stored
    assert "unreal" in stored


def test_discovered_plugins_arrive_off_and_marked(registry, installed):
    plugin_settings.discover_plugins()

    godot = plugin_settings.get_plugin("godot")
    assert godot["enabled"] is False, "a scan is not consent"
    assert godot["discovered"] is True


def test_discovery_does_not_touch_a_plugin_that_already_exists(registry, installed):
    """The rule that stops a rescan wiping a path the user typed."""
    plugin_settings.update_plugin("unity", {"project_path": ""})
    before = plugin_settings.get_plugin("unity")

    plugin_settings.discover_plugins()

    assert plugin_settings.get_plugin("unity") == before
    assert plugin_settings.get_plugin("unity")["enabled"] is True


def test_an_existing_plugin_is_reported_as_skipped(registry, installed):
    outcome = plugin_settings.discover_plugins()

    skipped = {entry["id"]: entry["reason"] for entry in outcome["skipped"]}
    assert skipped["unity"] == "already installed"


def test_a_second_scan_adds_nothing(registry, installed):
    first = plugin_settings.discover_plugins()
    second = plugin_settings.discover_plugins()

    assert first["added"], "the first scan should have found something"
    assert second["added"] == []


def test_a_settled_scan_does_not_rewrite_the_file(registry, installed):
    """Opening the Plugins page should not write to disk every time."""
    plugin_settings.discover_plugins()
    before = registry.read_text(encoding="utf-8")
    stamp = registry.stat().st_mtime_ns

    plugin_settings.discover_plugins()

    assert registry.read_text(encoding="utf-8") == before
    assert registry.stat().st_mtime_ns == stamp


def test_a_name_already_taken_is_refused(registry, installed):
    """PART 5: plugin name must be unique."""
    plugins = plugin_settings.load_plugins()
    plugins["mything"] = {"id": "mything", "name": "Godot Integration",
                          "enabled": True, "version": "1.0.0"}
    plugin_settings.save_plugins(plugins)

    outcome = plugin_settings.discover_plugins()

    reasons = {entry["id"]: entry["reason"] for entry in outcome["skipped"]}
    assert "godot" in reasons
    assert "already called" in reasons["godot"]
    assert "godot" not in plugin_settings.load_plugins()


def test_the_executable_path_is_stored_where_the_form_reads_it(registry, installed):
    plugin_settings.discover_plugins()

    godot = plugin_settings.get_plugin("godot")
    assert Path(godot[plugin_settings.DISCOVERED_FIELD]).is_file()


def test_the_registry_is_saved_atomically(registry, installed, monkeypatch):
    """An interrupted save must leave the previous registry, not half of
    the new one. Nothing may write to the real path except the final
    move."""
    before = registry.read_text(encoding="utf-8")

    def explode(source, destination):
        # Fail at the moment of the move: whatever was written to the
        # temporary file must not have reached the registry.
        raise OSError("the disk went away")

    monkeypatch.setattr(plugin_settings.shutil, "move", explode)

    with pytest.raises(OSError):
        plugin_settings.discover_plugins()

    assert registry.read_text(encoding="utf-8") == before


def test_a_failed_save_leaves_no_litter(registry, installed, monkeypatch):
    def explode(source, destination):
        raise OSError("no")

    monkeypatch.setattr(plugin_settings.shutil, "move", explode)

    with pytest.raises(OSError):
        plugin_settings.discover_plugins()

    leftovers = list(registry.parent.glob(".plugins-*.json"))
    assert leftovers == [], f"a failed save left {leftovers}"


def test_a_scan_that_throws_does_not_break_the_page(registry, monkeypatch):
    def explode():
        raise RuntimeError("the scanner fell over")

    monkeypatch.setattr(plugin_discovery, "discover", explode)

    outcome = plugin_settings.discover_plugins()

    assert outcome["error"] == "the scanner fell over"
    # The page still gets the plugins the user already has.
    assert {plugin["id"] for plugin in outcome["discovered"]} == {"unity", "ludo"}


# ======================================================
# PART 2/5 - registry validation
# ======================================================

def test_two_plugins_with_one_name_is_a_problem():
    problems = plugin_settings.validate_registry({
        "a": {"id": "a", "name": "Same Name"},
        "b": {"id": "b", "name": "same name"},
    })

    assert len(problems) == 1
    # Case-insensitively the same name, which is what matters: two cards
    # a user cannot tell apart.
    assert "a" in problems[0] and "b" in problems[0]


def test_a_record_filed_under_the_wrong_key_is_a_problem():
    problems = plugin_settings.validate_registry({
        "unity": {"id": "blender", "name": "Blender"},
    })

    assert any("blender" in problem for problem in problems)


def test_the_shipped_registry_is_valid():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    shipped = json.loads((root / "aria_config" / "plugins.json").read_text("utf-8"))

    assert plugin_settings.validate_registry(shipped) == []


def test_a_tombstone_does_not_compete_for_a_name():
    problems = plugin_settings.validate_registry({
        "godot": {"id": "godot", "name": "Godot Integration", "dismissed": True},
        "other": {"id": "other", "name": "Godot Integration"},
    })

    assert problems == []


# ======================================================
# PART 5 - validating a discovered plugin's field
# ======================================================

def test_a_discovered_plugin_can_be_configured(registry, installed, tmp_path):
    """PART 8: every discovered plugin must be configurable."""
    plugin_settings.discover_plugins()

    replacement = _make(tmp_path, "elsewhere/Godot.exe")
    updated = plugin_settings.update_plugin(
        "godot", {plugin_settings.DISCOVERED_FIELD: str(replacement)})

    assert updated[plugin_settings.DISCOVERED_FIELD] == str(replacement)


def test_an_executable_that_is_not_there_is_refused(registry, installed, tmp_path):
    """PART 5: executable_path must exist."""
    plugin_settings.discover_plugins()

    problems = plugin_settings.validate_plugin(
        "godot", {plugin_settings.DISCOVERED_FIELD: str(tmp_path / "gone.exe")})

    assert any("nothing exists" in problem for problem in problems)


def test_a_folder_is_not_an_executable(registry, installed, tmp_path):
    plugin_settings.discover_plugins()

    problems = plugin_settings.validate_plugin(
        "godot", {plugin_settings.DISCOVERED_FIELD: str(tmp_path)})

    assert any("not a folder" in problem for problem in problems)


@pytest.mark.skipif(os.name == "nt",
                    reason="Windows decides by extension, not a permission bit")
def test_a_file_that_is_not_executable_is_refused(registry, installed, tmp_path):
    """PART 5: executable_path must be executable."""
    plugin_settings.discover_plugins()

    text = tmp_path / "notes.txt"
    text.write_text("this is not a program", encoding="utf-8")
    text.chmod(0o644)

    problems = plugin_settings.validate_plugin(
        "godot", {plugin_settings.DISCOVERED_FIELD: str(text)})

    assert any("not executable" in problem for problem in problems)


def test_a_plugin_without_that_field_does_not_gain_one(registry):
    """Only a plugin that actually has executable_path may be sent one.
    A form that could save a field nothing reads is a form that lies."""
    problems = plugin_settings.validate_plugin(
        "ludo", {plugin_settings.DISCOVERED_FIELD: "C:/anything.exe"})

    assert any("no field called" in problem for problem in problems)


def test_flags_must_be_booleans(registry, installed):
    plugin_settings.discover_plugins()

    assert plugin_settings.validate_plugin("godot", {"discovered": "yes"})
    assert plugin_settings.validate_plugin("godot", {"enabled": 1})
    assert plugin_settings.validate_plugin("godot", {"enabled": True}) == []


# ======================================================
# PART 3/4 - adopting and removing
# ======================================================

def test_enabling_a_discovered_plugin_works(registry, installed):
    plugin_settings.discover_plugins()

    plugin_settings.enable_plugin("godot")

    assert plugin_settings.get_plugin("godot")["enabled"] is True


def test_enabling_keeps_the_record_of_how_it_got_here(registry, installed):
    """discovered says HOW this plugin arrived, and that stays true.

    An earlier draft cleared it on enable so the card would not show a
    badge and an enabled dot at once. The cost was that removing an
    adopted plugin left no tombstone and the next scan re-offered it --
    so the flag stays and the page decides when to show the badge.
    """
    plugin_settings.discover_plugins()
    plugin_settings.enable_plugin("godot")

    assert plugin_settings.get_plugin("godot")["discovered"] is True


def test_removing_a_discovered_plugin_takes_it_off_the_page(registry, installed):
    plugin_settings.discover_plugins()

    assert plugin_settings.remove_plugin("godot") is True

    listed = {plugin["id"] for plugin in plugin_settings.list_plugins()}
    assert "godot" not in listed


def test_a_removed_plugin_does_not_come_back_by_itself(registry, installed):
    """The whole reason a tombstone exists. Removing a plugin found by
    scanning the disk does not remove it from the disk, so without this
    it reappears the moment the page is opened again."""
    plugin_settings.discover_plugins()
    plugin_settings.remove_plugin("godot")

    plugin_settings.discover_plugins()

    listed = {plugin["id"] for plugin in plugin_settings.list_plugins()}
    assert "godot" not in listed


def test_a_removed_plugin_stays_gone_after_being_enabled_first(registry, installed):
    plugin_settings.discover_plugins()
    plugin_settings.enable_plugin("godot")
    plugin_settings.remove_plugin("godot")

    plugin_settings.discover_plugins()

    listed = {plugin["id"] for plugin in plugin_settings.list_plugins()}
    assert "godot" not in listed


def test_an_explicit_rescan_reconsiders_it(registry, installed):
    """Pressing a button that says "look again" is a different
    instruction from opening a page."""
    plugin_settings.discover_plugins()
    plugin_settings.remove_plugin("godot")

    plugin_settings.discover_plugins(force=True)

    godot = plugin_settings.get_plugin("godot")
    assert godot["enabled"] is False
    assert godot.get("dismissed", False) is False


def test_removing_an_ordinary_plugin_still_deletes_it(registry):
    """No regression: a plugin nobody discovered is removed outright,
    with no tombstone to make it look installed."""
    assert plugin_settings.remove_plugin("ludo") is True

    assert "ludo" not in plugin_settings.load_plugins()


def test_a_tombstone_is_not_offered_as_a_plugin(registry, installed):
    plugin_settings.discover_plugins()
    plugin_settings.remove_plugin("godot")

    for plugin in plugin_settings.list_plugins():
        assert plugin["id"] != "godot"


# ======================================================
# PART 6 - the IPC surface
# ======================================================

def test_the_page_can_ask_for_a_scan(registry, installed):
    packet = ipc_router.dispatch({"type": schema.PLUGIN_DISCOVERY_REQUEST,
                                  "payload": {}})

    assert packet["type"] == schema.PLUGIN_DISCOVERY_RESULT
    payload = packet["payload"]
    assert {plugin["id"] for plugin in payload["added"]} == {"blender", "godot", "unreal"}
    # The whole list comes back too, so the page needs no second trip.
    assert {plugin["id"] for plugin in payload["discovered"]} >= {"unity", "ludo", "godot"}


def test_the_rescan_button_sends_force(registry, installed):
    ipc_router.dispatch({"type": schema.PLUGIN_DISCOVERY_REQUEST, "payload": {}})
    plugin_settings.remove_plugin("godot")

    quiet = ipc_router.dispatch({"type": schema.PLUGIN_DISCOVERY_REQUEST,
                                 "payload": {}})
    assert {p["id"] for p in quiet["payload"]["discovered"]}.isdisjoint({"godot"})

    loud = ipc_router.dispatch({"type": schema.PLUGIN_DISCOVERY_REQUEST,
                                "payload": {"force": True}})
    assert "godot" in {plugin["id"] for plugin in loud["payload"]["discovered"]}


def test_a_scan_never_returns_an_api_key(registry, installed):
    plugin_settings.update_plugin("ludo", {"api_key": "sk-not-for-the-wire"})

    packet = ipc_router.dispatch({"type": schema.PLUGIN_DISCOVERY_REQUEST,
                                  "payload": {}})

    assert "sk-not-for-the-wire" not in json.dumps(packet)


def test_the_discovery_handler_is_registered_once():
    handlers = [name for name in dir(ipc_router) if name == "_handle_plugin_discovery"]

    assert handlers == ["_handle_plugin_discovery"]
    assert schema.PLUGIN_DISCOVERY_REQUEST in ipc_router._HANDLERS


def test_discovered_plugins_appear_in_the_list_packet(registry, installed):
    ipc_router.dispatch({"type": schema.PLUGIN_DISCOVERY_REQUEST, "payload": {}})

    packet = ipc_router.dispatch({"type": schema.PLUGIN_REGISTRY_LIST_REQUEST,
                                  "payload": {}})

    listed = {plugin["id"]: plugin for plugin in packet["payload"]["plugins"]}
    assert listed["godot"]["discovered"] is True
    assert listed["godot"]["enabled"] is False


def test_a_discovered_plugin_can_be_opened_and_saved_over_ipc(registry, installed):
    """PART 8, end to end: found, opened, configured, enabled."""
    ipc_router.dispatch({"type": schema.PLUGIN_DISCOVERY_REQUEST, "payload": {}})

    opened = ipc_router.dispatch({"type": schema.PLUGIN_GET_REQUEST,
                                  "payload": {"id": "godot"}})
    assert opened["type"] == schema.PLUGIN_GET_RESULT
    plugin = opened["payload"]["plugin"]
    assert plugin["discovered"] is True
    assert Path(plugin[plugin_settings.DISCOVERED_FIELD]).is_file()

    saved = ipc_router.dispatch({
        "type": schema.PLUGIN_UPDATE_REQUEST,
        "payload": {"id": "godot", "fields": {"enabled": True}},
    })
    assert saved["type"] == schema.PLUGIN_UPDATE_RESULT
    assert saved["payload"]["plugin"]["enabled"] is True


def test_a_discovered_plugin_has_a_working_test_button(registry, installed):
    """A Test button that can only answer "no test available" is a
    control that exists to decline."""
    plugin_settings.discover_plugins()

    outcome = plugin_settings.test_plugin_connection("godot")

    assert outcome["ok"] is True
    assert "Godot" in outcome["message"] or ".exe" in outcome["message"]


def test_the_test_button_notices_the_program_moving(registry, installed):
    plugin_settings.discover_plugins()
    Path(plugin_settings.get_plugin("godot")[plugin_settings.DISCOVERED_FIELD]).unlink()

    outcome = plugin_settings.test_plugin_connection("godot")

    assert outcome["ok"] is False
    assert "Nothing runnable" in outcome["message"]


def test_a_form_cannot_set_the_discovery_flags(registry, installed):
    """Only a scan sets discovered, and only remove_plugin sets
    dismissed. A page that could set the latter would hide a plugin
    with no way back to it."""
    plugin_settings.discover_plugins()

    plugin_settings.update_plugin("godot", {"discovered": False, "dismissed": True})

    godot = plugin_settings.get_plugin("godot")
    assert godot["discovered"] is True
    assert godot.get("dismissed", False) is False
    assert "godot" in {plugin["id"] for plugin in plugin_settings.list_plugins()}
