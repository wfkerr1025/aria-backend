# backend/tests/test_unity_editor_bridge.py
#
# The Python half of the file-based Unity Editor Bridge.
#
# Nothing here runs Unity. The editor is played by a thread that does
# what Assets/ARIA/Editor/ARIAEditorBridge.cs does: waits for the
# commands file, consumes it, writes a results file with the same id. So
# every path -- a good answer, a refusal, a stale results file, a
# half-written one, silence -- runs on a machine with no editor.
#
# The C# side compiling and behaving is checked separately, against a
# real editor; a green run here says nothing about it.

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from aria import unity_editor_bridge as ueb


# ======================================================
# A stand-in editor
# ======================================================

@pytest.fixture
def project(tmp_path):
    root = tmp_path / "Proj"
    (root / "Assets").mkdir(parents=True)
    return root


@pytest.fixture
def bridge(project):
    return ueb.UnityEditorBridge(project, timeout=3, poll_interval=0.01)


def ok(command, **data):
    return {"command": command, "success": True, "message": "done", "data": data}


def fail(command, message):
    return {"command": command, "success": False, "message": message, "data": None}


class FakeUnity:
    """Consumes the commands file and answers it, the way the bridge does."""

    def __init__(self, bridge, reply=None, *, junk_first=False, delay=0.0):
        self.bridge = bridge
        self.reply = reply or (lambda commands: [ok(c["command"], index=i) for i, c in enumerate(commands)])
        self.junk_first = junk_first
        self.delay = delay
        self.seen = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.thread.join(timeout=5)

    def _run(self):
        deadline = time.monotonic() + 5
        while not self.bridge.commands_path.exists():
            if time.monotonic() > deadline:
                return
            time.sleep(0.005)
        time.sleep(self.delay)

        self.seen = json.loads(self.bridge.commands_path.read_text(encoding="utf-8"))
        self.bridge.commands_path.unlink()

        if self.junk_first:
            self.bridge.results_path.write_text('{"id": "', encoding="utf-8")
            time.sleep(0.05)

        results = self.reply(self.seen["commands"])
        envelope = {
            "id": self.seen["id"],
            "ok": all(r["success"] for r in results),
            "message": f"Ran {len(results)} command(s).",
            "unityVersion": "6000.3.6f1",
            "results": results,
        }
        self.bridge.results_path.write_text(json.dumps(envelope), encoding="utf-8")


# ======================================================
# Writing commands
# ======================================================

def test_write_commands_is_an_envelope_with_an_id(bridge):
    path = bridge.write_commands([ueb.make_command("Ping")], request_id="abc", stop_on_error=False)

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {"id": "abc", "stopOnError": False, "commands": [{"command": "Ping", "args": {}}]}
    assert not (bridge.folder / "unity_commands.json.tmp").exists(), "the temp file is renamed, not left"


def test_make_command_drops_unset_arguments_and_rejects_unknown_names():
    assert ueb.make_command("CreateGameObject", name="A", parent=None) == {
        "command": "CreateGameObject", "args": {"name": "A"}}
    with pytest.raises(ValueError, match="not a bridge command"):
        ueb.make_command("Explode")


def test_vectors_are_validated_before_anything_is_written(bridge):
    with pytest.raises(ValueError, match="position must have 3 numbers"):
        bridge.create_game_object("A", position=[1, 2])
    with pytest.raises(ValueError, match="rotation must have 3 or 4 numbers"):
        bridge.create_game_object("A", rotation=[1])
    with pytest.raises(ValueError, match="sequence of numbers"):
        bridge.set_transform("A", position="not, numbers, here" and [1, "x", 3])
    assert not bridge.commands_path.exists()


def test_set_field_keeps_a_null_value_and_lists_tuples():
    batch = ueb.Batch()
    batch.set_field("Player", "Rigidbody", "mass", None)
    batch.set_field("Player", "Rigidbody", "centerOfMass", (0, 1, 0))
    batch.set_field("Player", None, "active", False)

    assert batch.commands[0]["args"] == {"target": "Player", "componentType": "Rigidbody",
                                         "field": "mass", "value": None}
    assert batch.commands[1]["args"]["value"] == [0, 1, 0]
    assert "componentType" not in batch.commands[2]["args"]


