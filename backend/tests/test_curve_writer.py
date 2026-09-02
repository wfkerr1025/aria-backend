"""Writing generated motion into a Unity AnimationClip.

WHY NOT FBX, MEASURED RATHER THAN ASSUMED
-----------------------------------------
The obvious route is Blender -> FBX -> Unity. It does not work. The
curves transfer onto the rig in Blender (315 of 315, and the rig
demonstrably moves), the FBX exports, Blender reads 50 actions and 230
moving curves back out of that same file -- and Unity answers

    importAnimation=True  animationType=Human  takes=0

Explicit bake flags and an NLA strip changed nothing. So the curves go
straight into a .anim asset instead.

WHAT THE LIVE RUN ESTABLISHED
-----------------------------
  * the source motion is real: 128 of 180 rotation curves move, and
    only 3 of 135 location curves do -- the Hips root translation,
    exactly as `mode: rot_only` documents;
  * the mapping needs no axis conversion. Unity's imported hierarchy
    carries the whole Z-up-to-Y-up rotation on the root and leaves
    every bone at identity, so a bone-local quaternion transfers with
    only Blender's w,x,y,z rewritten as Unity's x,y,z,w. Verified: at
    t=0.25 Unity read back x=-0.2414 where the source frame holds
    -0.2272, the same rotation;
  * A HUMANOID ANIMATOR IGNORES THESE CURVES. Sampling the clip against
    the Humanoid import moved nothing at all; the same clip against the
    same model imported Generic drives it. Humanoid wants muscle
    curves, and generated-per-rig animation does not need retargeting
    in the first place;
  * root translation must not be written raw. The source is in the
    pre-cleanup scale, and writing it threw the character metres across
    the scene.

183 curves took 68 seconds to write, one command each. There is no
batch form.
"""

from __future__ import annotations

import json

import pytest

from backend.unity import curve_writer as curves
from backend.unity import unity_delivery as delivery


def dump(bones=("mixamorig:Hips", "mixamorig:LeftUpLeg")):
    """A curve export shaped the way blender_curve_export writes one.

    Rotations only, as whole quaternions in Blender's w,x,y,z order.
    Translation never reaches here -- the export drops it, because
    writing it raw put the feet at 7.7m on a 1.8m character.
    """
    return {
        "action": "Steady Forward Walk",
        "bones": [
            {"bone": bone, "property": "rotation",
             "keys": [{"frame": 0.0, "wxyz": [1.0, 0.0, 0.0, 0.0]},
                      {"frame": 15.0, "wxyz": [0.97, -0.22, 0.01, -0.002]}]}
            for bone in bones
        ],
    }


PATHS = {
    "mixamorig:Hips": "skintokens_rig/mixamorig:Hips",
    "mixamorig:LeftUpLeg": "skintokens_rig/mixamorig:Hips/mixamorig:LeftUpLeg",
}


@pytest.fixture
def cli(monkeypatch):
    calls = []
    state = {"fail": None,
             "hierarchy": "\n".join(sorted(PATHS.values()))}

    def fake(command, args=None, **kwargs):
        argv = list(args or [])
        named = {argv[i].lstrip("-"): argv[i + 1]
                 for i in range(0, len(argv) - 1, 2) if argv[i].startswith("--")}
        name = command.replace("cmd ", "")
        calls.append({"command": name, "args": named})

        if name == state["fail"]:
            return {"success": False, "output": "", "json": None,
                    "error": "refused"}

        result = ({"success": True, "result": state["hierarchy"]}
                  if name == "eval" else {"ok": True})
        return {"success": True, "output": "", "error": None,
                "json": {"success": True, "errors": [],
                         "data": {"command": name, "parameters": named,
                                  "result": result, "target": {},
                                  "success": True}}}

    from backend.unity import unity_cli_engine
    monkeypatch.setattr(unity_cli_engine, "run_invocation", fake)
    return calls, state


# ======================================================
# The mapping
# ======================================================

