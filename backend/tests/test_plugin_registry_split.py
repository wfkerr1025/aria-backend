"""What ships is one file; what this machine did is another.

THE FAILURE THIS EXISTS FOR
---------------------------
aria_config/plugins.json was both the shipped registry and the live
one. Being live, it collected a developer's absolute paths, his Unity
project's 142 discovered commands, and -- until the change before this
-- his Ludo.ai API key. Being shipped, it was committed, so all of that
was in the file every checkout starts from. It had grown from 3 entries
to 148.

The key was moved into key_manager first. This is the same fix one
layer down: a file cannot be both the default and the state.

WHAT THESE TESTS ARE REALLY CHECKING
------------------------------------
The merge is the easy half. The hard half is that the two files stay
separate under every operation that writes -- and that a plugin removed
from the live file does not come back from the shipped one on the next
page load, which is the bug this design invents if nobody guards it.
"""

from __future__ import annotations

import json

import pytest

from backend.plugins import plugin_settings


def _write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")


@pytest.fixture
def shipped():
    """The registry as it comes out of the repository."""
    path = plugin_settings.defaults_file()
    _write(path, {
        "blender": {"id": "blender", "name": "Blender Integration",
                    "version": "1.0.0", "enabled": True, "logo": "b.png",
                    "configPage": "blender-config", "blender_path": ""},
        "ludo": {"id": "ludo", "name": "Ludo.ai Integration",
                 "version": "1.0.0", "enabled": True, "logo": "l.png",
                 "configPage": "ludo-config", "api_key": "", "model": ""},
    })
    return path


@pytest.fixture
def live():
    return plugin_settings.plugins_file()


# ======================================================
# Reading
# ======================================================

def test_a_fresh_install_reads_what_shipped(shipped, live):
    """No live file yet is the state a fresh install is in."""
    assert not live.exists()

    loaded = plugin_settings.load_plugins()
    assert sorted(loaded) == ["blender", "ludo"]
    assert loaded["blender"]["blender_path"] == ""


def test_reading_creates_nothing(shipped, live):
    """The Plugins page calls this constantly. A read that writes would
    make opening a page a change to disk."""
    plugin_settings.load_plugins()

    assert not live.exists()


def test_this_machine_wins_over_what_shipped(shipped, live):
    _write(live, {"blender": {"id": "blender", "blender_path": "C:/here.exe"}})

    assert plugin_settings.load_plugins()["blender"]["blender_path"] == "C:/here.exe"


def test_a_configured_plugin_still_picks_up_a_renamed_label(shipped, live):
    """Merged field by field, not record by record. Record by record,
    a machine that had ever set a path would be frozen at whatever the
    plugin was called on the day it was configured."""
    _write(live, {"blender": {"id": "blender", "blender_path": "C:/here.exe"}})

    merged = plugin_settings.load_plugins()["blender"]
    assert merged["name"] == "Blender Integration"
    assert merged["logo"] == "b.png"
    assert merged["blender_path"] == "C:/here.exe"


def test_what_only_this_machine_knows_comes_through_whole(shipped, live):
    """Discovered plugins and the 142 Unity CLI commands exist in no
    shipped file anywhere."""
    _write(live, {"unity_cmd_build": {
        "id": "unity_cmd_build", "name": "build", "version": "0.0.0",
        "enabled": False, "logo": "", "configPage": "unity_cmd_build-config",
        "type": plugin_settings.COMMAND_TYPE, "plugin": "unity_cli",
        "label": "Build it", "command": "cmd build", "args": [],
        "group": "built-in", "discovered": True}})

    loaded = plugin_settings.load_plugins()
    assert loaded["unity_cmd_build"]["command"] == "cmd build"
    assert len(plugin_settings.list_commands()) == 1


def test_an_integration_added_upstream_arrives_on_its_own(shipped, live):
    """The reason to merge rather than copy the shipped file once. A
    user who has configured Blender should not have to delete their
    settings to be told a new integration exists."""
    _write(live, {"blender": {"id": "blender", "blender_path": "C:/here.exe"}})

    records = json.loads(shipped.read_text(encoding="utf-8"))
    records["godot"] = {"id": "godot", "name": "Godot", "version": "1.0.0",
                        "enabled": True, "logo": "g.png",
                        "configPage": "godot-config"}
    _write(shipped, records)

    assert "godot" in plugin_settings.load_plugins()