# ======================================================
# The exchange
# ======================================================

def test_create_game_object_returns_the_bridge_data(bridge):
    def reply(commands):
        assert commands == [{"command": "CreateGameObject",
                             "args": {"name": "Floor", "position": [0.0, 0.0, 0.0], "primitive": "Plane"}}]
        return [ok("CreateGameObject", path="Floor", instanceId=42, globalId="gid:x")]

    with FakeUnity(bridge, reply):
        data = bridge.create_game_object("Floor", position=(0, 0, 0), primitive="Plane")

    assert data == {"path": "Floor", "instanceId": 42, "globalId": "gid:x"}
    assert not bridge.commands_path.exists(), "the editor consumed the file"
    assert bridge.last_results.unity_version == "6000.3.6f1"


def test_a_refused_command_raises_with_its_index_and_message(bridge):
    def reply(commands):
        return [ok("CreateGameObject"),
                fail("AddComponent", "No type named 'Rigidbodyy' in any loaded assembly."),
                fail("SetField", "Skipped: an earlier command failed and stopOnError is on.")]

    with ueb.UnityEditorBridge(bridge.project_root, timeout=3, poll_interval=0.01).batch() as batch:
        pass  # an empty batch sends nothing
    assert batch.results is None

    with FakeUnity(bridge, reply), pytest.raises(ueb.UnityBridgeError) as raised:
        with bridge.batch() as batch:
            batch.create_game_object("A")
            batch.add_component("A", "Rigidbodyy")
            batch.set_field("A", "Rigidbodyy", "mass", 2)

    error = raised.value
    assert error.command == "AddComponent"
    assert error.index == 1
    assert "command 2 of 3" in str(error)
    assert "Rigidbodyy" in str(error)
    assert [r.success for r in error.results] == [True, False, False]


def test_a_stale_results_file_from_another_request_is_ignored(bridge):
    bridge.folder.mkdir()
    bridge.results_path.write_text(json.dumps({"id": "old", "ok": True, "results": [ok("Ping", stale=True)]}))

    with FakeUnity(bridge, lambda commands: [ok("Ping", fresh=True)]):
        data = bridge.ping()

    assert data == {"fresh": True}


def test_a_half_written_results_file_is_waited_out(bridge):
    with FakeUnity(bridge, lambda commands: [ok("Ping", whole=True)], junk_first=True):
        assert bridge.ping() == {"whole": True}


def test_silence_is_a_timeout_that_says_what_to_check(bridge):
    bridge.timeout = 0.05
    with pytest.raises(ueb.UnityBridgeTimeout, match="did not pick up") as raised:
        bridge.ping()
    assert "Watch For Commands" in str(raised.value)
    assert bridge.pending(), "the commands file is left for a manual trigger"

    # Taken but never answered is the other message.
    bridge.commands_path.unlink()
    bridge.timeout = 0.05
    with pytest.raises(ueb.UnityBridgeTimeout, match="wrote no results"):
        bridge.wait_for_results("whatever")


def test_wait_off_writes_the_file_and_returns_none(bridge):
    bridge.wait = False
    assert bridge.create_game_object("Later") is None
    assert bridge.pending()

    written = json.loads(bridge.commands_path.read_text(encoding="utf-8"))
    assert written["commands"][0]["args"] == {"name": "Later"}

    # A per-call override works the other way too. The pending file is
    # cleared first: the stand-in editor would otherwise answer it.
    bridge.clear()
    with FakeUnity(bridge, lambda commands: [ok("Ping")]):
        assert bridge.send([ueb.make_command("Ping")], wait=True).ok


