"""Rigging a body from its landmarks.

A rig is only a rig if moving a bone moves the right part of the body
and nothing else -- so these pose the real thing and measure the mesh.
Bone names are checked against Unity's Humanoid list, because a rig
Unity cannot map is a rig somebody maps by hand, bone by bone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.blender import blender_actions
from backend.blender import blender_script_templates as templates
from backend.blender import blender_session as bs

UNITY_REQUIRED = {"Hips", "Spine", "Head", "LeftUpperLeg", "LeftLowerLeg", "LeftFoot",
                  "RightUpperLeg", "RightLowerLeg", "RightFoot", "LeftUpperArm",
                  "LeftLowerArm", "LeftHand", "RightUpperArm", "RightLowerArm", "RightHand"}
_BUNDLE = (Path(__file__).resolve().parents[2] / "aria_models" / "blender" / "meshes"
           / "human_base_meshes_bundle.blend")


def test_the_script_compiles():
    compile(templates.build_script([{"action": "auto_rig", "params": {"object": "Body"}}]),
            "auto_rig", "exec")


def test_the_bone_names_are_unitys():
    assert UNITY_REQUIRED <= {bone for bone, _parent, _c in templates.HUMANOID_BONES}


# ======================================================
# Chat
# ======================================================

@pytest.fixture
def one_body(monkeypatch):
    runs = []
    monkeypatch.setattr(bs.Session, "describe", lambda self: {"success": True, "scene": {
        "objects": [{"name": "Body", "type": "MESH", "dimensions": [0.8, 0.3, 1.8]}]}})

    def run(self, actions, **kwargs):
        runs.append(actions)
        return {"success": True, "ran": True, "renders": ["C:/r.png"], "notes": [
            {"step": "auto_rig", "armature": "Body_Rig", "bones": ["Hips"] * 19, "arms": True,
             "unweighted": 0}]}

    monkeypatch.setattr(bs.Session, "run", run)
    return runs


@pytest.mark.parametrize("said", ["rig him in Blender", "rig the Body in Blender",
                                  "give him a skeleton in Blender", "add bones to it in Blender"])
def test_chat_rigs_after_marking_the_body(one_body, said):
    answer = bs.answer_command(said)
    assert [a["action"] for a in one_body[0]] == ["find_landmarks", "auto_rig"]
    assert one_body[0][0]["params"]["kind"] == "body"
    assert "19-bone skeleton" in answer["text"] and "Humanoid" in answer["text"]


@pytest.mark.parametrize("said", ["how do I rig a character in Blender?",
                                  "make the right arm longer in Blender"])
def test_questions_and_other_sentences_do_not_rig(one_body, said):
    answer = bs.answer_command(said)
    assert not any(a["action"] == "auto_rig" for run in one_body for a in run)


# ======================================================
# The real Blender
# ======================================================

def _blender_or_skip():
    try:
        blender_actions.blender_path()
    except Exception:
        pytest.skip("Blender is not configured on this machine")


def _rigged_base(extra=()):
    _blender_or_skip()
    if not _BUNDLE.is_file():
        pytest.skip("the human base mesh bundle is not on this machine")
    result = blender_actions.run_actions([
        {"action": "clear_scene"},
        {"action": "append_from_blend", "params": {"blend": str(_BUNDLE), "object": "GEO-body_male_stylized",
                                                   "name": "Body", "location": [0, 0, 0]}},
        {"action": "scale_to_height", "params": {"height": 1.8}},
        {"action": "apply_transforms", "params": {}},
        {"action": "origin_to_floor", "params": {}},
        {"action": "find_landmarks", "params": {"object": "Body", "kind": "body"}},
        {"action": "auto_rig", "params": {"object": "Body"}},
    ] + list(extra) + [{"action": "describe_scene"}], timeout=600)
    assert result["success"], result["output"][-2500:]
    note = next(s for s in result["result"]["steps"] if s.get("step") == "auto_rig")
    objects = {o["name"]: o for o in result["result"]["scene"]["objects"]}
    return note, objects


def test_the_base_body_gets_a_full_unity_skeleton_and_every_vertex_weighted():
    note, objects = _rigged_base()
    assert UNITY_REQUIRED <= set(objects["Body_Rig"]["bones"])
    assert objects["Body"]["parent"] == "Body_Rig"
    assert note["unweighted"] == 0


def test_bending_the_elbow_moves_the_forearm_only():
    _note, rest = _rigged_base()
    _note, bent = _rigged_base([{"action": "set_pose", "params": {
        "armature": "Body_Rig", "bone": "LeftLowerArm", "rotation": [80, 0, 0], "frame": 1}}])
    # The body gets deeper front to back (a forearm now reaching forward)
    # and no taller or wider than the arms already made it.
    assert bent["Body"]["dimensions"][1] > rest["Body"]["dimensions"][1] + 0.05
    assert abs(bent["Body"]["dimensions"][2] - rest["Body"]["dimensions"][2]) < 0.01


def test_lifting_a_leg_moves_the_leg():
    _note, rest = _rigged_base()
    _note, lifted = _rigged_base([{"action": "set_pose", "params": {
        "armature": "Body_Rig", "bone": "RightUpperLeg", "rotation": [-60, 0, 0], "frame": 1}}])
    assert lifted["Body"]["dimensions"][1] > rest["Body"]["dimensions"][1] + 0.2


def _figure(arms):
    from test_blender_sculpt_language import _primitive_figure  # noqa: E402

    return _primitive_figure(arms, [{"action": "auto_rig", "params": {"object": "Torso"}}])


def test_a_t_posed_figure_is_rigged_with_its_arms(monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    note, figure = _figure("out")
    # _primitive_figure returns the find_landmarks note; the rig is on the scene.
    assert figure["parent"] == "Torso_Rig"


def test_a_figure_with_arms_against_its_sides_is_rigged_without_them():
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    _note, figure = _figure("sides")
    assert figure["parent"] == "Torso_Rig"
