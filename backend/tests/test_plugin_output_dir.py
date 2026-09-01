"""Where a plugin writes what it makes, and who decides.

THE COMPLAINT THIS EXISTS FOR
-----------------------------
Generated assets were going to `aria_output/` beside the source tree,
which is wrong twice over: it is a repository, and it is not anywhere
a person would think to look for a model they asked for. The
developer's words: "I don't think having the output in Aria's Root
Folder is a good idea."

So the folder is a setting, the default is under the user's own
documents, and the resolution is shared by every plugin that produces
files rather than written once per plugin -- two copies of "where do
the files go" drift, and the one nobody updated is the one somebody is
using.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend.blender import blender_actions
from backend.plugins import plugin_settings


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "plugins.json"
    path.write_text(json.dumps({
        "blender": {"id": "blender", "name": "Blender Integration",
                    "version": "1.0.0", "enabled": True, "logo": "",
                    "configPage": "blender-config",
                    "blender_path": ""},
        "ludo": {"id": "ludo", "name": "Ludo.ai", "version": "1.0.0",
                 "enabled": True, "logo": "", "configPage": "ludo-config",
                 "api_key": ""},
    }, indent=2), encoding="utf-8")
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(path))
    for plugin in ("BLENDER", "LUDO"):
        monkeypatch.delenv(f"ARIA_{plugin}_OUTPUT", raising=False)
    return path


# ======================================================
# The default
# ======================================================

@pytest.mark.parametrize("plugin_id,folder", [("blender", "Blender"),
                                              ("ludo", "Ludo")])
def test_the_default_is_under_the_users_own_documents(plugin_id, folder):
    """Not the repository. A person looks for a file they asked a
    program to make in their own documents, not in its source tree."""
    default = plugin_settings.default_output_dir(plugin_id)

    assert default.name == folder
    assert default.parent.name == "ARIA"
    assert str(Path.home()) in str(default)


def test_the_default_is_never_inside_the_repository():
    """The specific thing that was wrong."""
    repo = Path(plugin_settings.__file__).resolve().parents[2]

    for plugin_id in ("blender", "ludo"):
        default = plugin_settings.default_output_dir(plugin_id)
        assert repo not in default.parents
        assert default != repo


def test_each_plugin_gets_its_own_folder():
    """Blender exports and Ludo downloads in one folder is a folder
    nobody can tidy."""
    assert (plugin_settings.default_output_dir("blender")
            != plugin_settings.default_output_dir("ludo"))


# ======================================================
# Choosing one
# ======================================================

def test_a_chosen_folder_is_used(registry, tmp_path, monkeypatch):
    chosen = tmp_path / "My Game Assets" / "Blender"
    plugin_settings.update_plugin("blender", {"output_dir": str(chosen)})

    assert plugin_settings.output_dir("blender") == chosen


def test_a_chosen_folder_is_created(registry, tmp_path):
    """Somebody types where they want their assets to go. Requiring
    them to make the folder first is a setting that looks broken."""
    chosen = tmp_path / "Assets" / "Deep" / "Nested"
    plugin_settings.update_plugin("blender", {"output_dir": str(chosen)})

    assert not chosen.exists()
    assert plugin_settings.output_dir("blender").is_dir()


def test_the_environment_wins(registry, tmp_path, monkeypatch):
    """Same order as every other path in this codebase: a setting
    beats a guess, and the environment beats both so one run can be
    redirected without changing anybody's configuration."""
    saved = tmp_path / "saved"
    override = tmp_path / "override"
    plugin_settings.update_plugin("blender", {"output_dir": str(saved)})
    monkeypatch.setenv("ARIA_BLENDER_OUTPUT", str(override))

    assert plugin_settings.output_dir("blender") == override


def test_each_plugin_has_its_own_environment_variable():
    assert plugin_settings._output_env("blender") == "ARIA_BLENDER_OUTPUT"
    assert plugin_settings._output_env("ludo") == "ARIA_LUDO_OUTPUT"
    # An id with punctuation still yields a legal variable name.
    assert plugin_settings._output_env("unity_cli") == "ARIA_UNITY_CLI_OUTPUT"


