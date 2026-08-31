"""A typed command line is answered by the CLI, or not at all.

THE FAILURE THIS EXISTS FOR
---------------------------
Measured on the developer's machine. Typed into chat:

    unity new-project --path "D:\\Users\\William\\Unity\\Projects\\ARIA_TestProject" --type 3D

nemo-12b replied:

    Unity project created at "D:\\Users\\William\\Unity\\Projects\\ARIA_TestProject"

Nothing ran. The packet log shows tool_runs empty and no
unity_cli_command_request at any point, and the directory did not
exist -- Projects/ was empty. The user was told a project had been
made and went looking for one.

That is the worst class of bug in this system: not a broken button,
which announces itself, but a confident false report the user then
acts on. The orchestrator already keeps workspace queries away from
the model for exactly this reason -- "a fact with an authority behind
it should never be routed through a model at all" -- and a command
invocation has an authority behind it too.

So these tests assert the two halves that matter: the model never sees
a command line, and every answer that did not run something says so in
its first sentence.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend.core import turn_orchestrator
from backend.core.turn_types import SessionState, TurnRequest
from backend.plugins import plugin_settings
from backend.unity import unity_cli_engine as engine


# ======================================================
# Fixtures
# ======================================================

_WINDOWS_CLI = """@echo off
if "%1"=="list" (echo {"commands":[{"name":"build","description":"Build the project"}]} & exit /b 0)
if "%1"=="build" (echo Building Player & exit /b 0)
if "%1"=="boom" (echo it broke 1>&2 & exit /b 2)
echo unknown & exit /b 1
"""

_POSIX_CLI = """#!/bin/sh
case "$1" in
  list) echo '{"commands":[{"name":"build","description":"Build the project"}]}'; exit 0;;
  build) echo "Building Player"; exit 0;;
  boom) echo "it broke" >&2; exit 2;;