def test_batch_sends_everything_in_one_file(bridge):
    with FakeUnity(bridge) as unity:
        with bridge.batch() as batch:
            batch.create_game_object("Sun")
            batch.create_light("Directional", "Sun", rotation=(50, -30, 0), intensity=1.5, color="#ffeedd")
            batch.create_camera("Main Camera", position=(0, 5, -10), fov=70, clear_flags="SolidColor", main=True)

    assert [c["command"] for c in unity.seen["commands"]] == ["CreateGameObject", "CreateLight", "CreateCamera"]
    assert unity.seen["commands"][1]["args"] == {
        "type": "Directional", "name": "Sun", "rotation": [50.0, -30.0, 0.0],
        "intensity": 1.5, "color": "#ffeedd"}
    assert unity.seen["commands"][2]["args"]["main"] is True
    assert unity.seen["commands"][2]["args"]["clearFlags"] == "SolidColor"
    assert len(batch.results) == 3
    assert batch.results[2].data == {"index": 2}


def test_modify_prefab_takes_a_batch_of_operations():
    operations = ueb.Batch()
    operations.add_component("", "Rigidbody")
    operations.set_field("", "Rigidbody", "mass", 5)
    operations.create_game_object("Muzzle", position=(0, 0, 1))

    holder = ueb.Batch()
    holder.modify_prefab("Assets/Prefabs/Gun.prefab", operations, stop_on_error=False)

    args = holder.commands[0]["args"]
    assert args["prefabPath"] == "Assets/Prefabs/Gun.prefab"
    assert args["stopOnError"] is False
    assert [op["command"] for op in args["operations"]] == ["AddComponent", "SetField", "CreateGameObject"]


def test_clear_removes_both_files(bridge):
    bridge.write_commands([ueb.make_command("Ping")])
    bridge.folder.mkdir(exist_ok=True)
    bridge.results_path.write_text("{}")
    bridge.clear()
    assert not bridge.commands_path.exists()
    assert not bridge.results_path.exists()
    bridge.clear()  # and again, with nothing there


# ======================================================
# Finding the project, installing the bridge
# ======================================================

def test_project_root_comes_from_the_environment(project, monkeypatch):
    monkeypatch.setenv(ueb.ENV_PROJECT, str(project))
    assert ueb.find_project_root() == project.resolve()


def test_a_folder_without_assets_is_refused(tmp_path):
    with pytest.raises(ueb.UnityBridgeUnavailable, match="no Assets folder"):
        ueb.UnityEditorBridge(tmp_path)


def test_a_folder_holding_one_project_resolves_to_it(project):
    """The workspace is often the folder the projects live in, not a project."""
    assert ueb.find_project_root(project.parent) == project.resolve()


def test_a_folder_holding_several_projects_names_them(project):
    other = project.parent / "Other"
    (other / "Assets").mkdir(parents=True)

    with pytest.raises(ueb.UnityBridgeUnavailable, match="'Other', 'Proj'"):
        ueb.find_project_root(project.parent)


def test_install_copies_the_real_bridge_source_once(bridge):
    assert ueb.BRIDGE_SOURCE.is_file(), "the C# bridge must ship in the repo"
    assert not bridge.is_installed()

    installed = bridge.install_bridge()

    assert installed == bridge.project_root / "Assets" / "ARIA" / "Editor" / "ARIAEditorBridge.cs"
    assert installed.read_bytes() == ueb.BRIDGE_SOURCE.read_bytes()
    assert bridge.is_installed()

    first_write = installed.stat().st_mtime_ns
    bridge.install_bridge()
    assert installed.stat().st_mtime_ns == first_write, "an identical file is not rewritten"


def test_the_shipped_bridge_dispatches_every_command_the_client_knows():
    """The two sides list the same commands, or one of them is lying."""
    source = ueb.BRIDGE_SOURCE.read_text(encoding="utf-8")
    for command in ueb.COMMANDS:
        assert f'case "{command}":' in source, f"{command} has no case in the C# dispatch"


# ======================================================
# Headless
# ======================================================