def test_neither_file_being_there_is_not_an_error(live):
    plugin_settings.defaults_file().unlink(missing_ok=True)

    assert plugin_settings.load_plugins() == {}


# ======================================================
# Writing
# ======================================================

def test_a_setting_is_written_to_the_live_file_only(shipped, live):
    before = shipped.read_bytes()
    plugin_settings.update_plugin("blender", {"blender_path": ""})

    assert live.exists()
    assert shipped.read_bytes() == before, "it wrote the shipped registry"


def test_the_live_file_holds_the_setting_that_was_made(shipped, live):
    plugin_settings.update_plugin("ludo", {"model": "blitz"})

    assert json.loads(live.read_text())["ludo"]["model"] == "blitz"


def test_the_shipped_registry_cannot_be_written_even_if_asked(
        shipped, live, monkeypatch):
    """The guard is on the path, not on the caller. Pointing the live
    registry at the shipped one is the one way back to a single file
    holding both, and it is how this started."""
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(shipped))
    before = shipped.read_bytes()

    with pytest.raises(plugin_settings.PluginError) as raised:
        plugin_settings.save_plugins({"blender": {"id": "blender"}})

    assert "refusing" in str(raised.value)
    assert shipped.read_bytes() == before


# ======================================================
# Removing, which is where a merge normally goes wrong
# ======================================================

def test_a_removed_shipped_plugin_does_not_come_back(shipped, live):
    """Delete it from the live file and the shipped file still lists
    it, so the next read merges it straight back and the user watches
    a plugin they removed return on the next page load."""
    assert plugin_settings.remove_plugin("blender") is True

    listed = [p["id"] for p in plugin_settings.list_plugins()]
    assert "blender" not in listed
    assert plugin_settings.load_plugins()["blender"]["dismissed"] is True


def test_a_removed_shipped_plugin_stays_gone_across_a_reload(shipped, live):
    plugin_settings.remove_plugin("blender")
    plugin_settings.update_plugin("ludo", {"model": "blitz"})

    assert "blender" not in [p["id"] for p in plugin_settings.list_plugins()]


def test_removing_a_discovered_plugin_still_tombstones(shipped, live):
    _write(live, {"unreal": {"id": "unreal", "name": "Unreal",
                             "version": "0.0.0", "enabled": False, "logo": "",
                             "configPage": "unreal-config",
                             "discovered": True, "executable_path": ""}})

    assert plugin_settings.remove_plugin("unreal") is True
    assert plugin_settings.load_plugins()["unreal"]["dismissed"] is True


def test_a_plugin_no_shipped_file_knows_about_is_simply_gone(shipped, live):
    """Nothing can merge it back, so there is nothing to tombstone."""
    _write(live, {"handmade": {"id": "handmade", "name": "Handmade",
                               "version": "1.0.0", "enabled": True,
                               "logo": "", "configPage": "handmade-config"}})

    assert plugin_settings.remove_plugin("handmade") is True
    assert "handmade" not in plugin_settings.load_plugins()


def test_removing_something_that_is_not_there_is_not_an_error(shipped, live):
    assert plugin_settings.remove_plugin("nothing-by-that-name") is False


# ======================================================
# The files themselves, as committed
# ======================================================

def test_the_shipped_registry_holds_no_machine_state():
    """The test that would have caught this before it was 148 entries.

    Every configurable value in the shipped file is empty. A path, a
    folder or a discovered command in here is this machine's state in
    the file every other checkout starts from.
    """
    real = json.loads(
        plugin_settings.PLUGINS_FILE.read_text(encoding="utf-8"))
    identity = {"id", "name", "version", "enabled", "logo", "configPage",
                "discovered", "dismissed"}

    for name, entry in real.items():
        assert not name.startswith("unity_cmd_"), (
            f"{name} is a command this machine discovered")
        assert entry.get("type") != plugin_settings.COMMAND_TYPE, name
        for field, value in entry.items():
            if field in identity:
                continue
            assert value == "", (
                f"{name}.{field} is this machine's state, not a default")


def test_the_live_registry_is_not_in_the_repository():
    import subprocess

    root = plugin_settings.PLUGINS_FILE.parent.parent
    result = subprocess.run(
        ["git", "check-ignore", "aria_config/plugins.local.json"],
        capture_output=True, text=True, cwd=str(root))

    assert result.returncode == 0, "this machine's plugin state is not ignored"
