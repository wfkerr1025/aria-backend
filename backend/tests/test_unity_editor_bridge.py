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
import re
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


# ======================================================
# Looking at the result
# ======================================================

def test_refresh_assets_without_a_path_refreshes_everything(bridge):
    def reply(commands):
        assert commands == [{"command": "RefreshAssets", "args": {}}]
        return [ok("RefreshAssets", scope="all")]

    with FakeUnity(bridge, reply):
        assert bridge.refresh_assets() == {"scope": "all"}


def test_refresh_assets_reports_what_one_file_became(bridge):
    """The reporting half is the whole reason the command exists.

    A tool that writes a .png and a hand-rolled .meta beside it cannot see
    whether the pair imported as a Sprite or as a plain Texture. The file
    is on disk either way and the mistake surfaces as an empty square in a
    running game, hours later.
    """
    def reply(commands):
        assert commands == [{"command": "RefreshAssets",
                             "args": {"path": "Assets/UI/item.png", "force": True}}]
        return [ok("RefreshAssets", path="Assets/UI/item.png", guid="abc123",
                   type="Texture2D", importer="TextureImporter",
                   textureType="Sprite", spriteMode="Single",
                   sprites=[{"name": "item", "rect": "256x256"}])]

    with FakeUnity(bridge, reply):
        data = bridge.refresh_assets("Assets/UI/item.png", force=True)

    assert data["guid"] == "abc123"
    assert data["textureType"] == "Sprite"
    assert [s["name"] for s in data["sprites"]] == ["item"]


def test_screenshot_defaults_to_the_game_view_and_returns_a_path(bridge):
    def reply(commands):
        assert commands == [{"command": "Screenshot", "args": {"width": 1600, "height": 900}}]
        return [ok("Screenshot", path="ARIA/shots/shot_20260910_2131.png",
                   width=1600, height=900, view="game", canvases=2)]

    with FakeUnity(bridge, reply):
        data = bridge.screenshot(width=1600, height=900)

    assert data["path"].endswith(".png")
    assert data["view"] == "game"
    assert data["canvases"] == 2, "overlay canvases were borrowed for the frame"


def test_screenshot_passes_the_scene_view_through(bridge):
    def reply(commands):
        assert commands[0]["args"]["view"] == "scene"
        return [ok("Screenshot", view="scene")]

    with FakeUnity(bridge, reply):
        assert bridge.screenshot(view="scene")["view"] == "scene"


def test_set_play_mode_says_what_it_will_be_rather_than_what_it_is(bridge):
    """It answers before the change, because the change destroys the answerer.

    Entering play mode reloads the C# domain and throws away everything
    holding the call, so the result is written first and the play mode
    lands after. A caller that waited for "isPlaying": true would wait
    forever.
    """
    def reply(commands):
        assert commands == [{"command": "SetPlayMode", "args": {"playing": True}}]
        return [ok("SetPlayMode", was=False, willBe=True, changed=True)]

    with FakeUnity(bridge, reply):
        data = bridge.set_play_mode(True)

    assert data["was"] is False and data["willBe"] is True


def test_stopping_is_the_same_command(bridge):
    def reply(commands):
        assert commands[0]["args"] == {"playing": False}
        return [ok("SetPlayMode", was=True, willBe=False, changed=True)]

    with FakeUnity(bridge, reply):
        assert bridge.set_play_mode(False)["changed"] is True


def test_the_three_new_commands_are_batchable(bridge):
    """The sequence that closes the loop: run it, photograph it, read it back."""
    def reply(commands):
        assert [c["command"] for c in commands] == ["SetPlayMode", "Screenshot", "RefreshAssets"]
        return [ok(c["command"], index=i) for i, c in enumerate(commands)]

    with FakeUnity(bridge, reply) as unity:
        with bridge.batch() as batch:
            batch.set_play_mode(True)
            batch.screenshot("ARIA/shots/book.png")
            batch.refresh_assets("Assets/UI/item.png")

    assert len(unity.seen["commands"]) == 3


