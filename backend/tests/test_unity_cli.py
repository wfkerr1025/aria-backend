"""The Unity CLI: finding it, asking what it can do, and running it.

THE FAKE CLI IS A REAL PROGRAM
------------------------------
These tests write an actual script -- a .cmd on Windows, a shell script
elsewhere -- and point ARIA at it. So the subprocess, the exit codes,
the streaming and the JSON parsing are the real ones, and what is being
tested is this side of the process boundary rather than a mock of it.

That matters more than usual here, because the OTHER side is unverified.
There is no Unity on the machine this was written on, so `unity list
--json` is the task's description of the tool and not something checked
against it. A test using a mock would have proved only that the mock
matched my assumption. This way, the argv, the timeout, the non-zero
exit and the parse are genuinely exercised, and the one unknown stays
clearly the one unknown.

No test here touches the real registry: conftest redirects it and fails
any test that writes to it anyway.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from backend import ipc_router, ipc_schema as schema
from backend.plugins import plugin_discovery, plugin_settings
from backend.unity import unity_cli_engine as engine


# ======================================================
# A fake unity, written as a real script
# ======================================================

_WINDOWS_CLI = """@echo off
if "%1"=="--version" (echo unity 3.4.5 & exit /b 0)
if "%1"=="list" (echo {"commands":[{"name":"build","description":"Build the project"},{"name":"test","description":"Run tests"}],"pipeline":[{"name":"deploy","description":"Ship it"}]} & exit /b 0)
if "%1"=="build" (echo compiling & echo linking & echo {"status":"ok","artifacts":2} & exit /b 0)
if "%1"=="plain" (echo just text & exit /b 0)
if "%1"=="boom" (echo it broke 1>&2 & exit /b 3)
if "%1"=="echoargs" (echo ARGS %* & exit /b 0)
echo unknown command & exit /b 1
"""

_POSIX_CLI = """#!/bin/sh
case "$1" in
  --version) echo "unity 3.4.5"; exit 0;;
  list) echo '{"commands":[{"name":"build","description":"Build the project"},{"name":"test","description":"Run tests"}],"pipeline":[{"name":"deploy","description":"Ship it"}]}'; exit 0;;
  build) echo "compiling"; echo "linking"; echo '{"status":"ok","artifacts":2}'; exit 0;;
  plain) echo "just text"; exit 0;;
  boom) echo "it broke" >&2; exit 3;;
  echoargs) echo "ARGS $*"; exit 0;;