def test_blender_quaternion_order_becomes_unity_order():
    """Blender stores w,x,y,z and Unity's serialized property is
    x,y,z,w. Index 0 is w in both this table and the dump."""
    assert curves.ROTATION_PROPERTIES == (
        "m_LocalRotation.w", "m_LocalRotation.x",
        "m_LocalRotation.y", "m_LocalRotation.z")


def test_a_rotation_curve_is_written_for_every_component():
    calls = curves.curves_for_clip(dump(("mixamorig:LeftUpLeg",)), PATHS)

    properties = sorted(c["property"] for c in calls)
    assert properties == ["m_LocalRotation.w", "m_LocalRotation.x",
                          "m_LocalRotation.y", "m_LocalRotation.z"]


def test_the_w_component_carries_blender_index_zero():
    """Getting this backwards produces a clip that plays a plausible
    but wrong rotation, which is far worse than one that plays
    nothing."""
    calls = curves.curves_for_clip(dump(("mixamorig:LeftUpLeg",)), PATHS)
    w = [c for c in calls if c["property"] == "m_LocalRotation.w"][0]
    x = [c for c in calls if c["property"] == "m_LocalRotation.x"][0]

    assert w["keys"][0]["value"] == 1.0
    assert x["keys"][1]["value"] == -0.22


def test_frames_become_seconds():
    """Blender numbers frames and Unity times keys in seconds."""
    calls = curves.curves_for_clip(dump(("mixamorig:LeftUpLeg",)), PATHS,
                                   frame_rate=30.0)

    assert calls[0]["keys"][0]["time"] == 0.0
    assert calls[0]["keys"][1]["time"] == 0.5


def test_the_frame_rate_is_used():
    calls = curves.curves_for_clip(dump(("mixamorig:LeftUpLeg",)), PATHS,
                                   frame_rate=60.0)

    assert calls[0]["keys"][1]["time"] == 0.25


def test_curves_are_addressed_by_hierarchy_path_not_bone_name():
    """Two bones can share a name at different places in a skeleton,
    and a curve is addressed by its path."""
    calls = curves.curves_for_clip(dump(("mixamorig:LeftUpLeg",)), PATHS)

    assert calls[0]["path"] == \
        "skintokens_rig/mixamorig:Hips/mixamorig:LeftUpLeg"


def test_a_bone_unity_does_not_have_is_skipped_not_guessed():
    calls = curves.curves_for_clip(dump(("mixamorig:Tail",)), PATHS)

    assert calls == []


def test_everything_is_a_transform_curve():
    calls = curves.curves_for_clip(dump(), PATHS)

    assert {c["type"] for c in calls} == {"Transform"}


# ======================================================
# Positions, which are the part that went wrong live
# ======================================================

def test_no_position_curve_is_ever_written():
    """The export drops translation before it reaches here, and there
    is no option to put it back. The source positions are in the
    pre-cleanup scale: writing them raw threw the character metres
    across the scene, feet at 7.7m on a 1.8m character. There is no
    correct value to substitute without the source scale, so a caller
    who wants root motion scales it deliberately rather than having
    this guess."""
    calls = curves.curves_for_clip(dump(), PATHS)

    assert not [c for c in calls if "Position" in c["property"]]


def test_a_dump_that_still_carries_translation_is_ignored_safely():
    """An older export, or a hand-made one. The rotations are still
    written and the translation is simply not looked at."""
    stale = dump()
    stale["bones"].append(
        {"bone": "mixamorig:Hips", "property": "location",
         "keys": [{"frame": 0.0, "wxyz": [0, 0, 0, 0]}]})

    calls = curves.curves_for_clip(stale, PATHS)

    assert {c["property"] for c in calls} == set(curves.ROTATION_PROPERTIES)


def test_the_position_property_names_are_kept_for_stripping_old_clips():
    """A clip written before the drop still has them, and removing one
    needs its exact serialized name."""
    assert curves.POSITION_PROPERTIES == (
        "m_LocalPosition.x", "m_LocalPosition.y", "m_LocalPosition.z")


def test_a_rotation_only_clip_is_four_curves_per_bone():
    calls = curves.curves_for_clip(dump(), PATHS)

    assert len(calls) == 2 * 4