def test_the_play_safe_list_is_the_same_on_both_sides():
    """The client must not promise a command in play mode the editor will not run.

    Found on the live editor: SetPlayMode(True) went through, and then the
    bridge answered nothing at all -- no ping, no screenshot, not even the
    command to stop -- because it refused everything while playing. What a
    playing editor runs now lives in both halves, and this holds them
    together.
    """
    source = ueb.BRIDGE_SOURCE.read_text(encoding="utf-8")
    block = re.search(r"PlaySafeCommands\s*=\s*new HashSet<string>\([^)]*\)\s*\{(?P<body>[^}]*)\}",
                      source)
    assert block, "PlaySafeCommands not found in the C# bridge"

    listed = set(re.findall(r'"(\w+)"', block.group("body")))
    assert listed == set(ueb.PLAY_SAFE_COMMANDS)
    assert ueb.PLAY_SAFE_COMMANDS <= ueb.COMMANDS
    assert "SetPlayMode" in ueb.PLAY_SAFE_COMMANDS, "a playing editor must always be stoppable"


def test_nothing_that_edits_a_scene_is_play_safe():
    """Play mode throws edits away on exit; a bridge must not report them as done."""
    editing = {"CreateGameObject", "DeleteGameObject", "AddComponent", "RemoveComponent",
               "SetTransform", "SetField", "OpenScene", "SaveScene", "CreatePrefab",
               "ModifyPrefab", "InstantiatePrefab", "CreateLight", "CreateCamera",
               "RefreshAssets"}
    assert not (editing & ueb.PLAY_SAFE_COMMANDS)


def test_the_play_mode_overrides_are_spelled_the_way_the_editor_reads_them(bridge):
    def reply(commands):
        assert commands[0]["args"] == {"playing": True, "testSave": False, "seed": "fresh",
                                       "allowUnfocused": True, "skipSnapshot": True}
        return [ok("SetPlayMode", changed=True)]

    with FakeUnity(bridge, reply):
        bridge.set_play_mode(True, test_save=False, seed="fresh",
                             allow_unfocused=True, skip_snapshot=True)

    source = ueb.BRIDGE_SOURCE.read_text(encoding="utf-8")
    assert 'Bool(args, "allowUnfocused", false)' in source
    assert 'Bool(args, "skipSnapshot", false)' in source
    assert 'Bool(args, "testSave", true)' in source, "the test save is on unless refused"
    assert 'Str(args, "seed")' in source


def test_entering_play_mode_checks_focus_and_copies_the_save_by_default():
    """What once destroyed a real save: an unfocused start with no copy taken."""
    source = ueb.BRIDGE_SOURCE.read_text(encoding="utf-8")
    handler = source[source.index("private static CommandResult SetPlayMode("):]
    handler = handler[:handler.index("#endregion")]

    assert "InternalEditorUtility.isApplicationActive" in handler
    assert "SnapshotPersistentData(" in handler
    assert handler.index("SnapshotPersistentData(") < handler.index("EditorApplication.EnterPlaymode()"), \
        "the copy has to exist before the game can write"


def _csharp_method(source, signature):
    body = source[source.index(signature):]
    return body[:body.index("\n        }\n") + 10]


def test_the_test_save_variable_is_the_one_the_game_reads():
    """Spelled once on each side of a process boundary nothing type-checks."""
    source = ueb.BRIDGE_SOURCE.read_text(encoding="utf-8")
    assert f'TestSaveVariable = "{ueb.TEST_SAVE_VARIABLE}"' in source
    assert f'LastPlayFileName = "{ueb.LAST_PLAY_FILE}"' in source