esac
echo "unknown"; exit 1
"""


@pytest.fixture
def cli(tmp_path):
    folder = tmp_path / "bin"
    folder.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        path = folder / "unity.cmd"
        path.write_text(_WINDOWS_CLI.replace("\n", "\r\n"), encoding="utf-8")
    else:
        path = folder / "unity"
        path.write_text(_POSIX_CLI, encoding="utf-8")
        path.chmod(0o755)
    return path


@pytest.fixture
def registry(tmp_path, cli, monkeypatch):
    path = tmp_path / "plugins.json"
    path.write_text(json.dumps({
        "unity_cli": {"id": "unity_cli", "name": "Unity CLI", "version": "0.0.0",
                      "enabled": True, "logo": "", "configPage": "unity-cli-config",
                      "unity_cli_path": str(cli), "unity_cli_project": "",
                      "unity_cli_mode": "", "discovered": True},
    }, indent=2), encoding="utf-8")
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(path))
    monkeypatch.delenv(engine.ENV_CLI_PATH, raising=False)
    return path


def _turn(text: str) -> TurnRequest:
    return TurnRequest(
        messages=[{"role": "user", "content": text}],
        latest_user_text=text,
        conversation_id="test-conversation",
        session=SessionState(),
    )


def _reply(text: str):
    return turn_orchestrator._unity_cli_reply(_turn(text), [])


# ======================================================
# Reading the line
# ======================================================

def test_a_windows_path_survives_being_parsed(registry):
    """shlex would eat the backslashes. Every path here is a Windows path."""
    parsed = engine.parse_invocation(
        r'unity build --path "D:\Users\William\Unity\Projects\Game" --type 3D')

    assert parsed["command"] == "build"
    assert parsed["args"] == ["--path", r"D:\Users\William\Unity\Projects\Game",
                              "--type", "3D"]


@pytest.mark.parametrize("text", [
    "unity is a game engine",
    "what does unity do?",
    "tell me about unity",
    "unity",
    "",
])
def test_a_sentence_is_not_a_command(registry, text):
    """A false positive turns a question into a command, so the bar is
    a subcommand plus either a flag or a name the CLI actually reported."""
    assert engine.parse_invocation(text) is None
    assert _reply(text) is None


@pytest.mark.parametrize("text", [
    'unity new-project --path "D:\\Games\\X" --type 3D',
    "unity build --clean",
    "unity --version",
])
def test_a_command_line_is_a_command(registry, text):
    assert engine.parse_invocation(text) is not None


def test_a_bare_command_does_not_need_discovering_first(registry):
    """`unity env` is the FIRST thing anybody types.

    An earlier version only recognised a bare `unity <word>` once the
    CLI had already reported that word -- so `unity env` and
    `unity pipeline list` fell through to the language model, which
    routed them to phi-3-mini and answered with prose and a web search.
    A command that only counts once you have discovered it cannot be
    the one that does the discovering.
    """
    assert engine.parse_invocation("unity env")["command"] == "env"
    assert engine.parse_invocation("unity build")["command"] == "build"
    assert engine.parse_invocation("unity pipeline list") == {
        "command": "pipeline", "args": ["list"]}


# ======================================================
# The model never sees it
# ======================================================

def test_the_turn_is_answered_without_a_model(registry):
    result = _reply("unity build --clean")

    assert result is not None
    assert result.kind == "text"
    # Not a model id. No model took this turn, and putting one here
    # would attribute a sentence to something that never wrote it.
    assert result.model_id == turn_orchestrator.UNITY_CLI_MODEL


def test_an_ordinary_message_is_left_alone(registry):
    """The short-circuit must be invisible to every other turn."""
    for text in ("what is the weather in Paris?", "write me a haiku",
                 "how do I use Unity's animator?"):
        assert _reply(text) is None


# ======================================================
# What it says when it did not run
# ======================================================

def test_the_exact_message_that_was_hallucinated(registry):
    """The regression test for the bug itself."""
    result = _reply(
        r'unity new-project --path "D:\Users\William\Unity\Projects\ARIA_TestProject" --type 3D')

    assert result is not None
    text = result.text
    # `new-project` is not a command this CLI has, so it runs and the
    # CLI rejects it. What matters is where the answer comes from: the
    # original failure was a fabricated success, and now the outcome is
    # the process's own, whatever it happens to be.
    assert "failed" in text
    # The sentence nemo-12b produced, which was false.
    assert "created at" not in text
    # And no model was involved in producing this one.
    assert result.model_id == turn_orchestrator.UNITY_CLI_MODEL


def test_an_unknown_command_is_answered_by_the_cli(registry):
    """ARIA does not decide what the CLI has. The CLI does.

    A command ARIA has never heard of is run and the CLI's own error is
    reported, which is both shorter and more accurate than a list ARIA
    maintains and the CLI has never seen.
    """
    result = _reply("unity teleport --to mars")

    assert "failed" in result.text
    # The CLI's own words, passed through rather than summarised.
    assert "unknown" in result.text


def test_a_disabled_plugin_is_refused(registry):
    plugin_settings.disable_plugin("unity_cli")

    result = _reply("unity build --clean")

    assert "I did not run" in result.text
    assert "switched off" in result.text


def test_a_missing_plugin_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(tmp_path / "none.json"))

    result = _reply("unity build --clean")

    assert "I did not run" in result.text
    assert "not installed" in result.text


def test_a_disabled_command_still_blocks_a_tile_or_a_model(registry):
    """The registry gate did not go away; it moved to where it belongs.

    A line the user typed is their own instruction and runs. A tile
    press and a model tool call are not, and both still go through
    run_command, which refuses anything not registered and enabled.
    """
    plugin_settings.refresh_unity_cli_commands()

    outcome = engine.run_command("unity_cmd_build")

    assert outcome["success"] is False
    assert "not enabled" in outcome["error"]


def test_a_refusal_leads_with_the_fact_that_nothing_happened(registry):
    """The first sentence is the one a person actually reads."""
    plugin_settings.disable_plugin("unity_cli")

    result = _reply("unity build --clean")
    first = result.text.splitlines()[0]

    assert first.startswith("I did not run")
    assert "nothing was created" in first


# ======================================================
# What it says when it did run
# ======================================================

def test_an_enabled_command_actually_runs(registry):
    plugin_settings.refresh_unity_cli_commands()
    plugin_settings.enable_plugin("unity_cmd_build")

    result = _reply("unity build")

    assert result.text.startswith("Ran `unity build`")
    # The CLI's own output, not a summary of it.
    assert "Building Player" in result.text


def test_a_failing_command_is_reported_as_failing(registry):
    result = _reply("unity boom")

    assert "failed" in result.text
    assert "it broke" in result.text


def test_a_broken_engine_still_says_nothing_was_created(registry, monkeypatch):
    """The one outcome that must never happen is falling back to the
    model, which is what an exception here would do."""
    def explode(_invocation):
        raise RuntimeError("the engine fell over")

    monkeypatch.setattr(engine, "answer_invocation", explode)

    result = _reply("unity build --clean")

    assert result is not None, "an error must not hand the turn to the model"
    assert "nothing was created" in result.text


# ======================================================
# The log that lost the evidence
# ======================================================

def test_a_log_that_cannot_rotate_keeps_writing(tmp_path):
    """Rotation fails on Windows when another process holds the file,
    and RotatingFileHandler's response is to DROP the record. That is
    how the log that should have held a Ludo.ai failure came to be
    empty of it."""
    import logging

    from logger import _SharedRotatingFileHandler

    path = tmp_path / "test.log"
    handler = _SharedRotatingFileHandler(str(path), maxBytes=200, backupCount=1)

    def refuse(source, dest):
        raise OSError(32, "held by another process")

    handler.rotate = refuse

    log = logging.getLogger("rotation-probe")
    log.handlers = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False

    for index in range(60):
        log.info("line %d of a log that cannot be rotated", index)
    handler.close()

    written = path.read_text(encoding="utf-8")
    assert "line 59" in written, "records were dropped instead of written"
    assert handler._rollover_blocked is True


# ======================================================
# Tool schemas, so a model can act rather than describe
# ======================================================

def test_the_five_tools_the_task_asked_for_exist(registry):
    from backend.core import tool_registry

    tool_registry.register_unity_cli_tools()

    for name in ("unity_env", "unity_pipeline_list", "unity_cmd_create_scene",
                 "unity_test", "unity_build"):
        assert name in tool_registry._REGISTRY, f"{name} is not registered"


def test_a_model_cannot_compose_a_command_line(registry):
    """The safety property of the whole tool layer.

    Each tool is one fixed subcommand. A model chooses WHICH tool; it
    never says what the command line is. A single tool with a `command`
    field would have handed a 12B a way to run any subcommand of a
    program on this machine.
    """
    for spec in engine.UNITY_TOOLS:
        assert "command" not in spec["parameters"], (
            f"{spec['name']} lets a model choose the command")
        assert "args" not in spec["parameters"]


def test_an_unknown_tool_is_refused(registry):
    outcome = engine.run_tool("unity_rm_rf", {})

    assert outcome["success"] is False
    assert "not a Unity CLI tool" in outcome["error"]


def test_a_required_argument_is_required(registry):
    outcome = engine.run_tool("unity_cmd_create_scene", {})

    assert outcome["success"] is False
    assert "needs name" in outcome["error"]


def test_a_value_becomes_one_argv_entry(registry):
    """A value cannot smuggle in a second argument: it is one entry
    either way, and there is no shell to reinterpret it."""
    seen = {}
    import subprocess

    real = subprocess.Popen

    def watched(*args, **kwargs):
        seen["argv"] = args[0]
        return real(*args, **kwargs)

    import pytest as _pytest
    monkey = _pytest.MonkeyPatch()
    monkey.setattr(subprocess, "Popen", watched)
    try:
        engine.run_tool("unity_cmd_create_scene", {"name": "A B; rm -rf /"})
    finally:
        monkey.undo()

    assert seen["argv"][-2:] == ["--name", "A B; rm -rf /"]


def test_the_tools_disappear_when_the_plugin_is_off(registry):
    """An unusable tool in the brief spends a small model's attention
    and teaches it that tools do not work."""
    from backend.core import tool_registry

    plugin_settings.disable_plugin("unity_cli")
    tool_registry.register_unity_cli_tools()

    assert "unity_env" not in tool_registry._REGISTRY

    plugin_settings.enable_plugin("unity_cli")
    tool_registry.register_unity_cli_tools()

    assert "unity_env" in tool_registry._REGISTRY


# ======================================================
# Routing: a short command is not a simple one
# ======================================================

def test_a_unity_command_is_a_tool_turn():
    """complexity_router picks a model from prompt LENGTH, and
    "unity env" is nine characters -- so it went to phi-3-mini, which
    cannot call tools, and answered with prose and a web search.

    The length of a request is not a measure of what it asks for.
    """
    from backend.chat import model_router

    for text in ("unity env", "unity pipeline list", "build the unity project",
                 "run the unity tests"):
        assert model_router._mentions_tools(text) is True, text


def test_talking_about_unity_is_still_just_talking():
    from backend.chat import model_router

    for text in ("unity is a game engine", "how do I use Unity's animator?"):
        assert model_router._mentions_tools(text) is False, text


def test_a_unity_turn_is_classified_as_tool_bearing():
    from backend.chat import model_router

    kind = model_router.classify_turn(_turn("unity env"))

    assert kind in (model_router.TURN_TOOLS, model_router.TURN_HEAVY)
    assert kind != model_router.TURN_CHAT, "TURN_CHAT is what routed it to phi-3-mini"
