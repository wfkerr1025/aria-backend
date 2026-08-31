# backend/tests/test_unity_ops.py
#
# The Python half of the Unity bridge.
#
# Nothing here runs Unity. subprocess.run is replaced, so every path --
# a good result, an empty one, malformed JSON, a timeout, a refusal, a
# retry -- is exercised on a machine with no editor installed. That is
# the only way this can be tested at all, and it is also the right way:
# what is being checked is how this module behaves when Unity says
# something, not whether Unity says it.
#
# The one thing a test cannot cover is the C# side compiling. That needs
# an editor, and it is stated as an open item rather than implied by a
# green suite.

from __future__ import annotations

import json
import subprocess

import pytest

from backend.core import unity_ops


def unity_says(payload, *, stderr: str = "", returncode: int = 0, noise: bool = True):
    """A fake subprocess.run whose stdout is what Unity would print.

    The noise matters. Batchmode output is licence checks and asset
    imports, and the sentinels exist precisely because the answer has to
    be found inside all of it.
    """
    body = payload if isinstance(payload, str) else json.dumps(payload)
    head = ("Unity Editor version 2022.3.10f1\nLicence check...\n"
            "Refreshing native plugins...\n" if noise else "")
    tail = "\nAssetDatabase: 1 asset(s) imported.\n" if noise else ""

    stdout = head + unity_ops.RESULT_OPEN + body + unity_ops.RESULT_CLOSE + tail

    def fake_run(command, **kwargs):
        fake_run.calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    fake_run.calls = []
    return fake_run


@pytest.fixture
def unity(tmp_path, monkeypatch):
    """A project that looks like Unity, and an editor that is not run."""
    (tmp_path / "Assets").mkdir()
    editor = tmp_path / "Unity.exe"
    editor.write_text("not really unity", encoding="utf-8")

    monkeypatch.setenv(unity_ops.ENV_EDITOR, str(editor))
    monkeypatch.setenv(unity_ops.ENV_PROJECT, str(tmp_path))

    # The retry itself is under test; the three-second wait between
    # attempts is not, and paying it in a unit suite is nine seconds of
    # nothing happening.
    monkeypatch.setattr(unity_ops, "RETRY_PAUSE_SECONDS", 0)
    return tmp_path


# ======================================================
# The command it builds
# ======================================================