def test_the_test_save_is_named_and_the_real_save_listed_before_the_game_starts():
    """The game reads the variable in its first frame; set after, it is set too late."""
    source = ueb.BRIDGE_SOURCE.read_text(encoding="utf-8")
    handler = source[source.index("private static CommandResult SetPlayMode("):]
    handler = handler[:handler.index("#endregion")]
    start = handler.index("EditorApplication.EnterPlaymode()")

    assert handler.index("Environment.SetEnvironmentVariable(TestSaveVariable") < start
    assert handler.index("RealSaveManifest()") < start, "what the real save was must be known first"
    assert handler.index("PendingStartKey") < start, "the session must be claimable as the bridge's"


def test_a_person_pressing_play_is_never_a_test_session():
    """A folder left named by a request that never started must not catch a real game.

    Only a SetPlayMode from moments earlier claims a session; anything else
    clears the variable as play begins, and every session's end clears it.
    """
    source = ueb.BRIDGE_SOURCE.read_text(encoding="utf-8")
    changed = _csharp_method(source, "private static void OnPlayModeChanged(")
    assert "PlayModeStateChange.ExitingEditMode" in changed and "ClaimOrDisownStart()" in changed
    assert "PlayModeStateChange.EnteredEditMode" in changed and "EndSession()" in changed

    claim = _csharp_method(source, "private static void ClaimOrDisownStart()")
    assert "StartPending()" in claim
    assert "if (!claimed) EndTestSave();" in claim

    end = _csharp_method(source, "private static void EndSession()")
    assert end.index("EndTestSave()") < end.index("if (!wasDriving) return;"), \
        "the variable is cleared whoever started the session"


def test_a_test_session_keeps_running_unfocused_and_hands_the_setting_back(bridge):
    """With Run In Background off an unfocused editor plays no frames at all.

    Measured in a lab editor minimized mid-play: 456 frames, then 456 three
    seconds later. The bridge is used while the person is somewhere else, so
    its own sessions set the flag for their length -- in the editor that is
    Player Settings' value too -- and give the old value back as play ends.
    """
    def reply(commands):
        assert commands[0]["args"] == {"playing": True, "keepRunning": False}
        return [ok("SetPlayMode", changed=True)]

    with FakeUnity(bridge, reply):
        bridge.set_play_mode(True, keep_running=False)

    source = ueb.BRIDGE_SOURCE.read_text(encoding="utf-8")
    assert 'Bool(args, "keepRunning", true)' in source

    changed = _csharp_method(source, "private static void OnPlayModeChanged(")
    assert "PlayModeStateChange.EnteredPlayMode) BeginDrivenPlay()" in changed
    assert "PlayModeStateChange.ExitingPlayMode) EndDrivenPlay()" in changed

    begin = _csharp_method(source, "private static void BeginDrivenPlay()")
    assert begin.index("if (!Driving") < begin.index("Application.runInBackground = true"), \
        "only the bridge's own test sessions"
    assert begin.index("_wasRunningInBackground = Application.runInBackground") < \
        begin.index("Application.runInBackground = true"), "the old value is kept before it is changed"

    end = _csharp_method(source, "private static void EndDrivenPlay()")
    assert "Application.runInBackground = _wasRunningInBackground" in end


def test_send_input_sends_its_actions_in_order(bridge):
    def reply(commands):
        assert commands == [{"command": "SendInput", "args": {"actions": [
            {"key": "space", "times": 3},
            {"key": ["LeftCtrl", "s"]},
            {"click": [640.0, 360.0], "button": "right"},
            {"click": "Canvas/Buy"},
            {"wait": 10},
            {"text": "hi"},
            {"scroll": -120},
        ]}}]
        return [ok("SendInput", queued=14, framesNeeded=30)]

    with FakeUnity(bridge, reply):
        data = bridge.send_input(
            ueb.Inputs.key("space", times=3),
            ueb.Inputs.key(["LeftCtrl", "s"]),
            ueb.Inputs.click((640, 360), button="right"),
            ueb.Inputs.click("Canvas/Buy"),
            ueb.Inputs.wait(10),
            ueb.Inputs.text("hi"),
            ueb.Inputs.scroll(-120))

    assert data["queued"] == 14


