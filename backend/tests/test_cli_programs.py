"""Plugins that are programs, typed at directly.

THE FAILURE THIS EXISTS FOR
---------------------------
Measured on the developer's machine. Typed into chat:

    blender --background --python <script>

ARIA routed it to phi-3-mini and answered with:

    def main:
        bpy.ops.preferences.addonSettings(module="Blender3D", reset=True)

`def main:` is not valid Python, that operator does not exist, and the
last line then repeated eleven times until the stream was cut. Nothing
ran, and nothing said so.

It is the more instructive half of the pair with the Unity case: there
the model claimed a project had been created, here it produced
something shaped like an answer. A model asked to run what it cannot
run does not decline -- it writes what such a request usually produces.

So a typed command line is answered by the program or not at all.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from backend.chat import model_router
from backend.core import turn_orchestrator
from backend.core.turn_types import SessionState, TurnRequest
from backend.plugins import cli_programs, plugin_settings


# ======================================================
# Fixtures -- a real script standing in for Blender
# ======================================================

_WINDOWS = """@echo off
echo Blender 5.0.1
if "%1"=="--background" (if "%2"=="--boom" (echo it broke 1>&2 & exit /b 4))
exit /b 0
"""

_POSIX = """#!/bin/sh
echo "Blender 5.0.1"
for a in "$@"; do
  if [ "$a" = "--boom" ]; then echo "it broke" >&2; exit 4; fi
