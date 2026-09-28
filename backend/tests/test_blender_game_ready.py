"""Game-ready: a dense sculpt made light, baked, LOD'd -- and what follows it.

The Blender half ran for real (a 210,300-triangle rock to 3,032 with
colour, normal and AO maps and LODs of 1,516 and 758; an X Bot to 5,564,
rigged through landmarks read on its sculpt, walked, and sent to Unity
as a Humanoid with a three-level LOD Group). These tests hold the rules
around it without starting Blender.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.blender import blender_script_templates as templates
from backend.blender import blender_session
from backend.blender import blender_to_unity as to_unity


def script(action, **params):
    return templates.build_script([{"action": action, "params": params}])


def test_game_ready_is_one_action_with_its_settings():
    text = script("make_game_ready", object="Rock", faces=3000, size=1024, lods=[0.5], folder="C:/t")
    assert "_gr_run('Rock', 3000, 1024, [0.5]" in text
    assert "quadriflow_remesh" in text and "DECIMATE" in text          # and its fallback
    assert 'type="NORMAL", normal_space="TANGENT"' in text and '"AO"' in text
    assert set(templates.parameters("make_game_ready")) >= {"object", "faces", "size", "lods", "folder"}


def test_game_ready_keeps_the_sculpt_and_measures_how_far_to_bake_from():
    text = script("make_game_ready", object="Rock")
    assert '_Sculpt' in text and "high.hide_set(True)" in text        # hidden, never deleted
    assert "_gr_gap(high, low)" in text                                # measured, not 2% of the size


def test_lod_ratios_must_be_numbers():
    with pytest.raises(templates.BadValue):
        script("make_game_ready", object="Rock", lods=["half"])


def test_a_rigged_model_is_refused_before_anything_is_made():
    assert "make it game-ready before rigging" in script("make_game_ready", object="Body")


def test_a_rig_is_named_after_the_model_not_its_lod():
    # Unity treats any node called *_LOD0* as a level of detail; a skeleton
    # called Body_LOD0_Rig broke the Humanoid ("Transform not found").
    assert '_target.name.rsplit("_LOD", 1)[0] + "_Rig"' in script("auto_rig", object="Body_LOD0")


def test_landmarks_copy_to_the_nearest_vertex_of_the_light_copy():
    text = script("copy_landmarks", source="Body_Sculpt", target="Body_LOD0")
    assert "find_nearest" in text and '"source": "copy_landmarks"' in text


def test_exports_say_which_maps_their_materials_use():
    assert '_note("export_fbx", path=_path, maps=_maps)' in script("export_fbx", path="C:/x.fbx")


class Scene:
    """describe() and run() for a scene that exists only as a list."""

    def __init__(self, objects):
        self.objects, self.ran, self.where, self.undo_hint = objects, [], "in Blender", "undo"

    def describe(self):
        return {"success": True, "scene": {"objects": self.objects}}

    def run(self, actions, **_):
        self.ran.append(actions)
        return {"success": False, "error": "not run"}


GAME = [
    {"name": "Bot_Game", "type": "EMPTY"},
    {"name": "Bot_LOD0", "type": "MESH", "parent": "Bot_Game"},
    {"name": "Bot_LOD1", "type": "MESH", "parent": "Bot_Game"},
    {"name": "Bot_LOD2", "type": "MESH", "parent": "Bot_Game"},
    {"name": "Bot_Sculpt", "type": "MESH"},
]


def test_a_game_ready_model_is_one_thing_to_pick():
    name, entry, problem = blender_session._pick_model("rig him", Scene(GAME), "rig", "rig the {}")
    assert problem is None and name == "Bot_LOD0"
    assert entry["lod_siblings"] == ["Bot_LOD1", "Bot_LOD2"] and entry["sculpt"] == "Bot_Sculpt"


def test_rigging_it_reads_the_sculpt_and_weights_every_lod():
    session = Scene(GAME)
    blender_session._answer_rig("rig him", session)
    [actions] = session.ran
    assert [a["action"] for a in actions] == [
        "find_landmarks", "copy_landmarks", "auto_rig", "transfer_weights", "transfer_weights"]
    assert actions[0]["params"]["object"] == "Bot_Sculpt"
    assert [a["params"]["target"] for a in actions[3:]] == ["Bot_LOD1", "Bot_LOD2"]


def test_a_game_ready_lod_is_not_mistaken_for_rigged():
    session = Scene(GAME)
    blender_session._answer_clip("make him walk", "walk", session)
    [actions] = session.ran
    assert "auto_rig" in [a["action"] for a in actions]
    assert actions[-2]["params"]["armature"] == "Bot_Rig"


@pytest.mark.parametrize("said, faces, lods", [
    ("make it game ready in Blender", None, None),
    ("make it game ready with 2k triangles in Blender", 2000, None),
    ("make it game-ready with 5,000 tris and no LODs in Blender", 5000, []),
])
def test_what_game_ready_hears(said, faces, lods):
    session = Scene(GAME)
    blender_session._answer_game_ready(said, session)
    [[step]] = session.ran
    # Again means again from the sculpt, never a copy of a copy.
    assert step["params"]["object"] == "Bot_Sculpt"
    assert step["params"].get("faces") == faces and step["params"].get("lods") == lods


def test_chat_takes_game_ready_but_not_a_question_about_it():
    assert blender_session._GAME_READY.search("make him game ready")
    assert blender_session._GAME_READY.search("retopologise it")
    assert blender_session.answer_command("what does game ready mean in Blender?", Scene(GAME)) is None


def test_the_session_keeps_baked_maps_with_the_scene(tmp_path, monkeypatch):
    session = blender_session.Session("x", root=tmp_path)
    actions = [{"action": "make_game_ready", "params": {"object": "Rock"}}]
    session._place_pictures(actions, 1)
    assert actions[0]["params"]["folder"] == str(tmp_path / "x" / "textures")


def test_the_report_says_what_it_made():
    text = blender_session.game_ready_text({
        "object": "Rock_Game", "sculpt": "Rock_Sculpt", "sculpt_faces": 210300, "method": "quadriflow",
        "faces": [3032, 1516, 758], "lods": ["Rock_LOD0", "Rock_LOD1", "Rock_LOD2"],
        "maps": {"color": "C:/t/Rock_color.png", "normal": "C:/t/Rock_normal.png"}, "seconds": 3.5})
    assert "210,300-triangle sculpt" in text and "3,032 triangles" in text
    assert "Rock_LOD2 758" in text and "kept, hidden, as Rock_Sculpt" in text


# ======================================================
# Into Unity as a prop
# ======================================================

def test_a_game_ready_model_without_a_skeleton_is_a_prop():
    scene = {"objects": GAME}
    assert to_unity.pick_prop(scene) == ("Bot_Game", "Bot")
    assert to_unity.has_something_to_send(scene)
    assert not to_unity.has_something_to_send({"objects": [{"name": "Car", "type": "MESH"}]})


class Exporting(Scene):
    def __init__(self, objects, maps):
        super().__init__(objects)
        self.maps = maps

    def run(self, actions, **_):
        self.ran.append(actions)
        Path(next(a for a in actions if a["action"] == "export_fbx")["params"]["path"]).write_bytes(b"fbx")
        return {"success": True, "notes": [{"step": "export_fbx", "maps": self.maps}]}


def test_a_prop_goes_to_props_with_its_maps_named_for_their_roles(tmp_path, monkeypatch):
    project = tmp_path / "Game"
    (project / "Assets").mkdir(parents=True)
    (project / "ProjectSettings").mkdir()
    maps = {}
    for label in ("Color", "AO", "Normal"):
        source = tmp_path / f"Bot_{label.lower()}.png"
        source.write_bytes(b"png")
        maps[label] = str(source)
    monkeypatch.setattr(to_unity, "project_is_open", lambda p: False)
    monkeypatch.setattr(to_unity, "_batch_import", lambda p, n, s: {"done": True, "report": {
        "name": n, "rigged": False, "prefab": "p", "lods": ["LOD0 a"], "material": "m"}})
    outcome = to_unity.send(Exporting(GAME, maps), project=project)
    folder = project / "Assets" / "ARIA" / "Props" / "Bot"
    sidecar = json.loads((folder / "Bot.aria.json").read_text())
    assert sidecar["rigged"] is False
    assert {t["role"]: t["path"] for t in sidecar["textures"]} == {
        "color": "Assets/ARIA/Props/Bot/Bot_color.png", "occlusion": "Assets/ARIA/Props/Bot/Bot_ao.png",
        "normal": "Assets/ARIA/Props/Bot/Bot_normal.png"}
    assert all((folder / f"Bot_{k}.png").is_file() for k in ("color", "ao", "normal"))
    assert "LOD Group" in outcome["text"] and "controller" not in outcome["text"]