def test_input_builders_refuse_what_the_editor_would():
    assert ueb.Inputs.click((0.5, 0.5), viewport=True) == {"click": [0.5, 0.5], "space": "viewport"}
    assert ueb.Inputs.mouse("down", (10, 20)) == {"mouse": "down", "at": [10.0, 20.0]}
    assert ueb.Inputs.wait(seconds=0.5) == {"waitSeconds": 0.5}

    with pytest.raises(ValueError):
        ueb.Inputs.click((1, 2, 3))
    with pytest.raises(ValueError):
        ueb.Inputs.wait()


def test_send_input_needs_something_to_send(bridge):
    with pytest.raises(ValueError):
        bridge.send_input()


def test_wait_for_input_waits_for_the_queue_to_drain(bridge, monkeypatch):
    pings = iter([{"inputPending": 5, "frame": 10}, {"inputPending": 2, "frame": 12},
                  {"inputPending": 0, "frame": 14}])
    monkeypatch.setattr(bridge, "ping", lambda: next(pings))

    assert bridge.wait_for_input(timeout=2)["frame"] == 14


def test_a_queue_that_never_drains_says_why(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "ping", lambda: {"inputPending": 3, "frame": 99})

    with pytest.raises(ueb.UnityBridgeTimeout, match="advancing"):
        bridge.wait_for_input(timeout=0.05)


def test_read_screen_asks_where_targets_are_drawn(bridge):
    def reply(commands):
        assert commands[0]["args"] == {"targets": ["LabCube"], "includeHidden": True}
        return [ok("ReadScreen", lines=["Score: 0"], texts=[], controls=[])]

    with FakeUnity(bridge, reply):
        assert bridge.read_screen("LabCube", include_hidden=True)["lines"] == ["Score: 0"]


def test_what_plays_the_game_refuses_the_real_save():
    """Input changes the game and the game saves what changes."""
    source = ueb.BRIDGE_SOURCE.read_text(encoding="utf-8")
    refuse = _csharp_method(source, "private static CommandResult RefuseUnlessDriving(")
    assert "if (Driving || Bool(args, \"allowRealSave\", false)) return null;" in refuse

    send = source[source.index("private static CommandResult SendInput("):]
    send = send[:send.index("private static CommandResult ReadScreen(")]
    assert send.index("RefuseUnlessDriving(args, \"SendInput\")") < send.index("PlanAction("), \
        "refused before anything is queued"


def test_one_action_never_lands_in_the_same_frame_as_the_last():
    """Measured: a button's release and the move away to the next target,
    sent in one update, read as a release somewhere else -- not a click."""
    source = ueb.BRIDGE_SOURCE.read_text(encoding="utf-8")
    send = source[source.index("private static CommandResult SendInput("):]
    send = send[:send.index("private static CommandResult ReadScreen(")]

    planned = send.index("PlanAction(")
    spaced = send.index("steps[first].Frames = Math.Max(1, steps[first].Frames)")
    assert planned < spaced
    assert "bool follows = first > 0 || _input.Count > 0;" in send, \
        "a queue already playing counts as something to follow"


def test_input_reaches_the_game_whatever_has_focus_and_only_for_a_test():
    """Read in the Input System: with the Game view unfocused, keyboard and
    pointer events are held for the editor and the game never sees them.
    For a test session the settings say otherwise -- on a copy, handed back."""
    source = ueb.BRIDGE_SOURCE.read_text(encoding="utf-8")

    route = _csharp_method(source, "private static void RouteInputToGame()")
    assert "Object.Instantiate(current)" in route, "a copy, never the project's settings object"
    assert "BackgroundBehavior.IgnoreFocus" in route
    assert "AllDeviceInputAlwaysGoesToGameView" in route

    begin = _csharp_method(source, "private static void BeginDrivenPlay()")
    assert begin.index("if (!Driving) return;") < begin.index("RouteInputToGame()")

    restore = _csharp_method(source, "private static void RestoreInput()")
    assert "RemoveAriaDevices()" in restore
    assert "InputSystem.settings = _originalInputSettings" in restore
    assert "RestoreInput()" in _csharp_method(source, "private static void EndDrivenPlay()")

    remove = _csharp_method(source, "private static void RemoveAriaDevices()")
    assert "StartsWith(AriaDevicePrefix" in remove, "only the bridge's own devices are ever removed"