def test_the_command_is_a_fixed_argument_list_with_no_shell(unity, monkeypatch):
    """This is the one place ARIA starts a process on the user's machine.

    The executable comes from configuration, the method is one of ten,
    and the arguments are JSON in a single argv entry -- never
    interpolated into a command line, and never through a shell.
    """
    fake = unity_says({"ok": True, "value": "Player"})
    monkeypatch.setattr(subprocess, "run", fake)

    unity_ops.create_game_object("Player")

    command, kwargs = fake.calls[0]
    assert kwargs["shell"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == unity_ops.DEFAULT_TIMEOUT_SECONDS

    assert "-batchmode" in command
    assert "-quit" in command
    assert command[command.index("-executeMethod") + 1] == unity_ops.ENTRY_POINT

    payload = [a for a in command if a.startswith("-ariaArgs=")][0]
    sent = json.loads(payload[len("-ariaArgs="):])
    assert sent == {"method": "CreateGameObject", "args": {"name": "Player"}}


def test_every_operation_names_a_method_the_bridge_has(unity, monkeypatch):
    fake = unity_says({"ok": True, "value": "{}"})
    monkeypatch.setattr(subprocess, "run", fake)

    for name, handler in unity_ops.UNITY_COMMANDS.items():
        fake.calls.clear()
        arguments = {
            "unity.create_game_object": {"name": "X"},
            "unity.add_component": {"game_object_name": "X", "component_type": "Rigidbody"},
            "unity.create_prefab": {"prefab_path": "Assets/X.prefab", "game_object_name": "X"},
            "unity.create_scriptable_object": {"type_name": "T", "asset_path": "Assets/X.asset"},
            "unity.load_scene": {"scene_path": "Assets/X.unity"},
            "unity.save_scene": {},
            "unity.run_build": {"build_path": "build/game.exe"},
            "unity.import_asset": {"asset_path": "Assets/X.png"},
            "unity.set_serialized_field": {"game_object_name": "X", "component_type": "C",
                                           "field_name": "f", "value": "1"},
            "unity.get_scene_summary": {},
        }[name]

        handler(**arguments)
        payload = [a for a in fake.calls[0][0] if a.startswith("-ariaArgs=")][0]
        method = json.loads(payload[len("-ariaArgs="):])["method"]
        assert method in unity_ops.BRIDGE_METHODS, f"{name} asked for {method}"


# ======================================================
# Reading what Unity said
# ======================================================

def test_a_result_is_found_inside_unitys_noise(unity, monkeypatch):
    monkeypatch.setattr(subprocess, "run", unity_says({"ok": True, "value": "Player"}))

    outcome = unity_ops.create_game_object("Player")

    assert outcome == {"ok": True, "result": "Player", "error": None}


def test_a_refusal_from_the_bridge_is_returned_as_one(unity, monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        unity_says({"ok": False, "error": "No GameObject named 'X'."}))

    outcome = unity_ops.add_component("X", "Rigidbody")

    assert outcome["ok"] is False
    assert "No GameObject named" in outcome["error"]


def test_no_output_at_all_is_said_plainly(unity, monkeypatch):
    def silent(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", silent)

    outcome = unity_ops.save_scene()

    assert outcome["ok"] is False
    assert outcome["error"] == "Unity returned no output"


def test_malformed_json_between_the_sentinels_is_reported(unity, monkeypatch):
    monkeypatch.setattr(subprocess, "run", unity_says("{not json at all"))

    outcome = unity_ops.save_scene()

    assert outcome["ok"] is False
    assert "not valid JSON" in outcome["error"]


def test_output_with_no_sentinels_points_at_the_likely_cause(unity, monkeypatch):
    """Almost always the bridge is not in the project, or did not
    compile. Saying that is more use than "no result"."""
    def compiled_badly(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 1, "Compilation failed\n", "error CS0103\n")

    monkeypatch.setattr(subprocess, "run", compiled_badly)

    outcome = unity_ops.save_scene()

    assert outcome["ok"] is False
    assert "ARIAEditorBridge.cs" in outcome["error"]
    assert "CS0103" in outcome["details"]


def test_a_timeout_says_what_usually_causes_it(unity, monkeypatch):
    def slow(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", slow)

    outcome = unity_ops.save_scene()

    assert outcome["ok"] is False
    assert "recompiles" in outcome["error"]
    assert unity_ops.ENV_TIMEOUT in outcome["error"]


def test_the_scene_summary_is_parsed_rather_than_handed_back_as_text(unity, monkeypatch):
    summary = {"scene": "Main", "path": "Assets/Main.unity",
               "objects": [{"name": "Player", "path": "Player",
                            "active": True, "components": ["Transform"]}]}
    monkeypatch.setattr(subprocess, "run",
                        unity_says({"ok": True, "value": json.dumps(summary)}))

    outcome = unity_ops.get_scene_summary()

    assert outcome["ok"] is True
    assert outcome["result"]["objects"][0]["name"] == "Player"


def test_a_scene_summary_that_is_not_json_is_reported(unity, monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        unity_says({"ok": True, "value": "not json"}))

    outcome = unity_ops.get_scene_summary()

    assert outcome["ok"] is False
    assert "not valid JSON" in outcome["error"]


# ======================================================
# Retries
# ======================================================

def test_a_transient_failure_is_retried_once(unity, monkeypatch):
    """A project lock clears on its own. A rejected argument does not."""
    monkeypatch.setattr(unity_ops, "RETRY_PAUSE_SECONDS", 0)
    attempts = {"n": 0}

    def flaky(command, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return subprocess.CompletedProcess(command, 1, "", "another instance is running")
        return subprocess.CompletedProcess(
            command, 0,
            unity_ops.RESULT_OPEN + '{"ok":true,"value":"Player"}' + unity_ops.RESULT_CLOSE,
            "")

    monkeypatch.setattr(subprocess, "run", flaky)

    outcome = unity_ops.create_game_object("Player")

    assert outcome["ok"] is True
    assert attempts["n"] == 2


def test_a_refusal_is_not_retried(unity, monkeypatch):
    """The bridge ran and said no. Running it again says no again."""
    monkeypatch.setattr(unity_ops, "RETRY_PAUSE_SECONDS", 0)
    fake = unity_says({"ok": False, "error": "not a Component"})
    monkeypatch.setattr(subprocess, "run", fake)

    unity_ops.add_component("Player", "String")

    assert len(fake.calls) == 1


# ======================================================
# Paths
# ======================================================

@pytest.mark.parametrize("path,reason", [
    ("", "required"),
    ("   ", "required"),
    ("Player.prefab", "inside Assets/"),
    ("/Assets/Player.prefab", "absolute"),
    ("C:/Assets/Player.prefab", "absolute"),
    ("Assets/../secrets.prefab", "'..'"),
    ("Assets\\Player.prefab", "forward slashes"),
    ("Assets/", "must not end"),
    (" Assets/Player.prefab", "whitespace"),
])
def test_the_paths_that_are_refused(path, reason):
    problem = unity_ops.validate_asset_path(path)

    assert problem is not None, f"{path!r} should have been refused"
    assert reason in problem


def test_the_paths_that_are_accepted():
    assert unity_ops.validate_asset_path("Assets") is None
    assert unity_ops.validate_asset_path("Assets/ARIA/Player.prefab") is None
    assert unity_ops.validate_asset_path("Assets/a b/c-d_e.png") is None


def test_a_suffix_is_checked_when_one_is_required():
    assert unity_ops.validate_asset_path("Assets/X.asset", suffix=".prefab")
    assert unity_ops.validate_asset_path("Assets/X.prefab", suffix=".prefab") is None


def test_a_bad_path_never_starts_a_process(unity, monkeypatch):
    """The check exists on both sides on purpose. One that only ran
    inside the editor would run two minutes later than it needed to."""
    def must_not_run(command, **kwargs):
        raise AssertionError("Unity was started for a path that is not valid")

    monkeypatch.setattr(subprocess, "run", must_not_run)

    assert unity_ops.create_prefab("../x.prefab", "Player")["ok"] is False
    assert unity_ops.import_asset("C:/x.png")["ok"] is False
    assert unity_ops.load_scene("Scenes/Main.unity")["ok"] is False


def test_a_build_path_is_not_held_to_the_assets_rule(unity, monkeypatch):
    """A build that could only be written inside the project would be
    useless. Traversal is still refused."""
    fake = unity_says({"ok": True, "value": "Succeeded"})
    monkeypatch.setattr(subprocess, "run", fake)

    assert unity_ops.run_build("build/game.exe")["ok"] is True
    assert unity_ops.run_build("../../game.exe")["ok"] is False


# ======================================================
# Arguments
# ======================================================

@pytest.mark.parametrize("call", [
    lambda: unity_ops.create_game_object(""),
    lambda: unity_ops.add_component("", "Rigidbody"),
    lambda: unity_ops.add_component("Player", ""),
    lambda: unity_ops.create_scriptable_object("", "Assets/X.asset"),
    lambda: unity_ops.set_serialized_field("", "C", "f", "1"),
    lambda: unity_ops.set_serialized_field("X", "C", "", "1"),
])
def test_a_missing_argument_is_refused_before_unity_is_started(call, unity, monkeypatch):
    def must_not_run(command, **kwargs):
        raise AssertionError("Unity was started with a missing argument")

    monkeypatch.setattr(subprocess, "run", must_not_run)

    assert call()["ok"] is False


def test_an_empty_value_is_allowed_because_clearing_a_field_is_a_thing(unity, monkeypatch):
    fake = unity_says({"ok": True, "value": ""})
    monkeypatch.setattr(subprocess, "run", fake)

    assert unity_ops.set_serialized_field("X", "C", "label", "")["ok"] is True


def test_a_value_that_is_not_a_string_is_refused(unity, monkeypatch):
    assert unity_ops.set_serialized_field("X", "C", "f", 3)["ok"] is False


# ======================================================
# The routing surface
# ======================================================

def test_every_command_maps_to_a_callable():
    assert len(unity_ops.UNITY_COMMANDS) == 10
    for name, handler in unity_ops.UNITY_COMMANDS.items():
        assert name.startswith("unity.")
        assert callable(handler)


def test_an_unknown_command_lists_the_known_ones():
    outcome = unity_ops.run_unity_command("unity.explode")

    assert outcome["ok"] is False
    assert "unity.create_game_object" in outcome["details"]


def test_the_wrong_arguments_read_like_a_caller_mistake(unity):
    outcome = unity_ops.run_unity_command("unity.create_game_object", wrong="Player")

    assert outcome["ok"] is False
    assert "wrong arguments" in outcome["error"]


def test_run_unity_command_never_raises(unity, monkeypatch):
    """A caller across a routing boundary cannot catch usefully."""
    def explode(command, **kwargs):
        raise RuntimeError("something unexpected")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(unity_ops, "RETRY_PAUSE_SECONDS", 0)

    outcome = unity_ops.run_unity_command("unity.save_scene")

    assert outcome["ok"] is False
    assert outcome["result"] is None


def test_every_result_has_the_same_shape(unity, monkeypatch):
    """A caller that tells success from failure by which keys exist will
    eventually get it wrong."""
    monkeypatch.setattr(subprocess, "run", unity_says({"ok": True, "value": "X"}))
    good = unity_ops.run_unity_command("unity.save_scene")

    bad = unity_ops.run_unity_command("unity.create_game_object", name="")

    for outcome in (good, bad):
        assert set(outcome) >= {"ok", "result", "error"}
        assert isinstance(outcome["ok"], bool)


# ======================================================
# Where Unity is
# ======================================================

def test_a_configured_editor_that_is_not_there_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv(unity_ops.ENV_EDITOR, str(tmp_path / "nope.exe"))

    with pytest.raises(unity_ops.UnityUnavailable):
        unity_ops.editor_path()


def test_a_missing_editor_is_reported_and_not_guessed(tmp_path, monkeypatch):
    (tmp_path / "Assets").mkdir()
    monkeypatch.setenv(unity_ops.ENV_PROJECT, str(tmp_path))
    monkeypatch.delenv(unity_ops.ENV_EDITOR, raising=False)
    monkeypatch.setattr(unity_ops, "_hub_candidates", lambda: [])
    monkeypatch.setattr(unity_ops.shutil, "which", lambda name: None)

    outcome = unity_ops.save_scene()

    assert outcome["ok"] is False
    assert unity_ops.ENV_EDITOR in outcome["error"]


def test_a_folder_that_is_not_a_unity_project_is_refused(tmp_path, monkeypatch):
    editor = tmp_path / "Unity.exe"
    editor.write_text("x", encoding="utf-8")
    monkeypatch.setenv(unity_ops.ENV_EDITOR, str(editor))
    monkeypatch.setenv(unity_ops.ENV_PROJECT, str(tmp_path))  # no Assets/

    outcome = unity_ops.save_scene()

    assert outcome["ok"] is False
    assert "Unity project" in outcome["error"]


def test_the_timeout_is_configurable(unity, monkeypatch):
    monkeypatch.setenv(unity_ops.ENV_TIMEOUT, "600")
    fake = unity_says({"ok": True, "value": ""})
    monkeypatch.setattr(subprocess, "run", fake)

    unity_ops.save_scene()

    assert fake.calls[0][1]["timeout"] == 600


def test_a_nonsense_timeout_falls_back_rather_than_failing(unity, monkeypatch):
    monkeypatch.setenv(unity_ops.ENV_TIMEOUT, "soon")
    fake = unity_says({"ok": True, "value": ""})
    monkeypatch.setattr(subprocess, "run", fake)

    unity_ops.save_scene()

    assert fake.calls[0][1]["timeout"] == unity_ops.DEFAULT_TIMEOUT_SECONDS


# ======================================================
# The boundary this module must not cross
# ======================================================

def test_no_unity_operation_is_an_action_a_model_can_propose():
    """A model can write an action block; it cannot make one of these
    run. Wiring that up is a separate decision with its own consent
    question, and it has not been made."""
    from backend.core.action_plan import ACTION_TOOLS, parse_actions

    for name in unity_ops.UNITY_COMMANDS:
        assert name not in ACTION_TOOLS

    assert parse_actions(
        '```json\n{"tool": "unity.run_build", "path": "x"}\n```') == []


def test_the_method_name_can_only_be_one_of_ten(unity, monkeypatch):
    def must_not_run(command, **kwargs):
        raise AssertionError("Unity was started for a method that does not exist")

    monkeypatch.setattr(subprocess, "run", must_not_run)

    assert unity_ops._invoke("Eval", {})["ok"] is False
    assert unity_ops._invoke("System.Diagnostics.Process.Start", {})["ok"] is False
