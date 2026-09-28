"""Animation clips: walk, idle, wave, nod, jump -- each its own clip for Unity.

The measurements that matter are the ones a viewer would notice: feet
that stay on the floor while the legs bend (they sank into it, at
first, through a jump's crouch), a body that leaves the floor at the top
of a jump, and clips that arrive in an FBX as separate animations.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.blender import blender_actions
from backend.blender import blender_script_templates as templates
from backend.blender import blender_session as bs

_BUNDLE = (Path(__file__).resolve().parents[2] / "aria_models" / "blender" / "meshes"
           / "human_base_meshes_bundle.blend")


@pytest.mark.parametrize("clip", sorted(templates.CLIP_NAMES))
def test_every_clip_builds_a_script(clip):
    compile(templates.build_script([{"action": "add_clip", "params": {
        "armature": "Rig", "clip": clip, "speed": 1.5, "strength": 0.5}}]), clip, "exec")


def test_an_unknown_clip_is_refused():
    with pytest.raises(templates.BadValue, match="walk"):
        templates.build_script([{"action": "add_clip", "params": {"armature": "R", "clip": "moonwalk"}}])


def test_a_speed_of_zero_is_refused():
    with pytest.raises(templates.BadValue):
        templates.build_script([{"action": "add_clip", "params": {"armature": "R", "clip": "walk", "speed": 0}}])


def test_frames_are_rendered_when_asked():
    source = templates.build_script([{"action": "render_preview", "params": {
        "path": "C:/p.png", "frames": [1, 7, 13]}}])
    assert "[1, 7, 13]" in source


# ======================================================
# Chat
# ======================================================

@pytest.fixture
def body(monkeypatch):
    runs = []
    entry = {"name": "Body", "type": "MESH", "dimensions": [0.8, 0.3, 1.8]}
    monkeypatch.setattr(bs.Session, "describe", lambda self: {"success": True, "scene": {
        "objects": [entry]}})

    def run(self, actions, **kwargs):
        runs.append(actions)
        return {"success": True, "ran": True, "renders": ["C:/r.png"], "notes": [
            {"step": "add_clip", "clip": "Walk", "clips": ["Walk"], "skipped": []}]}

    monkeypatch.setattr(bs.Session, "run", run)
    return runs, entry


def test_an_unrigged_body_is_rigged_before_it_walks(body):
    runs, _ = body
    answer = bs.answer_command("make him walk in Blender")
    assert [a["action"] for a in runs[0]] == ["find_landmarks", "auto_rig", "add_clip", "render_preview"]
    assert runs[0][2]["params"] == {"armature": "Body_Rig", "clip": "walk", "speed": 1.0}
    assert "rigged it first" in answer["text"]


def test_a_rigged_body_just_gets_the_clip(body):
    runs, entry = body
    entry["parent"] = "Body_Rig"
    bs.answer_command("make him wave in Blender")
    assert [a["action"] for a in runs[0]] == ["add_clip", "render_preview"]


@pytest.mark.parametrize("said, clip, speed", [
    ("make him walk slowly in Blender", "walk", 0.6),
    ("make her jump in Blender", "jump", 1.0),
    ("add a fast walk cycle in Blender", "walk", 1.6),
    ("give him an idle animation in Blender", "idle", 1.0),
    ("make him nod in Blender", "nod", 1.0),
])
def test_clip_words(body, said, clip, speed):
    runs, entry = body
    entry["parent"] = "Body_Rig"
    bs.answer_command(said)
    assert runs[0][0]["params"]["clip"] == clip
    assert runs[0][0]["params"]["speed"] == speed


@pytest.mark.parametrize("said", ["how do I make him walk in Blender?",
                                  "the wavy hair in Blender", "make the nose bigger in Blender"])
def test_other_sentences_do_not_animate(body, said):
    runs, entry = body
    entry["parent"] = "Body_Rig"
    bs.answer_command(said)
    assert not any(a["action"] == "add_clip" for run in runs for a in run)


# ======================================================
# The real Blender
# ======================================================

def _rigged(extra, frame=None):
    try:
        blender_actions.blender_path()
    except Exception:
        pytest.skip("Blender is not configured on this machine")
    if not _BUNDLE.is_file():
        pytest.skip("the human base mesh bundle is not on this machine")
    steps = [
        {"action": "clear_scene"},
        {"action": "append_from_blend", "params": {"blend": str(_BUNDLE), "object": "GEO-body_male_stylized",
                                                   "name": "Body", "location": [0, 0, 0]}},
        {"action": "scale_to_height", "params": {"height": 1.8}},
        {"action": "apply_transforms", "params": {}},
        {"action": "origin_to_floor", "params": {}},
        {"action": "find_landmarks", "params": {"object": "Body", "kind": "body"}},
        {"action": "auto_rig", "params": {"object": "Body"}},
    ] + list(extra)
    if frame is not None:
        steps.append({"action": "set_frame", "params": {"frame": frame}})
    steps.append({"action": "describe_scene"})
    result = blender_actions.run_actions(steps, timeout=600)
    assert result["success"], result["output"][-2500:]
    return result


def _bounds(result):
    return next(o for o in result["result"]["scene"]["objects"] if o["name"] == "Body")["bounds"]


@pytest.mark.parametrize("frame", [1, 4, 7, 10, 13, 16, 19, 22])
def test_walking_keeps_the_feet_on_the_floor(frame):
    walk = _rigged([{"action": "add_clip", "params": {"armature": "Body_Rig", "clip": "walk"}}], frame)
    assert abs(_bounds(walk)[0][2]) < 0.025


def test_a_jump_crouches_on_the_floor_and_leaves_it():
    jump = [{"action": "add_clip", "params": {"armature": "Body_Rig", "clip": "jump"}}]
    rest, crouch, peak = _rigged(jump, 1), _rigged(jump, 9), _rigged(jump, 19)
    assert abs(_bounds(crouch)[0][2]) < 0.03                         # feet still down
    assert _bounds(crouch)[1][2] < _bounds(rest)[1][2] - 0.1        # but crouched
    assert _bounds(peak)[0][2] > 0.1                                 # off the floor


def test_waving_raises_the_right_hand_to_head_height():
    """Raised out to the side, the elbow still reaches about as far out as
    the hanging arm did -- so the measure is height, not width."""
    wave = _rigged([{"action": "add_clip", "params": {"armature": "Body_Rig", "clip": "wave"}}], 13)
    rest = _rigged([], 1)
    assert _bounds(wave)[1][2] >= _bounds(rest)[1][2] - 0.01
    # The left arm has not moved.
    assert _bounds(wave)[1][0] == pytest.approx(_bounds(rest)[1][0], abs=0.01)


def test_every_clip_arrives_in_the_fbx_as_its_own_animation(tmp_path):
    path = str(tmp_path / "clips.fbx")
    _rigged([{"action": "add_clip", "params": {"armature": "Body_Rig", "clip": "walk"}},
             {"action": "add_clip", "params": {"armature": "Body_Rig", "clip": "wave"}},
             {"action": "add_clip", "params": {"armature": "Body_Rig", "clip": "idle"}},
             {"action": "export_fbx", "params": {"path": path}}])
    back = blender_actions.run_actions([{"action": "clear_scene"},
                                        {"action": "import_model", "params": {"path": path}},
                                        {"action": "describe_scene"}], timeout=600)
    assert back["success"], back["output"][-2500:]
    imported = back["result"]["scene"]["actions"]
    for clip in ("Walk", "Wave", "Idle"):
        assert any(name.endswith("|" + clip) for name in imported), (clip, imported)