def test_get_log_reads_what_is_new_and_narrows_it(bridge):
    def reply(commands):
        assert commands[0]["args"] == {"since": 41, "types": ["errors"], "contains": "Null"}
        return [ok("GetLog", entries=[{"seq": 42, "type": "exception", "message": "NullReferenceException"}],
                   next=42, errors=1)]

    with FakeUnity(bridge, reply):
        data = bridge.get_log(41, types="errors", contains="Null")

    assert data["next"] == 42 and data["errors"] == 1


def test_get_log_can_start_from_the_play_session(bridge):
    def reply(commands):
        assert commands[0]["args"] == {"session": True, "stack": False}
        return [ok("GetLog", entries=[], next=7, errors=0)]

    with FakeUnity(bridge, reply):
        bridge.get_log(session=True, stack=False)


def test_the_console_is_heard_from_the_start_and_carried_across_reloads():
    """A null reference in a panel that never opened was in the console all
    along, where nothing outside the editor could see it."""
    source = ueb.BRIDGE_SOURCE.read_text(encoding="utf-8")
    start = source[source.index("static ARIAEditorBridge()"):]
    start = start[:start.index("\n        }\n")]

    assert "Application.logMessageReceivedThreaded += OnLogMessage" in start, \
        "threaded: a game logs from other threads too"
    assert "AssemblyReloadEvents.beforeAssemblyReload += SaveLog" in start
    assert start.index("RestoreLog()") < start.index("logMessageReceivedThreaded"), \
        "what was carried over is put back before anything new arrives"
    assert "CompilationPipeline.assemblyCompilationFinished += OnAssemblyCompiled" in start, \
        "compile errors never reach the console as log messages"

    handler = _csharp_method(source, "private static void OnLogMessage(")
    assert handler.index("bool main =") < handler.index("Time.frameCount"), \
        "frame and play state are only read on the main thread"


def test_chatter_cannot_push_a_fault_out_of_the_log():
    """Measured: two audio listeners, reported every frame, pushed an
    exception out of a 500-entry buffer in under a second."""
    source = ueb.BRIDGE_SOURCE.read_text(encoding="utf-8")
    add = _csharp_method(source, "private static void AddLog(")
    assert "last.Repeat++" in add, "the same message straight after itself is counted, not kept twice"
    assert add.index("if (IsError(kind))") < add.index("_faults.Add(entry)"), "faults are kept apart"

    get = source[source.index("private static CommandResult GetLog("):]
    get = get[:get.index("#endregion")]
    assert "KeptEntries()" in get, "a read sees the faults the log has let go of"

    assert "foreach (LogEntry entry in _faults)" in _csharp_method(source, "private static int ErrorsSince(")


def test_a_burst_is_a_screenshot_with_a_count(bridge):
    def reply(commands):
        assert commands[0] == {"command": "Screenshot", "args": {
            "path": "ARIA/shots/charm", "count": 8, "everySeconds": 0.15, "columns": 4}}
        return [ok("Screenshot", burst=True, manifest="ARIA/shots/charm.json")]

    with FakeUnity(bridge, reply):
        data = bridge.screenshot("ARIA/shots/charm", count=8, every_seconds=0.15, columns=4)

    assert data["burst"] is True