esac
echo "unknown command"; exit 1
"""


def _write_cli(folder: Path, name: str = "unity") -> Path:
    """A runnable fake Unity CLI in this folder."""
    folder.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        path = folder / f"{name}.cmd"
        path.write_text(_WINDOWS_CLI.replace("\n", "\r\n"), encoding="utf-8")
    else:
        path = folder / name
        path.write_text(_POSIX_CLI, encoding="utf-8")
        path.chmod(0o755)
    return path


@pytest.fixture
def cli(tmp_path):
    return _write_cli(tmp_path / "bin")


@pytest.fixture
def registry(tmp_path, cli, monkeypatch):
    """A registry with the Unity CLI plugin installed and enabled."""
    path = tmp_path / "plugins.json"
    path.write_text(json.dumps({
        "unity": {"id": "unity", "name": "Unity Integration", "version": "1.0.0",
                  "enabled": True, "logo": "assets/plugin_logos/unity.png",
                  "configPage": "unity-config", "unity_path": "", "project_path": ""},
        "unity_cli": {"id": "unity_cli", "name": "Unity CLI", "version": "0.0.0",
                      "enabled": True, "logo": "assets/plugin_logos/unity.png",
                      "configPage": "unity-cli-config",
                      "unity_cli_path": str(cli), "unity_cli_project": "",
                      "unity_cli_mode": "", "discovered": True},
    }, indent=2), encoding="utf-8")

    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(path))
    monkeypatch.delenv(engine.ENV_CLI_PATH, raising=False)
    return path


# ======================================================
# PART 1 - finding the CLI
# ======================================================

def test_the_cli_is_found_on_path(tmp_path, monkeypatch):
    folder = tmp_path / "bin"
    _write_cli(folder)
    monkeypatch.setenv("PATH", str(folder), prepend=False)
    monkeypatch.delenv(engine.ENV_CLI_PATH, raising=False)
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(tmp_path / "none.json"))

    assert engine.cli_path().parent == folder


def test_a_saved_setting_beats_path(registry, tmp_path, monkeypatch):
    """A path a user typed is not a guess and must win over one."""
    other = _write_cli(tmp_path / "elsewhere")
    monkeypatch.setenv("PATH", str(other.parent))
    plugin_settings.update_plugin("unity_cli", {engine.FIELD_PATH: str(registry.parent / "bin" / other.name)})

    assert engine.cli_path().parent.name == "bin"


def test_the_environment_beats_everything(registry, tmp_path, monkeypatch):
    override = _write_cli(tmp_path / "override")
    monkeypatch.setenv(engine.ENV_CLI_PATH, str(override))

    assert engine.cli_path() == override


def test_no_cli_anywhere_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv(engine.ENV_CLI_PATH, raising=False)
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(tmp_path / "none.json"))
    monkeypatch.setenv(plugin_discovery.ENV_PROGRAM_ROOTS, str(tmp_path / "empty"))

    with pytest.raises(engine.UnityCliUnavailable):
        engine.cli_path()


def test_a_configured_path_that_is_gone_says_which_path(registry, tmp_path, monkeypatch):
    monkeypatch.setenv(engine.ENV_CLI_PATH, str(tmp_path / "vanished.exe"))

    with pytest.raises(engine.UnityCliUnavailable) as raised:
        engine.cli_path()

    assert "vanished.exe" in str(raised.value)


# ======================================================
# PART 1 - discovery, and the registry entry
# ======================================================

def test_discovery_finds_the_cli(tmp_path, monkeypatch):
    folder = tmp_path / "bin"
    _write_cli(folder)
    monkeypatch.setenv("PATH", str(folder))
    monkeypatch.delenv(engine.ENV_CLI_PATH, raising=False)
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(tmp_path / "none.json"))

    found = plugin_discovery.discover_unity_cli()

    assert len(found) == 1
    assert found[0]["id"] == "unity_cli"
    assert found[0]["configPage"] == "unity-cli-config"
    assert found[0]["settings"][engine.FIELD_PATH].endswith(("unity.cmd", "unity"))
    assert found[0]["discovered"] is True


def test_discovery_does_not_run_the_cli(tmp_path, monkeypatch):
    """A discovery pass happens when a page opens, and a page opening
    must not start a program. The version stays 0.0.0 until somebody
    presses Test CLI, which is a deliberate act."""
    folder = tmp_path / "bin"
    _write_cli(folder)
    monkeypatch.setenv("PATH", str(folder))
    monkeypatch.delenv(engine.ENV_CLI_PATH, raising=False)
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(tmp_path / "none.json"))

    import subprocess

    def refuse(*args, **kwargs):
        raise AssertionError("discovery started a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)

    found = plugin_discovery.discover_unity_cli()

    assert found[0]["version"] == "0.0.0"


def test_no_cli_is_an_empty_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv(engine.ENV_CLI_PATH, raising=False)
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(tmp_path / "none.json"))
    monkeypatch.setenv(plugin_discovery.ENV_PROGRAM_ROOTS, str(tmp_path / "empty"))

    assert plugin_discovery.discover_unity_cli() == []


def test_the_cli_plugin_merges_without_touching_unity(tmp_path, cli, monkeypatch):
    """unity and unity_cli are different tools and different plugins.
    Finding one must not disturb the other."""
    path = tmp_path / "plugins.json"
    path.write_text(json.dumps({
        "unity": {"id": "unity", "name": "Unity Integration", "version": "1.0.0",
                  "enabled": True, "logo": "", "configPage": "unity-config",
                  "unity_path": "C:/somewhere/Unity.exe", "project_path": ""},
    }), encoding="utf-8")
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(path))
    monkeypatch.setenv("PATH", str(cli.parent))
    monkeypatch.delenv(engine.ENV_CLI_PATH, raising=False)
    monkeypatch.setenv(plugin_discovery.ENV_PROGRAM_ROOTS, str(tmp_path / "empty"))
    monkeypatch.setenv(plugin_discovery.ENV_PROJECT_ROOT, str(tmp_path / "empty"))

    plugin_settings.discover_plugins()

    assert plugin_settings.get_plugin("unity")["unity_path"] == "C:/somewhere/Unity.exe"
    assert plugin_settings.get_plugin("unity_cli")["enabled"] is False


def test_a_dismissed_cli_is_not_resurrected(tmp_path, cli, monkeypatch):
    path = tmp_path / "plugins.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(path))
    monkeypatch.setenv("PATH", str(cli.parent))
    monkeypatch.delenv(engine.ENV_CLI_PATH, raising=False)
    monkeypatch.setenv(plugin_discovery.ENV_PROGRAM_ROOTS, str(tmp_path / "empty"))
    monkeypatch.setenv(plugin_discovery.ENV_PROJECT_ROOT, str(tmp_path / "empty"))

    plugin_settings.discover_plugins()
    plugin_settings.remove_plugin("unity_cli")

    plugin_settings.discover_plugins()

    assert "unity_cli" not in {p["id"] for p in plugin_settings.list_plugins()}


# ======================================================
# PART 2 - the config page's fields
# ======================================================

def test_the_config_fields_round_trip(registry, tmp_path):
    project = tmp_path / "MyGame"
    project.mkdir()

    plugin_settings.update_plugin("unity_cli", {
        engine.FIELD_PROJECT: str(project),
        engine.FIELD_MODE: "PlayMode",
    })

    saved = plugin_settings.get_plugin("unity_cli")
    assert saved[engine.FIELD_PROJECT] == str(project)
    assert saved[engine.FIELD_MODE] == "PlayMode"


def test_the_mode_must_be_one_of_the_three(registry):
    assert plugin_settings.validate_plugin("unity_cli", {engine.FIELD_MODE: "Sideways"})
    for mode in ("", *engine.MODES):
        assert plugin_settings.validate_plugin("unity_cli", {engine.FIELD_MODE: mode}) == []


def test_a_project_path_that_is_not_a_folder_is_refused(registry, cli):
    problems = plugin_settings.validate_plugin("unity_cli", {engine.FIELD_PROJECT: str(cli)})

    assert any("no folder" in problem for problem in problems)


def test_the_page_offers_the_three_modes():
    choices = plugin_settings.FIELD_CHOICES["unity_cli"][engine.FIELD_MODE]

    assert set(engine.MODES) <= set(choices)
    assert "" in choices, "an empty mode means passing no --mode at all"


# ======================================================
# PART 2 - Test CLI
# ======================================================

def test_test_cli_reports_success_and_records_the_version(registry):
    outcome = plugin_settings.test_plugin_connection("unity_cli")

    assert outcome["ok"] is True
    assert "3.4.5" in outcome["message"]
    assert plugin_settings.get_plugin("unity_cli")["version"] == "3.4.5"


def test_test_cli_starts_the_program_once(registry, monkeypatch):
    """An earlier draft asked three times for one button press."""
    import subprocess

    starts = []
    real = subprocess.Popen

    def counted(*args, **kwargs):
        starts.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", counted)

    plugin_settings.test_plugin_connection("unity_cli")

    assert len(starts) == 1, f"started {len(starts)} processes"


def test_test_cli_on_a_missing_program_says_so(registry, tmp_path, monkeypatch):
    monkeypatch.setenv(engine.ENV_CLI_PATH, str(tmp_path / "gone.exe"))

    outcome = plugin_settings.test_plugin_connection("unity_cli")

    assert outcome["ok"] is False
    assert "gone.exe" in outcome["message"]


def test_a_program_that_is_not_the_unity_cli_is_not_claimed_to_be(registry, tmp_path, monkeypatch):
    if os.name == "nt":
        impostor = tmp_path / "impostor.cmd"
        impostor.write_text("@echo off\r\nexit /b 1\r\n", encoding="utf-8")
    else:
        impostor = tmp_path / "impostor"
        impostor.write_text("#!/bin/sh\nexit 1\n")
        impostor.chmod(0o755)
    monkeypatch.setenv(engine.ENV_CLI_PATH, str(impostor))

    outcome = plugin_settings.test_plugin_connection("unity_cli")

    assert outcome["ok"] is False
    assert "may not be the Unity CLI" in outcome["message"]


# ======================================================
# PART 3 - command discovery
# ======================================================

def test_listing_parses_the_commands(registry):
    listing = engine.list_commands()

    assert listing["success"] is True
    assert {command["name"] for command in listing["commands"]} == {"build", "test", "deploy"}


def test_the_groups_survive(registry):
    by_name = {c["name"]: c for c in engine.list_commands()["commands"]}

    assert by_name["build"]["group"] == "builtin"
    assert by_name["deploy"]["group"] == "pipeline"


@pytest.mark.parametrize("shape,expected", [
    ('["build","test"]', {"build", "test"}),
    ('{"commands":["build"]}', {"build"}),
    ('{"commands":[{"name":"build","label":"Build"}]}', {"build"}),
    ('{"builtin":[{"name":"a"}],"pipeline":[{"name":"b"}],"custom":[{"name":"c"}]}',
     {"a", "b", "c"}),
    ('{"nothing":"useful"}', set()),
    ('not json at all', set()),
])
def test_several_plausible_json_shapes_are_accepted(shape, expected):
    """The real format is not verified, so the parser accepts the
    shapes it might plausibly be rather than betting on one."""
    parsed = engine._extract_json(shape)

    assert {c["name"] for c in engine._named_commands(parsed)} == expected


def test_json_after_a_banner_is_still_found():
    parsed = engine._extract_json('Unity CLI 3.4\n{"commands":["build"]}\n')

    assert parsed == {"commands": ["build"]}


def test_a_command_record_has_the_shape_the_task_asked_for(registry):
    record = engine.command_record({"name": "build", "label": "Build it"})

    assert record["id"] == "unity_cmd_build"
    assert record["type"] == "unity_cli_command"
    assert record["plugin"] == "unity_cli"
    assert record["command"] == "build"
    assert record["args"] == []
    assert record["discovered"] is True
    assert record["enabled"] is False


def test_discovered_commands_reach_the_registry(registry):
    plugin_settings.refresh_unity_cli_commands()

    stored = plugin_settings.load_plugins()
    assert "unity_cmd_build" in stored
    assert stored["unity_cmd_build"]["enabled"] is False


def test_commands_do_not_appear_as_plugin_cards(registry):
    plugin_settings.refresh_unity_cli_commands()

    listed = {plugin["id"] for plugin in plugin_settings.list_plugins()}
    assert not any(name.startswith("unity_cmd_") for name in listed)

    commands = {command["id"] for command in plugin_settings.list_commands()}
    assert "unity_cmd_build" in commands


def test_a_refresh_does_not_overwrite_an_edited_command(registry):
    """The whole point of merging rather than replacing."""
    plugin_settings.refresh_unity_cli_commands()
    plugin_settings.update_plugin("unity_cmd_build",
                                  {"args": ["--target", "Android"]})

    plugin_settings.refresh_unity_cli_commands()

    assert plugin_settings.get_plugin("unity_cmd_build")["args"] == ["--target", "Android"]


def test_a_refresh_keeps_a_command_enabled(registry):
    plugin_settings.refresh_unity_cli_commands()
    plugin_settings.enable_plugin("unity_cmd_build")

    plugin_settings.refresh_unity_cli_commands()

    assert plugin_settings.get_plugin("unity_cmd_build")["enabled"] is True


def test_a_removed_command_does_not_come_back_by_itself(registry):
    plugin_settings.refresh_unity_cli_commands()
    plugin_settings.remove_plugin("unity_cmd_test")

    plugin_settings.refresh_unity_cli_commands()

    assert "unity_cmd_test" not in {c["id"] for c in plugin_settings.list_commands()}


def test_an_explicit_refresh_reconsiders_a_removed_command(registry):
    plugin_settings.refresh_unity_cli_commands()
    plugin_settings.remove_plugin("unity_cmd_test")

    plugin_settings.refresh_unity_cli_commands(force=True)

    assert "unity_cmd_test" in {c["id"] for c in plugin_settings.list_commands()}


def test_a_disabled_cli_plugin_is_not_asked_for_commands(registry):
    """Listing commands runs the CLI. Adopting the plugin is the consent."""
    plugin_settings.disable_plugin("unity_cli")

    outcome = plugin_settings.refresh_unity_cli_commands()

    assert outcome["success"] is False
    assert "Enable the Unity CLI plugin" in outcome["error"]


# ======================================================
# PART 5 - running one
# ======================================================

def _enabled_build(registry):
    plugin_settings.refresh_unity_cli_commands()
    plugin_settings.enable_plugin("unity_cmd_build")
    return "unity_cmd_build"


def test_running_a_command_returns_structured_output(registry):
    outcome = engine.run_command(_enabled_build(registry))

    assert outcome["success"] is True
    assert outcome["code"] == 0
    assert outcome["json"] == {"status": "ok", "artifacts": 2}
    assert "compiling" in outcome["output"]


def test_output_streams_as_it_arrives(registry):
    seen = []
    engine.run_command(_enabled_build(registry),
                       on_output=lambda kind, line: seen.append((kind, line)))

    assert [line.strip() for _, line in seen][:2] == ["compiling", "linking"]
    assert all(kind == "stdout" for kind, _ in seen)


def test_plain_text_output_is_not_a_failure(registry):
    _enabled_build(registry)
    plugin_settings.update_plugin("unity_cmd_build", {"command": "plain"})

    outcome = engine.run_command("unity_cmd_build")

    assert outcome["success"] is True
    assert outcome["json"] is None, "there was no JSON, and none is not an error"
    assert "just text" in outcome["output"]


def test_a_non_zero_exit_is_reported_with_its_output(registry):
    _enabled_build(registry)
    plugin_settings.update_plugin("unity_cmd_build", {"command": "boom"})

    outcome = engine.run_command("unity_cmd_build")

    assert outcome["success"] is False
    assert outcome["code"] == 3
    assert "it broke" in outcome["error"]
    # The reason a build failed is in its output; an error message that
    # replaced it with "exit code 3" would be useless.
    assert "it broke" in outcome["output"]


def test_a_command_that_is_not_registered_is_refused(registry):
    outcome = engine.run_command("unity_cmd_nonsense")

    assert outcome["success"] is False
    assert "not a Unity CLI command" in outcome["error"]


def test_a_disabled_command_will_not_run(registry):
    plugin_settings.refresh_unity_cli_commands()

    outcome = engine.run_command("unity_cmd_build")

    assert outcome["success"] is False
    assert "not enabled" in outcome["error"]


def test_a_plugin_is_not_runnable_as_a_command(registry):
    outcome = engine.run_command("unity_cli")

    assert outcome["success"] is False
    assert "not a Unity CLI command" in outcome["error"]


def test_a_missing_cli_is_reported_not_raised(registry, tmp_path, monkeypatch):
    command_id = _enabled_build(registry)
    monkeypatch.setenv(engine.ENV_CLI_PATH, str(tmp_path / "gone.exe"))

    outcome = engine.run_command(command_id)

    assert outcome["success"] is False
    assert "gone.exe" in outcome["error"]


def test_a_command_that_never_finishes_is_given_up_on(registry, tmp_path, monkeypatch):
    """A timeout is an answer, and the process does not survive it."""
    if os.name == "nt":
        slow = tmp_path / "slow.cmd"
        slow.write_text("@echo off\r\nping -n 30 127.0.0.1 >nul\r\n", encoding="utf-8")
    else:
        slow = tmp_path / "slow"
        slow.write_text("#!/bin/sh\nsleep 30\n")
        slow.chmod(0o755)

    monkeypatch.setenv(engine.ENV_CLI_PATH, str(slow))
    monkeypatch.setenv(engine.ENV_TIMEOUT, "1")

    import time

    started = time.monotonic()
    result = engine._invoke(["anything"])
    elapsed = time.monotonic() - started

    assert result["success"] is False
    assert "did not finish" in result["error"]

    # AND IT COMES BACK. This is the assertion that matters, and the one
    # the first version of this test did not make: killing only the
    # process ARIA started left the .cmd's child holding the stdout pipe
    # open, so the read blocked, and a one-second timeout returned the
    # right answer twenty-nine seconds late. The verdict was correct and
    # the timeout was useless.
    assert elapsed < 10, f"the timeout took {elapsed:.0f}s to return"


# ======================================================
# PART 5 - what gets run
# ======================================================

def test_the_invocation_is_settings_then_arguments(registry, tmp_path):
    project = tmp_path / "MyGame"
    project.mkdir()
    plugin_settings.update_plugin("unity_cli", {
        engine.FIELD_PROJECT: str(project), engine.FIELD_MODE: "EditMode"})
    plugin_settings.refresh_unity_cli_commands()

    record = plugin_settings.get_plugin("unity_cmd_build")
    argv = engine.build_invocation(record, ["--extra"])

    assert argv == ["build", "--project", str(project), "--mode", "EditMode", "--extra"]


def test_an_empty_mode_passes_no_mode(registry):
    plugin_settings.refresh_unity_cli_commands()
    record = plugin_settings.get_plugin("unity_cmd_build")

    assert "--mode" not in engine.build_invocation(record)


def test_a_template_cannot_choose_the_program(registry):
    """The executable comes from settings and only from settings.

    A template is subcommands and flags. If it could name a program,
    editing a command would be a way to run anything on the machine.
    """
    command_id = _enabled_build(registry)
    plugin_settings.update_plugin("unity_cmd_build", {"command": "echoargs"})

    outcome = engine.run_command(command_id)

    # It ran the configured fake unity, which answered. Nothing else did.
    assert outcome["success"] is True
    assert "ARGS" in outcome["output"]


def test_arguments_are_separate_argv_entries(registry):
    """This is what makes shell quoting something this code never has
    to get right: there is no shell."""
    command_id = _enabled_build(registry)
    plugin_settings.update_plugin(
        "unity_cmd_build", {"command": "echoargs", "args": ["one two", "three"]})

    record = plugin_settings.get_plugin(command_id)
    argv = engine.build_invocation(record)

    assert argv == ["echoargs", "one two", "three"]


def test_a_command_is_never_run_through_a_shell(registry, monkeypatch):
    import subprocess

    seen = {}
    real = subprocess.Popen

    def watched(*args, **kwargs):
        seen.update(kwargs)
        seen["argv"] = args[0]
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", watched)
    engine.run_command(_enabled_build(registry))

    assert seen.get("shell", False) is False
    assert isinstance(seen["argv"], list)


# ======================================================
# PART 5 - validation of what a form may send
# ======================================================

def test_arguments_must_be_a_list_of_single_line_strings(registry):
    plugin_settings.refresh_unity_cli_commands()

    assert plugin_settings.validate_plugin("unity_cmd_build", {"args": "not a list"})
    assert plugin_settings.validate_plugin("unity_cmd_build", {"args": [1, 2]})
    assert plugin_settings.validate_plugin("unity_cmd_build", {"args": ["a\nb"]})
    assert plugin_settings.validate_plugin("unity_cmd_build", {"args": ["--ok"]}) == []


def test_a_command_cannot_be_retyped_or_reassigned(registry):
    plugin_settings.refresh_unity_cli_commands()

    assert plugin_settings.validate_plugin("unity_cmd_build", {"type": "something_else"})
    assert plugin_settings.validate_plugin("unity_cmd_build", {"plugin": "blender"})


def test_a_command_keeps_its_discovery_flags(registry):
    plugin_settings.refresh_unity_cli_commands()

    plugin_settings.update_plugin("unity_cmd_build",
                                  {"discovered": False, "dismissed": True})

    stored = plugin_settings.get_plugin("unity_cmd_build")
    assert stored["discovered"] is True
    assert stored.get("dismissed", False) is False


# ======================================================
# PART 6/7 - the IPC surface
# ======================================================

def test_listing_commands_over_ipc_does_not_run_the_cli(registry, monkeypatch):
    plugin_settings.refresh_unity_cli_commands()

    import subprocess

    def refuse(*args, **kwargs):
        raise AssertionError("listing started a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)

    packet = ipc_router.dispatch({"type": schema.UNITY_CLI_COMMANDS_REQUEST,
                                  "payload": {}})

    assert packet["type"] == schema.UNITY_CLI_COMMANDS_RESULT
    assert {c["id"] for c in packet["payload"]["commands"]} >= {"unity_cmd_build"}


def test_refreshing_over_ipc_finds_the_commands(registry):
    packet = ipc_router.dispatch({"type": schema.UNITY_CLI_REFRESH_REQUEST,
                                  "payload": {}})

    assert {c["id"] for c in packet["payload"]["added"]} == {
        "unity_cmd_build", "unity_cmd_test", "unity_cmd_deploy"}


def test_running_over_ipc_streams_and_answers(registry):
    command_id = _enabled_build(registry)
    streamed = []

    packet = ipc_router.dispatch(
        {"type": schema.UNITY_CLI_COMMAND_REQUEST, "payload": {"id": command_id}},
        on_progress=streamed.append)

    assert packet["type"] == schema.UNITY_CLI_COMMAND_RESULT
    payload = packet["payload"]
    assert payload["success"] is True
    assert payload["json"] == {"status": "ok", "artifacts": 2}
    assert payload["invocation"] == ["build"]

    # Every streamed item is a whole packet, not a progress string:
    # build output must not become chat commentary.
    assert streamed, "nothing was streamed"
    for item in streamed:
        assert item["type"] == schema.UNITY_CLI_OUTPUT
        assert item["payload"]["id"] == command_id


def test_the_run_handler_receives_on_progress():
    """dispatch only forwards on_progress to handlers that asked for it,
    and this is one of them."""
    assert schema.UNITY_CLI_COMMAND_REQUEST in ipc_router._WANTS_PROGRESS


def test_a_run_without_an_id_is_refused(registry):
    packet = ipc_router.dispatch({"type": schema.UNITY_CLI_COMMAND_REQUEST,
                                  "payload": {}})

    assert packet["type"] == "error"


def test_args_must_be_strings_over_the_wire(registry):
    command_id = _enabled_build(registry)

    packet = ipc_router.dispatch({"type": schema.UNITY_CLI_COMMAND_REQUEST,
                                  "payload": {"id": command_id, "args": [1, 2]}})

    assert packet["type"] == "error"


def test_every_unity_cli_handler_is_registered_once():
    for name in (schema.UNITY_CLI_COMMANDS_REQUEST,
                 schema.UNITY_CLI_REFRESH_REQUEST,
                 schema.UNITY_CLI_COMMAND_REQUEST):
        assert name in ipc_router._HANDLERS


def test_no_model_tool_can_run_a_unity_command():
    """These run because a person pressed Run. The tool registry is
    what a model can reach, and none of this is in it."""
    from backend.core import tool_registry

    names = {str(name).lower() for name in getattr(tool_registry, "_TOOLS", {})}

    assert not any("unity_cli" in name for name in names)


# ======================================================
# PART 9 - the registry still holds together
# ======================================================

def test_commands_do_not_break_registry_validation(registry):
    plugin_settings.refresh_unity_cli_commands()

    assert plugin_settings.validate_registry(plugin_settings.load_plugins()) == []


def test_two_commands_may_share_a_name_with_a_plugin(registry):
    """The unique-name rule is about plugin cards, not commands."""
    problems = plugin_settings.validate_registry({
        "blender": {"id": "blender", "name": "Build"},
        "unity_cmd_build": {"id": "unity_cmd_build", "name": "Build",
                            "type": "unity_cli_command"},
    })

    assert problems == []


def test_the_cli_plugin_gets_no_stray_executable_path(registry):
    """It has unity_cli_path. A second empty path field would put a box
    on its page that nothing reads."""
    plugin = plugin_settings.get_plugin("unity_cli")

    assert plugin_settings.DISCOVERED_FIELD not in plugin


def test_a_command_gets_no_path_field_either(registry):
    plugin_settings.refresh_unity_cli_commands()

    assert plugin_settings.DISCOVERED_FIELD not in plugin_settings.get_plugin("unity_cmd_build")


# ======================================================
# Addressing a config page whose name is not its id
# ======================================================

def test_the_config_page_can_be_looked_up_by_its_route(registry):
    """unity_cli's page is "unity-cli-config", and an underscore is not
    a hyphen.

    The config page used to work out which plugin it was for by taking
    "-config" off its own route, which made it ask for a plugin called
    "unity-cli". The registry answered, correctly, that no such plugin
    was installed, and the page showed that as an error where its
    settings should have been.
    """
    plugin = plugin_settings.get_plugin_by_config_page("unity-cli-config")

    assert plugin["id"] == "unity_cli"


def test_an_unknown_page_says_so(registry):
    with pytest.raises(plugin_settings.PluginError):
        plugin_settings.get_plugin_by_config_page("nothing-config")


def test_the_page_opens_over_ipc_without_knowing_the_id(registry):
    packet = ipc_router.dispatch({
        "type": schema.PLUGIN_GET_REQUEST,
        "payload": {"configPage": "unity-cli-config"},
    })

    assert packet["type"] == schema.PLUGIN_GET_RESULT
    plugin = packet["payload"]["plugin"]
    assert plugin["id"] == "unity_cli"
    # The page reads its own id back out of this, so it has to be here.
    assert plugin["configPage"] == "unity-cli-config"


def test_asking_by_id_still_works(registry):
    """Every other page addresses itself this way, and must keep doing so."""
    packet = ipc_router.dispatch({
        "type": schema.PLUGIN_GET_REQUEST,
        "payload": {"id": "unity"},
    })

    assert packet["type"] == schema.PLUGIN_GET_RESULT
    assert packet["payload"]["plugin"]["id"] == "unity"


def test_the_route_the_task_asked_for_is_the_one_stored(registry):
    assert plugin_settings.get_plugin("unity_cli")["configPage"] == "unity-cli-config"


def test_two_plugins_cannot_claim_one_config_page():
    """A page addresses a plugin, so a shared page is a route with two
    possible answers and no way to choose."""
    problems = plugin_settings.validate_registry({
        "one": {"id": "one", "name": "One", "configPage": "shared-config"},
        "two": {"id": "two", "name": "Two", "configPage": "shared-config"},
    })

    assert any("shared-config" in problem for problem in problems)


def test_every_installed_plugin_can_be_reached_from_its_route(registry):
    """The invariant the bug broke: whatever a plugin's page is called,
    opening it finds that plugin."""
    plugin_settings.refresh_unity_cli_commands()

    for plugin in plugin_settings.list_plugins():
        page = plugin.get("configPage")
        if not page:
            continue
        assert plugin_settings.get_plugin_by_config_page(page)["id"] == plugin["id"]