done
exit 0
"""


@pytest.fixture
def program(tmp_path):
    folder = tmp_path / "bin"
    folder.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        path = folder / "blender.cmd"
        path.write_text(_WINDOWS.replace("\n", "\r\n"), encoding="utf-8")
    else:
        path = folder / "blender"
        path.write_text(_POSIX, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def registry(tmp_path, program, monkeypatch):
    path = tmp_path / "plugins.json"
    path.write_text(json.dumps({
        "blender": {"id": "blender", "name": "Blender Integration",
                    "version": "1.0.0", "enabled": True, "logo": "",
                    "configPage": "blender-config",
                    "blender_path": str(program)},
    }, indent=2), encoding="utf-8")
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(path))
    return path


def _turn(text: str) -> TurnRequest:
    return TurnRequest(
        messages=[{"role": "user", "content": text}],
        latest_user_text=text,
        conversation_id="test-conversation",
        session=SessionState(),
    )


def _reply(text: str):
    return turn_orchestrator._cli_program_reply(_turn(text), [])


# ======================================================
# Reading the line
# ======================================================

def test_the_exact_line_that_was_hallucinated(registry):
    parsed = cli_programs.parse_invocation("blender --background --python <script>")

    assert parsed == {"program": "blender",
                      "args": ["--background", "--python", "<script>"]}


def test_a_windows_path_survives(registry):
    parsed = cli_programs.parse_invocation(
        r'blender --background --python "D:\Users\William\render.py"')

    assert parsed["args"][-1] == r"D:\Users\William\render.py"


@pytest.mark.parametrize("text", [
    "blender is a modelling tool",
    "blender models are stored as .blend files",
    "how do I use blender?",
    "what does blender do?",
    "blender",
    "",
])
def test_a_sentence_is_not_a_command(registry, text):
    """A false positive turns a question into an execution."""
    assert cli_programs.parse_invocation(text) is None
    assert _reply(text) is None


@pytest.mark.parametrize("text", [
    "blender --version",
    "blender --background --python x.py",
    "blender render",
])
def test_a_command_line_is_a_command(registry, text):
    assert cli_programs.parse_invocation(text) is not None


def test_an_unknown_program_is_left_alone(registry):
    """ludo is an HTTP API, not something you type at."""
    assert cli_programs.parse_invocation("ludo --version") is None
    assert cli_programs.parse_invocation("photoshop --open x.psd") is None


# ======================================================
# The model never sees it
# ======================================================

def test_the_turn_is_answered_without_a_model(registry):
    result = _reply("blender --version")

    assert result is not None
    assert result.kind == "text"
    assert result.model_id == turn_orchestrator.CLI_PROGRAM_MODEL


def test_ordinary_messages_are_untouched(registry):
    for text in ("what is the weather in Paris?", "write me a haiku",
                 "how do I model a chair in Blender?"):
        assert _reply(text) is None


def test_a_program_line_routes_to_a_model_that_could_act(registry):
    """complexity_router picks from prompt LENGTH, and this one was
    short enough for phi-3-mini -- which cannot call tools, and so
    answered with invented bpy operators repeated eleven times."""
    assert model_router._mentions_tools("blender --background --python x.py") is True
    assert model_router._mentions_tools("blender is a modelling tool") is False


def test_routing_and_running_agree(registry):
    """Both ask the same parser, so a line that would run is a line
    that routes."""
    for text in ("blender --version", "blender is a modelling tool",
                 "how do I use blender?"):
        parses = cli_programs.parse_invocation(text) is not None
        assert model_router._mentions_tools(text) is parses


# ======================================================
# Running it
# ======================================================

def test_it_actually_runs(registry):
    result = _reply("blender --version")

    assert result.text.startswith("Ran `blender --version`")
    assert "Blender 5.0.1" in result.text


def test_a_failure_is_reported_as_one(registry):
    result = _reply("blender --background --boom")

    assert "failed" in result.text
    assert "it broke" in result.text


def test_a_gui_is_never_opened(registry, monkeypatch):
    """Blender opens a window unless told otherwise, and a window
    nobody can see is a process that never exits."""
    seen = {}
    from backend.core import cli_runner

    real = cli_runner.run

    def watched(executable, arguments, **kwargs):
        seen["argv"] = list(arguments)
        return real(executable, arguments, **kwargs)

    monkeypatch.setattr(cli_runner, "run", watched)
    _reply("blender --version")

    assert "--background" in seen["argv"]


def test_the_flag_is_not_added_twice(registry, monkeypatch):
    seen = {}
    from backend.core import cli_runner

    real = cli_runner.run

    def watched(executable, arguments, **kwargs):
        seen["argv"] = list(arguments)
        return real(executable, arguments, **kwargs)

    monkeypatch.setattr(cli_runner, "run", watched)
    _reply("blender --background --version")

    assert seen["argv"].count("--background") == 1


# ======================================================
# When it cannot run
# ======================================================

def test_a_disabled_plugin_is_refused(registry):
    plugin_settings.disable_plugin("blender")

    result = _reply("blender --version")

    assert result.text.startswith("I did not run")
    assert "nothing happened" in result.text
    assert "switched off" in result.text


def test_a_missing_plugin_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(tmp_path / "none.json"))

    result = _reply("blender --version")

    assert result.text.startswith("I did not run")
    assert "not installed" in result.text


def test_a_path_that_is_gone_is_refused(registry, tmp_path):
    plugins = plugin_settings.load_plugins()
    plugins["blender"]["blender_path"] = str(tmp_path / "vanished.exe")
    plugin_settings.save_plugins(plugins)

    result = _reply("blender --version")

    assert result.text.startswith("I did not run")
    assert "Nothing runnable" in result.text


def test_a_refusal_never_claims_to_have_run(registry):
    plugin_settings.disable_plugin("blender")

    result = _reply("blender --version")

    assert not result.text.startswith("Ran ")


def test_a_broken_runner_still_says_nothing_happened(registry, monkeypatch):
    """An exception must not hand the turn back to the model, which is
    the one outcome this whole path exists to prevent."""
    def explode(_invocation, **_kwargs):
        raise RuntimeError("the runner fell over")

    monkeypatch.setattr(cli_programs, "answer_invocation", explode)

    result = _reply("blender --version")

    assert result is not None
    assert "nothing happened" in result.text


# ======================================================
# The output is a chat message, not a log
# ======================================================

def test_a_long_log_keeps_the_end(registry):
    lines = [f"line {i}" for i in range(400)]
    lines.append("Error: the actual reason")

    shown = cli_programs._readable("\n".join(lines))

    assert "Error: the actual reason" in shown
    assert "line 0" not in shown
    assert "not shown" in shown