def test_wait_for_burst_reads_the_list_of_shots(bridge, monkeypatch):
    shots = bridge.project_root / "ARIA" / "shots"
    shots.mkdir(parents=True)
    (shots / "charm.json").write_text(json.dumps({
        "taken": 2, "count": 2, "sheet": "ARIA/shots/charm_sheet.png",
        "shots": [{"path": "ARIA/shots/charm_00.png", "frame": 10},
                  {"path": "ARIA/shots/charm_01.png", "frame": 40}]}), encoding="utf-8")

    pings = iter([{"burstPending": 1, "lastBurst": ""},
                  {"burstPending": 0, "lastBurst": "ARIA/shots/charm.json"}])
    monkeypatch.setattr(bridge, "ping", lambda: next(pings))

    manifest = bridge.wait_for_burst(timeout=2)
    assert manifest["taken"] == 2
    assert manifest["sheetPath"].endswith("charm_sheet.png")
    assert manifest["shots"][1]["absolutePath"].endswith("charm_01.png")


def test_a_burst_needs_a_playing_game_and_takes_turns():
    source = ueb.BRIDGE_SOURCE.read_text(encoding="utf-8")
    start = source[source.index("private static CommandResult StartBurst("):]
    start = start[:start.index("private static void PumpBurst()")]
    assert start.index("if (!EditorApplication.isPlaying)") < start.index("_burst = new Burst")
    assert start.index("if (_burst != null)") < start.index("_burst = new Burst"), "one burst at a time"

    pump = _csharp_method(source, "private static void Pump()")
    assert pump.index("PumpBurst();") < pump.index("#if ENABLE_INPUT_SYSTEM"), \
        "bursts run in a project without the Input System too"

    finish = _csharp_method(source, "private static void FinishBurst()")
    assert "Object.DestroyImmediate(frame)" in finish, "the textures are let go"


def test_a_single_screenshot_and_a_burst_are_taken_the_same_way():
    source = ueb.BRIDGE_SOURCE.read_text(encoding="utf-8")
    single = source[source.index("private static CommandResult Screenshot("):]
    single = single[:single.index("private static Camera FindCaptureCamera(")]
    assert "CaptureFrame(camera, width, height, wantsScene, out canvases)" in single
    assert single.index("path += \".png\"") < single.index("FindCaptureCamera("), \
        "a refused path cannot leave a borrowed camera behind"

    pump = _csharp_method(source, "private static void PumpBurst()")
    assert "CaptureFrame(" in pump and "FindCaptureCamera(" in pump


def test_a_session_report_says_whether_anything_went_wrong():
    source = ueb.BRIDGE_SOURCE.read_text(encoding="utf-8")
    end = _csharp_method(source, "private static void EndSession()")
    assert 'report["errors"] = ErrorsSince(logStart)' in end
    assert 'report["firstErrors"]' in end

    handler = source[source.index("private static CommandResult SetPlayMode("):]
    handler = handler[:handler.index("#endregion")]
    assert handler.index("SessionLogStartKey") < handler.index("EditorApplication.EnterPlaymode()"), \
        "where the log stood is recorded before the game can say anything"


def test_emptying_the_test_folder_is_confined_to_the_bridge_folder():
    source = ueb.BRIDGE_SOURCE.read_text(encoding="utf-8")
    prepare = source[source.index("private static Dictionary<string, object> PrepareTestSave("):]
    prepare = prepare[:prepare.index("private static int CopyRealSave(")]
    assert prepare.index("StartsWith(Path.GetFullPath(BridgeFolder)") < prepare.index("Directory.Delete(")


def test_last_play_reads_the_report_the_session_left(bridge):
    assert bridge.last_play() is None, "no session yet"

    bridge.folder.mkdir(parents=True, exist_ok=True)
    report = bridge.folder / ueb.LAST_PLAY_FILE
    report.write_text(json.dumps({"realSaveSafe": True, "realSaveTouched": [],
                                  "testSave": "ARIA/testsave"}), encoding="utf-8")
    assert bridge.last_play()["realSaveSafe"] is True

    report.write_text('{"realSave', encoding="utf-8")
    assert bridge.last_play() is None, "half a report is no report"
