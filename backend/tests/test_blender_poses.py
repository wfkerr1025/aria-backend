"""Poses in plain English -- "raise his left arm", "make him sit", "point at the door".

Run for real on 2026-09-28 on the Ludo dwarf: raise an arm, then sit
(the arm stays up, the feet stay on the floor), point at a Door cube,
kneel + hands on hips + look left, stand up straight, cheer -- and the
cheer arriving in Unity as a clip that holds (after one fix: a one-key
take came in as a 1-second clip with the arms thrust forward).
"""

from __future__ import annotations

import pytest

from backend.blender import blender_script_templates as templates
from backend.blender import blender_session


def script(**params):
    return templates.build_script([{"action": "pose_character", "params": params}])


@pytest.mark.parametrize("pose", templates.POSES)
def test_every_pose_builds(pose):
    compile(script(armature="Body_Rig", pose=pose), pose, "exec")


def test_the_old_bone_by_bone_set_pose_is_still_there():
    assert templates.parameters("set_pose") == ["armature", "bone", "frame", "rotation"]


def test_a_pose_is_held_for_two_frames_and_adds_to_the_last():
    text = script(armature="Body_Rig", pose="cheer")
    assert "for frame in (1, 2):" in text
    assert 'ad.action.name.startswith("Pose")' in text          # the next pose keeps this one


def test_seated_poses_put_the_feet_back_on_the_floor():
    assert "_pz_feet_down(rig, floor)" in script(armature="Body_Rig", pose="sit")


def test_pointing_and_saluting_default_to_the_right_arm():
    assert "'point', 'right'" in script(armature="R", pose="point")
    assert "'raise_arm', 'both'" in script(armature="R", pose="raise_arm")


@pytest.mark.parametrize("said, expected", [
    ("raise his left arm in Blender", ("raise_arm", "left", "forward", None)),
    ("raise both arms in Blender", ("raise_arm", "both", "forward", None)),
    ("make him sit in Blender", ("sit", None, "forward", None)),
    ("make him point at the door in Blender", ("point", None, "forward", "door")),
    ("have her kneel in Blender", ("kneel", None, "forward", None)),
    ("make him look left in Blender", ("look", "left", "left", None)),
    ("put his hands on his hips in Blender", ("hands_on_hips", None, "forward", None)),
    ("stand him up straight in Blender", ("stand", None, "forward", None)),
    ("reset his pose in Blender", ("rest", None, "forward", None)),
    ("make him walk in Blender", None),                   # a clip, not a pose
    ("make him wave in Blender", None),
    ("give him a bow and arrow in Blender", None),
    ("make the nose bigger in Blender", None),
])
def test_what_is_heard_as_a_pose(said, expected):
    assert blender_session._pose_asked(said) == expected


class Scene:
    where, undo_hint = "in Blender", "undo"

    def __init__(self, objects):
        self.objects, self.ran = objects, []

    def describe(self):
        return {"success": True, "scene": {"objects": self.objects}}

    def run(self, actions, **_):
        self.ran.append(actions)
        return {"success": True, "renders": []}


BODY = [{"name": "Dwarf", "type": "MESH", "parent": "Dwarf_Rig"}, {"name": "Dwarf_Rig", "type": "ARMATURE"},
        {"name": "Door", "type": "MESH"}]


def test_pointing_at_something_names_it_as_it_is_in_the_scene():
    session = Scene(BODY)
    answer = blender_session.answer_command("make him point at the door in Blender", session)
    assert answer["ran"] and session.ran == [[{"action": "pose_character", "params": {
        "armature": "Dwarf_Rig", "pose": "point", "target": "Door"}}]]


def test_pointing_at_nothing_there_says_what_is():
    answer = blender_session.answer_command("make him point at the window in Blender", Scene(BODY))
    assert not answer["success"] and "window" in answer["text"] and "Door" in answer["text"]


def test_no_skeleton_no_pose():
    answer = blender_session.answer_command("make him sit in Blender", Scene([{"name": "Rock", "type": "MESH"}]))
    assert not answer["success"] and "rig him" in answer["text"]


def test_two_skeletons_are_asked_about():
    two = BODY + [{"name": "Wick_Rig", "type": "ARMATURE"}]
    assert "Which one" in blender_session.answer_command("make him sit in Blender", Scene(two))["text"]
    session = Scene(two)
    blender_session.answer_command("make Wick sit in Blender", session)
    assert session.ran[0][0]["params"]["armature"] == "Wick_Rig"
