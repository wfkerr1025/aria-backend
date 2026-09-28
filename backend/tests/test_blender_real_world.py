"""What real characters broke, rebuilt from primitives so it stays fixed.

Everything here was found by running the whole pipeline -- landmarks,
rig, clips -- on two characters nobody built for this layer: an
armoured knight from the Unity test project, and Mixamo's bot in a
T-pose. Each test rebuilds the one feature of theirs that broke
something, and checks the fix:

  * boots touching at the heels -- the crotch was read at the feet;
  * a sword hanging beside a thigh -- it was taken for the leg;
  * legs standing close -- both knees averaged into one, both legs'
    bones down the middle;
  * a mesh in overlapping layers -- automatic weights gave no vertex a
    weight, so a stand-in is used;
  * groups left by an older rig -- normalizing counted them and halved
    the new weights;
  * a rig's old armature deleted -- apply_transforms then failed with
    nothing active;
  * a T-pose rig -- clips left the arms straight out, and a wave went
    over the head.
"""

from __future__ import annotations

import pytest

from backend.blender import blender_actions


def _blender_or_skip():
    try:
        blender_actions.blender_path()
    except Exception:
        pytest.skip("Blender is not configured on this machine")


def _step(action, **params):
    return {"action": action, "params": params}


def _figure(extra_parts=(), *, arms="down", legs=0.1, remesh=True, after=(), allow_python=False):
    """A standing figure from primitives, as one mesh called Torso."""
    _blender_or_skip()
    turn = {"out": ([0.52, 0, 1.40], [0, 90, 0], [-0.52, 0, 1.40], [0, 90, 0]),
            "down": ([0.40, 0, 1.18], [0, 45, 0], [-0.40, 0, 1.18], [0, -45, 0])}[arms]
    steps = [
        _step("clear_scene"),
        _step("add_cylinder", name="LegL", radius=0.08, depth=0.86, location=[legs, 0, 0.43]),
        _step("add_cylinder", name="LegR", radius=0.08, depth=0.86, location=[-legs, 0, 0.43]),
        _step("add_sphere", name="Hips", radius=0.19, location=[0, 0, 0.9]),
        _step("add_cylinder", name="Torso", radius=0.17, depth=0.62, location=[0, 0, 1.17]),
        _step("add_cylinder", name="Neck", radius=0.055, depth=0.14, location=[0, 0, 1.52]),
        _step("add_sphere", name="Head", radius=0.12, location=[0, 0, 1.68]),
        _step("add_cylinder", name="ArmL", radius=0.05, depth=0.72, location=turn[0]),
        _step("rotate", object="ArmL", x=turn[1][0], y=turn[1][1], z=turn[1][2]),
        _step("add_cylinder", name="ArmR", radius=0.05, depth=0.72, location=turn[2]),
        _step("rotate", object="ArmR", x=turn[3][0], y=turn[3][1], z=turn[3][2]),
    ]
    names = ["Torso", "LegL", "LegR", "Hips", "Neck", "Head", "ArmL", "ArmR"]
    for part in extra_parts:
        steps.append(part)
        if part["action"].startswith("add_"):
            names.append(part["params"]["name"])
    steps.append(_step("join_objects", objects=names))
    if remesh:
        steps.append(_step("voxel_remesh", object="Torso", size=0.015))
    steps += list(after) + [_step("describe_scene")]
    result = blender_actions.run_actions(steps, timeout=600, allow_python=allow_python)
    assert result["success"], result["output"][-2500:]
    objects = {o["name"]: o for o in result["result"]["scene"]["objects"]}
    notes = {s["step"]: s for s in result["result"]["steps"]}
    return objects, notes, result


def test_boots_touching_at_the_heels_are_not_the_crotch():
    # A slab 0.42 wide and 0.1 high across both feet: the heels touch.
    boots = _step("add_cube", name="Boots", size=1, location=[0, 0, 0.05])
    scale = _step("scale", object="Boots", x=0.42, y=0.2, z=0.1)
    _objects, notes, _r = _figure([boots, scale], after=[
        _step("find_landmarks", object="Torso", kind="body")])
    assert notes["find_landmarks"]["heights"]["crotch"] > 0.6


