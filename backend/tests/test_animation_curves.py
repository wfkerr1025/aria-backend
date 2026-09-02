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

from backend.unity import animation_curves as curves
from backend.unity import unity_delivery as delivery


def dump(bones=("mixamorig:Hips", "mixamorig:LeftUpLeg")):
    """A curve dump shaped the way the Blender side writes one."""
    return {
        bone: {
            "rotation_quaternion": {
                "0": [[0.0, 1.0], [15.0, 0.97]],
                "1": [[0.0, 0.0], [15.0, -0.22]],
                "2": [[0.0, 0.0], [15.0, 0.01]],
                "3": [[0.0, 0.0], [15.0, -0.002]],
            },
            "location": {
                "0": [[0.0, 0.0], [15.0, 0.008]],
                "1": [[0.0, 0.0], [15.0, 0.067]],
                "2": [[0.0, 0.0], [15.0, 0.007]],
            },
        }
        for bone in bones
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

def test_only_the_root_gets_a_position_curve():
    """The source translations are in the pre-cleanup scale. Writing
    them for every bone threw the character metres across the scene."""
    calls = curves.curves_for_clip(dump(), PATHS,
                                   root_bone="mixamorig:Hips")

    positions = [c for c in calls if "Position" in c["property"]]
    assert {c["path"] for c in positions} == \
        {"skintokens_rig/mixamorig:Hips"}


def test_root_position_can_be_left_out_entirely():
    """In-place locomotion: the engine moves the character and the
    clip only cycles the legs."""
    calls = curves.curves_for_clip(dump(), PATHS,
                                   root_bone="mixamorig:Hips",
                                   include_root_position=False)

    assert not [c for c in calls if "Position" in c["property"]]


def test_no_root_bone_means_no_positions():
    calls = curves.curves_for_clip(dump(), PATHS)

    assert not [c for c in calls if "Position" in c["property"]]


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
