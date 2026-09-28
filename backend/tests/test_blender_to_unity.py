"""Sending a rigged character from Blender into Unity in one step.

Unity itself is not started here: the rules around it are -- which rig,
which project, whether the project is open, what lands on disk and in
what order, and what chat takes as a request. The Unity half
(ARIACharacterImport.cs) was run for real against a closed project in
batch mode; see backend/blender/blender_to_unity.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.blender import blender_script_templates as templates
from backend.blender import blender_session
from backend.blender import blender_to_unity as to_unity

BODY = {"name": "Body", "type": "MESH", "parent": "Body_Rig"}
RIG = {"name": "Body_Rig", "type": "ARMATURE"}


def scene(*objects):
    return {"objects": list(objects)}


def test_the_rig_and_the_mesh_it_moves_are_the_character():
    assert to_unity.pick_rig(scene(RIG, BODY, {"name": "Light", "type": "LIGHT"})) == ("Body_Rig", "Body")


def test_naming_the_mesh_finds_its_rig():
    other = {"name": "Other_Rig", "type": "ARMATURE"}
    second = {"name": "Knight", "type": "MESH", "parent": "Other_Rig"}
    assert to_unity.pick_rig(scene(RIG, BODY, other, second), "Knight") == ("Other_Rig", "Knight")


@pytest.mark.parametrize("objects, says", [
    ((BODY | {"parent": None},), "rig him"),
    ((), "nothing in the scene"),
    ((RIG, BODY, {"name": "Other_Rig", "type": "ARMATURE"},
      {"name": "Knight", "type": "MESH", "parent": "Other_Rig"}), "more than one skeleton"),
    ((RIG,), "moves no mesh"),
])
def test_what_cannot_be_sent_says_why(objects, says):
    rig, reason = to_unity.pick_rig(scene(*objects))
    assert rig is None and says in reason


@pytest.fixture
def projects(tmp_path, monkeypatch):
    made = {}
    for name in ("Aria Test Project", "Stone Betting"):
        root = tmp_path / name
        (root / "Assets").mkdir(parents=True)
        (root / "ProjectSettings").mkdir()
        made[name] = root
    monkeypatch.setattr(to_unity, "configured_project", lambda: made["Aria Test Project"])
    return made


def test_the_sentence_can_name_another_project(projects):
    assert to_unity.project_named("send him to Unity in stone betting") == projects["Stone Betting"]
    assert to_unity.project_named("send him to Unity") is None


def test_a_project_is_open_only_while_its_lockfile_is_held(projects):
    root = projects["Aria Test Project"]
    assert to_unity.project_is_open(root) is False
    lock = root / "Temp" / "UnityLockfile"
    lock.parent.mkdir()
    lock.write_text("")
    assert to_unity.project_is_open(root) is False          # left behind by a crash
    # Unity opens it sharing nothing, as this does.
    import _winapi
    held = _winapi.CreateFile(str(lock), _winapi.GENERIC_READ | _winapi.GENERIC_WRITE, 0, 0,
                              _winapi.OPEN_EXISTING, 0, 0)
    try:
        assert to_unity.project_is_open(root) is True
    finally:
        _winapi.CloseHandle(held)
    assert to_unity.project_is_open(root) is False


def test_the_importer_is_installed_once_and_updated_when_it_changes(projects):
    root = projects["Aria Test Project"]
    assert to_unity.install_importer(root) is True
    assert to_unity.install_importer(root) is False
    (root / to_unity.IMPORTER_IN_PROJECT).write_text("// old")
    assert to_unity.install_importer(root) is True


class FakeSession:
    """Answers describe(); records what it was asked to run."""

    def __init__(self, objects, order):
        self.objects, self.order, self.ran = objects, order, []

    def describe(self):
        return {"success": True, "scene": {"objects": self.objects}}

    def run(self, actions, preview=None):
        self.ran.append(actions)
        path = Path(actions[0]["params"]["path"])
        self.order.append(("fbx", path.parent / f"{path.stem}.aria.json" in list(path.parent.iterdir())))
        path.write_bytes(b"fbx")
        return {"success": True}


def test_the_sidecar_lands_before_the_model_and_unity_builds_it(projects, monkeypatch):
    root = projects["Aria Test Project"]
    order = []
    session = FakeSession([RIG, BODY], order)

    def batch(project, name, since):
        report = {"name": name, "prefab": f"Assets/ARIA/Characters/{name}/{name}.prefab",
                  "controller": "c", "avatarHuman": True, "humanBones": 19,
                  "clips": [{"name": "Walk", "loop": True, "length": 0.8, "picture": "w.png"}]}
        return {"done": True, "report": report}

    monkeypatch.setattr(to_unity, "project_is_open", lambda project: False)
    monkeypatch.setattr(to_unity, "_batch_import", batch)
    outcome = to_unity.send(session, name="Miner Guy")
    assert outcome["success"] and outcome["name"] == "Miner_Guy"
    assert order == [("fbx", True)]                        # the sidecar was already there
    folder = root / "Assets" / "ARIA" / "Characters" / "Miner_Guy"
    sidecar = json.loads((folder / "Miner_Guy.aria.json").read_text())
    assert sidecar["loop"] == ["Walk", "Idle"]
    [[export]] = session.ran
    assert export == {"action": "export_fbx", "params": {"objects": ["Body_Rig"],
                                                        "path": str(folder / "Miner_Guy.fbx")}}
    assert (root / to_unity.IMPORTER_IN_PROJECT).is_file()
    assert outcome["pictures"] == ["w.png"] and "Humanoid" in outcome["text"]


def test_an_open_project_that_does_not_answer_says_to_click_into_unity(projects, monkeypatch):
    monkeypatch.setattr(to_unity, "project_is_open", lambda project: True)
    monkeypatch.setattr(to_unity, "_open_import", lambda *a: {
        "done": False, "text": "unity has the project open but did not answer"})
    outcome = to_unity.send(FakeSession([RIG, BODY], []))
    assert outcome["success"] and outcome["imported"] is False
    assert "Unity has the project open" in outcome["text"]


def test_an_unrigged_model_is_left_to_the_plain_export(monkeypatch):
    session = FakeSession([BODY | {"parent": None}], [])
    assert blender_session.answer_send("export the car to Unity", session) is None
    assert session.ran == []


@pytest.mark.parametrize("said, sends", [
    ("send him to Unity", True),
    ("send the Miner to Unity as Wick", True),
    ("export it into Unity", True),
    ("put him in Unity", True),
    ("I love Unity", False),
    ("send him a message", False),
])
def test_what_chat_takes_as_sending(said, sends):
    assert bool(blender_session.SEND_TO_UNITY.search(said)) is sends


def test_a_question_about_sending_does_nothing(monkeypatch):
    from backend.blender import blender_actions

    monkeypatch.setattr(blender_session, "answer_send",
                        lambda *a: pytest.fail("a question must not send anything"))
    assert blender_actions.answer_request("how do I send him to Unity?") is None


def test_exporting_a_rig_takes_everything_under_it():
    script = templates.build_script([{"action": "export_fbx", "params": {
        "objects": ["Body_Rig"], "path": "C:/x.fbx"}}])
    assert "children_recursive" in script and "use_selection=_use_selection" in script