def test_a_sword_beside_the_thigh_is_not_the_leg():
    sword = _step("add_cylinder", name="Sword", radius=0.015, depth=0.7, location=[0.23, 0, 0.55])
    objects, _notes, _r = _figure([sword], after=[_step("find_landmarks", object="Torso", kind="body")])
    marks = objects["Torso"]["landmarks"]
    for name in ("knee_l", "thigh_l", "shin_l"):
        assert abs(marks[name][0] - 0.1) < 0.1, (name, marks[name])      # on the leg at x 0.1


def test_close_legs_get_a_bone_each():
    objects, _notes, _r = _figure(legs=0.085, after=[
        _step("find_landmarks", object="Torso", kind="body"), _step("auto_rig", object="Torso")])
    joints = objects["Torso_Rig"]["joints"]
    for bone, sign in (("LeftLowerLeg", 1), ("RightLowerLeg", -1), ("LeftUpperLeg", 1)):
        head, tail = joints[bone]
        assert head[0] * sign > 0.04 and tail[0] * sign > 0.04, (bone, head, tail)


def test_a_layered_mesh_is_weighted_through_a_stand_in():
    """Not remeshed: separate overlapping primitives, the way a game
    character's armour sits on its body. Forced to the stand-in here, so
    the path is tested whether or not automatic weights would cope."""
    objects, notes, _r = _figure(remesh=False, after=[
        _step("find_landmarks", object="Torso", kind="body"),
        _step("auto_rig", object="Torso", weights="standin")])
    assert "stand-in" in notes["auto_rig"]["weights"]
    assert notes["auto_rig"]["unweighted"] == 0
    assert objects["Torso"]["parent"] == "Torso_Rig"


def test_an_older_rigs_groups_do_not_halve_the_new_weights():
    old = _step("run_python", code=(
        "o = obj('Torso')\n"
        "g = o.vertex_groups.new(name='mixamorig:Hips')\n"
        "g.add(list(range(len(o.data.vertices))), 1.0, 'REPLACE')\n"))
    check = _step("run_python", code=(
        "o = obj('Torso')\n"
        "bones = set(b.name for b in obj('Torso_Rig').data.bones)\n"
        "names = {g.index: g.name for g in o.vertex_groups}\n"
        "sums = [sum(g.weight for g in v.groups if names[g.group] in bones) for v in o.data.vertices]\n"
        "result['low'] = min(sums)\n"
        "result['old_kept'] = 'mixamorig:Hips' in [g.name for g in o.vertex_groups]\n"))
    _objects, notes, result = _figure(after=[
        _step("find_landmarks", object="Torso", kind="body"), old,
        _step("auto_rig", object="Torso"), check], allow_python=True)
    back = result["result"]["python"][-1]
    assert back["low"] > 0.99          # every vertex fully on the new bones
    assert back["old_kept"] is True     # and the old group left alone


def test_apply_transforms_after_the_active_object_is_deleted():
    _blender_or_skip()
    result = blender_actions.run_actions([
        _step("clear_scene"), _step("add_cube", name="A"), _step("add_cube", name="B"),
        _step("delete_object", object="B"), _step("apply_transforms")], timeout=300)
    assert result["success"], result["output"][-2000:]


def test_a_t_pose_walks_and_waves_with_its_arms_down():
    objects, _notes, _r = _figure(arms="out", after=[
        _step("find_landmarks", object="Torso", kind="body"), _step("auto_rig", object="Torso"),
        _step("add_clip", armature="Torso_Rig", clip="walk"), _step("set_frame", frame=7)])
    rest_width = 1.76                                  # arm tip to arm tip, straight out (0.88 each side)
    low, high = objects["Torso"]["bounds"]
    assert high[0] - low[0] < rest_width * 0.7          # the arms hang, not stick out
    waving, _n, _r = _figure(arms="out", after=[
        _step("find_landmarks", object="Torso", kind="body"), _step("auto_rig", object="Torso"),
        _step("add_clip", armature="Torso_Rig", clip="wave"), _step("set_frame", frame=13)])
    low, high = waving["Torso"]["bounds"]
    assert high[2] < 1.80 + 0.45                        # up, not over and down the far side
    assert high[0] < 0.6                                # the left arm hangs too (0.88 when out)