# ======================================================
# Reading the hierarchy
# ======================================================

def test_the_paths_come_from_unity_not_from_the_rig(cli):
    calls, _ = cli
    paths = curves.bone_paths("AriaHero")

    assert calls[0]["command"] == "eval"
    assert paths["mixamorig:LeftUpLeg"] == \
        "skintokens_rig/mixamorig:Hips/mixamorig:LeftUpLeg"


def test_an_object_that_is_not_there_yields_nothing(cli):
    _, state = cli
    state["hierarchy"] = "NO_OBJECT"

    assert curves.bone_paths("Ghost") == {}


def test_the_first_path_wins_for_a_repeated_bone_name(cli):
    _, state = cli
    state["hierarchy"] = "rig/Bone\nrig/Other/Bone"

    assert curves.bone_paths("X")["Bone"] == "rig/Bone"


# ======================================================
# Writing
# ======================================================

def test_every_curve_is_one_command(cli):
    """There is no batch form. 183 curves took 68 seconds live, and
    that is worth knowing before somebody waits for four clips."""
    calls, _ = cli
    plan = curves.curves_for_clip(dump(), PATHS)

    result = curves.write_clip_curves("Assets/X.anim", plan)

    assert result["written"] == len(plan)
    assert [c["command"] for c in calls] == ["set_animation_curve"] * len(plan)


def test_the_keys_go_over_as_json(cli):
    calls, _ = cli
    plan = curves.curves_for_clip(dump(("mixamorig:LeftUpLeg",)), PATHS)

    curves.write_clip_curves("Assets/X.anim", plan)

    keys = json.loads(calls[0]["args"]["keys"])
    assert keys[0] == {"time": 0.0, "value": 1.0}


def test_a_refused_curve_is_reported_rather_than_swallowed(cli):
    calls, state = cli
    state["fail"] = "set_animation_curve"
    plan = curves.curves_for_clip(dump(), PATHS)

    result = curves.write_clip_curves("Assets/X.anim", plan)

    assert result["success"] is False
    assert result["written"] == 0
    assert result["failures"]


def test_progress_is_reported_while_it_runs(cli):
    seen = []
    plan = curves.curves_for_clip(dump(), PATHS)

    curves.write_clip_curves("Assets/X.anim", plan,
                             on_progress=lambda done, total: seen.append(done))

    assert seen == list(range(1, len(plan) + 1))


def test_writing_nothing_is_not_a_success(cli):
    result = curves.write_clip_curves("Assets/X.anim", [])

    assert result["success"] is False
    assert result["written"] == 0


# ======================================================
# A curve value is a POSE, not a motion
#
# The bug this section exists for, reported as "he's swimming, and
# he's lying flat on the ground". One cause, both symptoms.
#
# Unity's rest has the Hips at +90 degrees about X, which cancels the
# armature root's -90. The animation's Hips curve is near identity, and
# an earlier version wrote it ABSOLUTELY -- destroying that
# compensation. The character rotated 90 degrees onto its back, and
# every child bone's motion was then applied in a frame rotated by 90
# degrees, which is exactly what swimming looks like.
#
# The claim that no axis conversion was needed came from checking ONE
# bone whose rest was identity. One sample does not validate a basis.
# ======================================================

HIPS_REST = (0.7071068, 0.7071068, 0.0, 0.0)      # w,x,y,z: +90 about X


def test_frame_zero_reproduces_unitys_rest_pose():
    """The property the previous version lacked. Whatever pose Unity
    imported, the first frame must be exactly that -- otherwise the
    character starts the clip somewhere it was never posed."""
    known = {"Hips": {"path": "rig/Hips", "rest": HIPS_REST}}
    calls = curves.curves_for_clip(dump(("Hips",)), known)

    at_zero = {c["property"].rsplit(".", 1)[-1]: c["keys"][0]["value"]
               for c in calls}
    for component, expected in zip("wxyz", HIPS_REST):
        assert abs(at_zero[component] - expected) < 1e-4, component