def test_send_headless_runs_unity_and_reads_the_results(bridge, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        envelope = json.loads(bridge.commands_path.read_text(encoding="utf-8"))
        bridge.commands_path.unlink()
        bridge.results_path.write_text(json.dumps({
            "id": envelope["id"], "ok": True, "message": "",
            "results": [ok("Ping", headless=True)]}))

    monkeypatch.setattr(ueb.subprocess, "run", fake_run)

    results = bridge.send_headless([ueb.make_command("Ping")], unity_path="C:/Unity/Unity.exe")

    assert results[0].data == {"headless": True}
    command = calls[0]
    assert Path(command[0]) == Path("C:/Unity/Unity.exe")
    assert "-batchmode" in command and "-quit" in command
    assert command[command.index("-executeMethod") + 1] == ueb.ENTRY_POINT
    assert command[command.index("-projectPath") + 1] == str(bridge.project_root)


def test_send_headless_without_results_names_the_log(bridge, monkeypatch):
    monkeypatch.setattr(ueb.subprocess, "run", lambda command, **kwargs: None)
    with pytest.raises(ueb.UnityBridgeError, match="wrote no results") as raised:
        bridge.send_headless([ueb.make_command("Ping")], unity_path="unity")
    assert "unity_headless.log" in str(raised.value)


# ======================================================
# The router
# ======================================================

def commands_for(text):
    return [(c["command"], c["args"]) for c in ueb.parse_unity_command(text)]


def test_router_creates_primitives_with_names_and_positions():
    assert commands_for("Create a floor plane at y=0") == [
        ("CreateGameObject", {"name": "Floor", "primitive": "Plane", "position": [0.0, 0.0, 0.0]})]
    assert commands_for('create a cube named "Crate" at (1, 2.5, -3)') == [
        ("CreateGameObject", {"name": "Crate", "primitive": "Cube", "position": [1.0, 2.5, -3.0]})]
    assert commands_for("make an empty game object called Root") == [
        ("CreateGameObject", {"name": "Root"})]
    assert commands_for("create a sphere named Ball under Root at 0,1,0 scale 2") == [
        ("CreateGameObject", {"name": "Ball", "primitive": "Sphere", "parent": "Root",
                              "position": [0.0, 1.0, 0.0], "scale": 2.0})]


def test_router_lights_and_cameras():
    assert commands_for("create a directional light named Sun intensity 1.5 color #ffeedd") == [
        ("CreateLight", {"type": "Directional", "name": "Sun", "intensity": 1.5, "color": "#ffeedd"})]
    assert commands_for("add a point light at 0, 3, 0") == [
        ("CreateLight", {"type": "Point", "name": "Point Light", "position": [0.0, 3.0, 0.0]})]
    assert commands_for("create a main camera at 0,5,-10 fov 70 with a solid color background") == [
        ("CreateCamera", {"name": "Main Camera", "position": [0.0, 5.0, -10.0], "fov": 70.0,
                          "clearFlags": "SolidColor", "main": True})]


def test_router_components_fields_and_transforms():
    assert commands_for("add a rigidbody to Crate") == [
        ("AddComponent", {"target": "Crate", "componentType": "Rigidbody"})]
    assert commands_for("attach a box collider to the Floor") == [
        ("AddComponent", {"target": "Floor", "componentType": "BoxCollider"})]
    assert commands_for("remove the Rigidbody from Crate") == [
        ("RemoveComponent", {"target": "Crate", "componentType": "Rigidbody"})]
    assert commands_for("set mass of Crate Rigidbody to 5") == [
        ("SetField", {"target": "Crate", "componentType": "Rigidbody", "field": "mass", "value": 5})]
    assert commands_for("set the intensity of Sun to 0.8") == [
        ("SetField", {"target": "Sun", "componentType": "Light", "field": "intensity", "value": 0.8})]
    assert commands_for("set Crate Rigidbody useGravity to false") == [
        ("SetField", {"target": "Crate", "componentType": "Rigidbody", "field": "useGravity", "value": False})]
    assert commands_for("disable Crate") == [("SetField", {"target": "Crate", "field": "active", "value": False})]
    assert commands_for("move Crate to 0, 1, 0") == [("SetTransform", {"target": "Crate", "position": [0.0, 1.0, 0.0]})]
    assert commands_for("rotate the Crate to 0,90,0") == [("SetTransform", {"target": "Crate", "rotation": [0.0, 90.0, 0.0]})]
    assert commands_for("scale Crate to 2") == [("SetTransform", {"target": "Crate", "scale": 2.0})]
    assert commands_for("delete Crate") == [("DeleteGameObject", {"target": "Crate"})]


def test_router_scenes_prefabs_and_hierarchy():
    assert commands_for("open scene Main") == [("OpenScene", {"path": "Assets/Scenes/Main.unity"})]
    assert commands_for("load the scene Assets/Levels/One.unity") == [("OpenScene", {"path": "Assets/Levels/One.unity"})]
    assert commands_for("save the scene") == [("SaveScene", {})]
    assert commands_for("save scene as Boot") == [("SaveScene", {"path": "Assets/Scenes/Boot.unity"})]
    assert commands_for("make a prefab from Crate") == [
        ("CreatePrefab", {"target": "Crate", "prefabPath": "Assets/Prefabs/Crate.prefab"})]
    assert commands_for("create a prefab of Root/Crate at Assets/Stuff/Box.prefab") == [
        ("CreatePrefab", {"target": "Root/Crate", "prefabPath": "Assets/Stuff/Box.prefab"})]
    assert commands_for("what's in the scene?") == [("GetHierarchy", {})]
    assert commands_for("ping") == [("Ping", {})]


def test_router_chains_clauses_and_resolves_it():
    commands = commands_for("create a cube named Crate at 0,1,0, then add a rigidbody to it and set mass of it Rigidbody to 3")
    assert commands == [
        ("CreateGameObject", {"name": "Crate", "primitive": "Cube", "position": [0.0, 1.0, 0.0]}),
        ("AddComponent", {"target": "Crate", "componentType": "Rigidbody"}),
        ("SetField", {"target": "Crate", "componentType": "Rigidbody", "field": "mass", "value": 3}),
    ]


def test_router_refuses_what_it_does_not_understand():
    with pytest.raises(ueb.UnroutableCommand, match="Could not map"):
        ueb.parse_unity_command("bake the lighting and email me")
    with pytest.raises(ueb.UnroutableCommand):
        ueb.parse_unity_command("   ")
    # One bad clause fails the whole description: nothing is half-sent.
    with pytest.raises(ueb.UnroutableCommand):
        ueb.parse_unity_command("create a cube then do something clever")


def test_run_unity_command_sends_and_returns_results(bridge):
    with FakeUnity(bridge) as unity:
        results = ueb.run_unity_command("create a cube named Crate then add a rigidbody to it", bridge)

    assert [c["command"] for c in unity.seen["commands"]] == ["CreateGameObject", "AddComponent"]
    assert [r.command for r in results] == ["CreateGameObject", "AddComponent"]
    assert all(r.success for r in results)


def test_module_functions_use_the_configured_bridge(project, monkeypatch):
    monkeypatch.setattr(ueb, "_default", None)
    configured = ueb.configure(project, timeout=3)
    configured.poll_interval = 0.01
    assert ueb.get_bridge() is configured

    with FakeUnity(configured, lambda commands: [ok("GetField", value=7.5)]):
        assert ueb.get_field("Crate", "Rigidbody", "mass") == 7.5


def test_cli_installs_and_reports(project, capsys):
    assert ueb.main(["--project", str(project), "--install"]) == 0
    assert "installed" in capsys.readouterr().out
    assert (project / "Assets" / "ARIA" / "Editor" / "ARIAEditorBridge.cs").is_file()

    assert ueb.main(["--project", str(project), "--no-wait", "create", "a", "cube"]) == 0
    assert "Run Bridge Commands" in capsys.readouterr().out

    assert ueb.main(["--project", str(project), "do", "a", "backflip"]) == 1
    assert "Could not map" in capsys.readouterr().err