def test_nothing_configured_falls_back_to_the_default(registry):
    resolved = plugin_settings.output_dir("blender", create=False)

    assert resolved == plugin_settings.default_output_dir("blender")


# ======================================================
# Refusing a folder that cannot work
# ======================================================

def test_a_folder_that_does_not_exist_yet_is_accepted(registry, tmp_path):
    """The normal case. It gets made on first use."""
    problems = plugin_settings.validate_plugin(
        "blender", {"output_dir": str(tmp_path / "not" / "here" / "yet")})

    assert problems == []


def test_a_relative_path_is_refused(registry):
    problems = plugin_settings.validate_plugin("blender",
                                               {"output_dir": "assets"})

    assert problems
    assert "full path" in problems[0]


def test_a_file_in_the_way_is_refused(registry, tmp_path):
    occupied = tmp_path / "notafolder.txt"
    occupied.write_text("hello", encoding="utf-8")

    problems = plugin_settings.validate_plugin("blender",
                                               {"output_dir": str(occupied)})

    assert problems
    assert "is a file" in problems[0]


@pytest.mark.skipif(os.name != "nt", reason="drive letters are a Windows idea")
def test_a_drive_that_is_not_there_is_refused(registry):
    """"Z:/assets" on a machine with no Z drive is a typo, not a plan."""
    problems = plugin_settings.validate_plugin("blender",
                                               {"output_dir": "Z:/assets"})

    assert problems


def test_an_empty_setting_is_not_an_error(registry):
    """Empty means "not configured", which is what a fresh install is
    and what "use the default" looks like."""
    assert plugin_settings.validate_plugin("blender", {"output_dir": ""}) == []


def test_the_field_is_only_offered_where_it_means_something(registry):
    """A setting on a plugin that writes nothing would be a control
    with nothing behind it."""
    problems = plugin_settings.validate_plugin("unity_cli",
                                               {"output_dir": "D:/anywhere"})

    assert problems
    assert "no field called" in problems[0]


# ======================================================
# Seeing it in the form
# ======================================================

@pytest.mark.parametrize("page,plugin_id", [("blender-config", "blender"),
                                            ("ludo-config", "ludo")])
def test_the_field_appears_before_anyone_has_set_it(registry, page, plugin_id):
    """The config page builds its form from the keys a record has, so
    a field nobody has set yet would simply not appear -- and a
    setting you cannot see is a setting you do not have."""
    record = plugin_settings.get_plugin_by_config_page(page)

    assert "output_dir" in record
    assert record["output_dir"] == ""


def test_filling_in_a_field_never_changes_a_stored_value(registry):
    plugin_settings.update_plugin("blender", {"output_dir": "D:/Chosen"})

    record = plugin_settings.get_plugin("blender")

    assert record["output_dir"] == "D:/Chosen"


def test_nothing_is_written_to_disk_just_by_looking(registry):
    before = registry.read_text(encoding="utf-8")

    plugin_settings.get_plugin("blender")
    plugin_settings.get_plugin_by_config_page("ludo-config")

    assert registry.read_text(encoding="utf-8") == before


def test_a_command_record_is_left_alone(registry):
    """Commands live in the same file and have their own fields. One
    must not grow an output folder."""
    record = {"id": "unity_cmd_build", "type": plugin_settings.COMMAND_TYPE,
              "plugin": "unity_cli", "command": "build"}

    assert plugin_settings.with_declared_fields("unity_cmd_build", record) == record


# ======================================================
# What Blender actually does with it
# ======================================================

def test_blender_asks_the_shared_resolver(registry, tmp_path, monkeypatch):
    """One answer to "where do the files go", not one per plugin."""
    chosen = tmp_path / "Chosen"
    monkeypatch.setenv("ARIA_BLENDER_OUTPUT", str(chosen))

    assert blender_actions.output_dir() == chosen


def test_the_old_repository_folder_is_gone_from_the_code():
    """A constant nobody reads is a lie about where files go, and this
    one named the folder the developer objected to."""
    source = Path(blender_actions.__file__).read_text(encoding="utf-8")

    assert "aria_output" not in source