def test_the_motion_survives_the_composition():
    """Preserving the rest pose is worthless if it flattens the
    animation into it."""
    known = {"Hips": {"path": "rig/Hips", "rest": HIPS_REST}}
    calls = curves.curves_for_clip(dump(("Hips",)), known)

    moved = [c for c in calls
             if abs(c["keys"][1]["value"] - c["keys"][0]["value"]) > 1e-3]
    assert moved, "the composition erased the motion"


def test_an_identity_rest_leaves_the_delta_alone():
    """42 of the 45 bones are identity at rest in both engines, which
    is why they looked plausible while three did not."""
    known = {"Hips": {"path": "rig/Hips", "rest": (1.0, 0.0, 0.0, 0.0)}}
    calls = curves.curves_for_clip(dump(("Hips",)), known)

    by_property = {c["property"].rsplit(".", 1)[-1]: c for c in calls}
    assert abs(by_property["x"]["keys"][1]["value"] - (-0.22)) < 1e-3


def test_a_bare_path_still_works_and_composes_onto_identity():
    """Callers that have no rest data get the old behaviour, which is
    correct wherever the rest IS identity."""
    calls = curves.curves_for_clip(dump(("Hips",)), {"Hips": "rig/Hips"})

    assert calls[0]["path"] == "rig/Hips"
    assert abs(calls[0]["keys"][0]["value"] - 1.0) < 1e-4


def test_quaternion_multiply_is_the_hamilton_product():
    identity = (1.0, 0.0, 0.0, 0.0)
    turn = (0.7071068, 0.7071068, 0.0, 0.0)

    assert curves.multiply(identity, turn) == pytest.approx(turn, abs=1e-6)
    # +90 about X twice is 180 about X.
    doubled = curves.multiply(turn, turn)
    assert doubled == pytest.approx((0.0, 1.0, 0.0, 0.0), abs=1e-6)


def test_inverse_undoes_a_rotation():
    turn = (0.7071068, 0.7071068, 0.0, 0.0)
    back = curves.multiply(curves.inverse(turn), turn)

    assert back == pytest.approx((1.0, 0.0, 0.0, 0.0), abs=1e-6)


# ======================================================
# The rest pose has to come from the asset
# ======================================================

def test_the_rest_pose_is_read_from_the_asset_when_one_is_given(cli):
    """A scene object's localRotation is its CURRENT pose, and sampling
    a clip leaves it posed. Reading a posed instance gave 35
    non-identity rotations where the prefab has 3 -- composing onto
    those folds one animation's pose into the next one's curves, and
    the error compounds every run."""
    calls, _ = cli
    curves.bone_rest("AriaHero", asset_path="Assets/ARIA/Prefabs/Hero.prefab")

    code = calls[0]["args"]["code"]
    assert "Assets/ARIA/Prefabs/Hero.prefab" in code
    # The branch has to be REACHABLE, not merely present: an earlier
    # version of this test passed against `if (false)`.
    assert "if (!string.IsNullOrEmpty(assetPath))" in code
    assert code.index("LoadAssetAtPath") < code.index("GameObject.Find"),         "the scene is consulted before the asset"


def test_it_falls_back_to_the_scene_when_no_asset_is_given(cli):
    calls, _ = cli
    curves.bone_rest("AriaHero")

    assert "GameObject.Find" in calls[0]["args"]["code"]


def test_the_rest_reader_returns_paths_and_quaternions(cli):
    calls, state = cli
    state["hierarchy"] = "rig/Hips\t0.707107,0.707107,0.000000,0.000000"

    found = curves.bone_rest("X")

    assert found["Hips"]["path"] == "rig/Hips"
    assert found["Hips"]["rest"] == pytest.approx(HIPS_REST, abs=1e-5)


def test_a_bone_with_no_rotation_column_defaults_to_identity(cli):
    calls, state = cli
    state["hierarchy"] = "rig/Hips"

    assert curves.bone_rest("X")["Hips"]["rest"] == (1.0, 0.0, 0.0, 0.0)
