"""Sculpting in plain English: sentences, landmarks, and what they do to a head.

The sentence half needs no Blender: "make the nose bigger" must become
strokes aimed at the nose, and "how do I make the nose bigger?" must
become nothing. The landmark half runs the real Blender, because a
landmark is only useful if it is where the feature is -- and stays
there once the feature has been sculpted (it did not, at first: the
nose's mark was left inside the nose after the nose was pulled out).
"""

from __future__ import annotations

import pytest

from backend.blender import blender_actions
from backend.blender import blender_script_templates as templates
from backend.blender import blender_sculpt_language as language
from backend.blender import blender_session as bs


# ======================================================
# Reading sentences
# ======================================================

@pytest.mark.parametrize("said, landmark, brush", [
    ("make the nose bigger in Blender", "nose", "inflate"),
    ("pull the chin out a little in Blender", "chin", "grab"),
    ("make his nose stick out more", "nose", "grab"),
    ("make the nose pointier", "nose", "pinch"),
    ("flatten the forehead", "forehead", "flatten"),
    ("smooth the cheeks", "cheek_l", "smooth"),
    ("deepen the eye sockets", "eye_l", "draw"),
    ("raise the cheekbones", "cheek_l", "grab"),
    ("lower the jaw", "jaw_l", "grab"),
    ("make the back of the head flatter", "back_of_head", "flatten"),
])
def test_a_change_is_aimed_at_its_feature(said, landmark, brush):
    plan = language.plan(said)
    assert plan is not None
    assert plan["strokes"][0]["landmark"] == landmark
    assert plan["strokes"][0]["brush"] == brush


def test_both_sides_unless_one_is_named():
    both = language.plan("make the ears smaller")["strokes"][0]
    left = language.plan("make the left ear smaller")["strokes"][0]
    right = language.plan("make the right ear smaller")["strokes"][0]
    assert both["mirror"] == "X" and both["landmark"] == "ear_l"
    assert "mirror" not in left and left["landmark"] == "ear_l"
    assert "mirror" not in right and right["landmark"] == "ear_r"


def test_amounts_scale_the_change():
    little = language.plan("pull the chin out a little")["strokes"][0]["distance"]
    normal = language.plan("pull the chin out")["strokes"][0]["distance"]
    lot = language.plan("pull the chin out a lot")["strokes"][0]["distance"]
    assert little < normal < lot


def test_one_change_for_several_features():
    plan = language.plan("make the nose and chin bigger")
    assert {s["landmark"] for s in plan["strokes"]} == {"nose", "chin"}


def test_several_changes_in_one_sentence():
    plan = language.plan("raise the cheekbones and flatten the forehead")
    assert [s["brush"] for s in plan["strokes"]] == ["grab", "flatten"]


def test_a_chin_is_moved_whole_not_as_a_knob():
    chin = language.plan("pull the chin out")["strokes"][0]
    nose = language.plan("pull the nose out")["strokes"][0]
    assert chin["size"] > nose["size"] * 1.4


@pytest.mark.parametrize("said, name", [
    ("give him a smile in Blender", "Smile"),
    ("make her frown", "Frown"),
    ("open his mouth", "Mouth_Open"),
    ("make him look surprised", "Surprised"),
    ("make it look angry", "Angry"),
])
def test_expressions_are_named_shape_keys(said, name):
    plan = language.plan(said)
    assert plan["expression"] == name
    assert all(s.get("landmark") for s in plan["strokes"])


@pytest.mark.parametrize("said", [
    "build a car in Blender",
    "make a character with a big nose",
    "what is a nose?",
    "the weather is nice",
])
def test_other_sentences_are_not_sculpting(said):
    assert language.plan(said) is None


def test_every_stroke_it_makes_is_a_valid_stroke():
    sentences = ["make the nose bigger", "make the ears smaller", "pull the chin out a lot",
                 "make the nose pointier", "flatten the forehead", "smooth the cheeks",
                 "make the jaw wider", "make the nose narrower", "raise the brow",
                 "lower the cheeks", "deepen the eye sockets", "give him a smile",
                 "make her frown", "open his mouth", "look surprised", "look angry"]
    for said in sentences:
        for stroke in language.plan(said)["strokes"]:
            source = templates.build_script([{"action": "sculpt_stroke",
                                              "params": {"object": "Head", **stroke}}])
            compile(source, said, "exec")


# ======================================================
# Chat
# ======================================================

@pytest.fixture
def one_head(monkeypatch):
    runs = []
    monkeypatch.setattr(bs.Session, "describe", lambda self: {"success": True, "scene": {
        "objects": [{"name": "Head", "type": "MESH", "shape_keys": [], "landmarks": ["nose"]}]}})

    def run(self, actions, **kwargs):
        runs.append(actions)
        return {"success": True, "ran": True, "renders": ["C:/r.png"], "text": ""}

    monkeypatch.setattr(bs.Session, "run", run)
    return runs


def test_chat_sculpts_the_one_model(one_head):
    answer = bs.answer_command("make the nose bigger in Blender")
    assert "Sculpted Head: nose bigger" in answer["text"]
    assert all(a["params"]["object"] == "Head" for a in one_head[0])
    assert "![Preview]" in answer["text"]


def test_chat_marks_the_face_the_first_time(monkeypatch, one_head):
    monkeypatch.setattr(bs.Session, "describe", lambda self: {"success": True, "scene": {
        "objects": [{"name": "Head", "type": "MESH"}]}})
    answer = bs.answer_command("make the nose bigger in Blender")
    assert one_head[0][0]["action"] == "find_landmarks"
    assert "show me the landmarks" in answer["text"]


def test_an_expression_goes_into_its_own_shape_key(one_head):
    bs.answer_command("give him a smile in Blender")
    actions = one_head[0]
    # The fixture head has only a nose marked, so the face is marked first.
    assert actions[0]["action"] == "find_landmarks"
    assert actions[1] == {"action": "add_shape_key", "params": {"object": "Head", "name": "Smile"}}
    assert all(a["params"].get("shape_key") == "Smile" for a in actions if a["action"] == "sculpt_stroke")
    assert actions[-1]["action"] == "set_shape_key"


def test_a_face_with_expressions_is_not_resculpted_underneath(monkeypatch, one_head):
    monkeypatch.setattr(bs.Session, "describe", lambda self: {"success": True, "scene": {
        "objects": [{"name": "Head", "type": "MESH", "shape_keys": ["Basis", "Smile"],
                     "landmarks": ["nose"]}]}})
    answer = bs.answer_command("make the nose bigger in Blender")
    assert "did not sculpt" in answer["text"] and not one_head


@pytest.mark.parametrize("said", [
    "how do I make the nose bigger in Blender?",
    "can you tell me how to give him a smile in Blender",
])
def test_a_question_about_sculpting_never_sculpts(one_head, said):
    assert bs.answer_command(said) is None
    assert not one_head


def test_sculpting_needs_blender_named_the_strict_way(one_head):
    assert bs.answer_command("make the nose bigger", gated=False) is None


def test_setting_an_expression_by_percent(monkeypatch, one_head):
    monkeypatch.setattr(bs.Session, "describe", lambda self: {"success": True, "scene": {
        "objects": [{"name": "Head", "type": "MESH", "shape_keys": ["Basis", "Smile"]}]}})
    answer = bs.answer_command("set smile to 50% in Blender")
    assert one_head[0][0]["params"] == {"object": "Head", "name": "Smile", "value": 0.5}
    assert "50%" in answer["text"]


# ======================================================
# Landmarks in the real Blender
# ======================================================

def _blender_or_skip():
    try:
        blender_actions.blender_path()
    except Exception:
        pytest.skip("Blender is not configured on this machine")


_HEAD = [
    {"action": "clear_scene"},
    {"action": "add_sphere", "params": {"name": "Head", "radius": 0.5, "location": [0, 0, 1]}},
    {"action": "sculpt_ready", "params": {"object": "Head", "detail": 60}},
    # A nose, pulled out of the front below the middle.
    {"action": "sculpt_stroke", "params": {"object": "Head", "brush": "grab",
                                           "points": [[0, -0.5, 0.9]], "radius": 0.1,
                                           "offset": [0, -0.12, 0]}},
    {"action": "find_landmarks", "params": {"object": "Head"}},
]


def _landmark_points(extra=()):
    """Run the head, then report each landmark's world point via a stroke-free probe."""
    result = blender_actions.run_actions(list(_HEAD) + list(extra) + [
        {"action": "describe_scene"}], timeout=300)
    assert result["success"], result["output"][-2500:]
    return result


def test_the_nose_is_found_where_it_sticks_out():
    _blender_or_skip()
    result = _landmark_points()
    note = next(s for s in result["result"]["steps"] if s.get("step") == "find_landmarks")
    assert "nose" in note["placed"] and "chin" in note["placed"]
    head = next(o for o in result["result"]["scene"]["objects"] if o["name"] == "Head")
    assert {"nose", "chin", "eye_l", "eye_r", "mouth", "ear_l", "ear_r"} <= set(head["landmarks"])


def test_a_landmark_stroke_goes_the_way_it_is_told():
    _blender_or_skip()
    before = _landmark_points()
    after = _landmark_points([{"action": "sculpt_stroke", "params": {
        "object": "Head", "landmark": "nose", "brush": "grab", "direction": "out", "distance": 0.8}}])
    depth = lambda r: next(o for o in r["result"]["scene"]["objects"] if o["name"] == "Head")["dimensions"][1]
    assert depth(after) > depth(before) + 0.03


def test_a_landmark_follows_the_surface_it_is_on():
    """Pull the nose out twice by landmark: the second pull starts from the
    tip where the first left it, so the nose grows twice as far."""
    _blender_or_skip()
    pull = {"action": "sculpt_stroke", "params": {
        "object": "Head", "landmark": "nose", "brush": "grab", "direction": "forward",
        "distance": 0.6}}
    base = _landmark_points()
    once = _landmark_points([pull])
    twice = _landmark_points([pull, pull])
    depth = lambda r: next(o for o in r["result"]["scene"]["objects"] if o["name"] == "Head")["dimensions"][1]
    first, second = depth(once) - depth(base), depth(twice) - depth(once)
    assert second == pytest.approx(first, rel=0.35)


def test_a_missing_landmark_is_named():
    _blender_or_skip()
    result = blender_actions.run_actions(list(_HEAD) + [{"action": "sculpt_stroke", "params": {
        "object": "Head", "landmark": "tail", "brush": "draw"}}], timeout=300)
    assert not result["success"]
    assert "no landmark called 'tail'" in result["output"]


def test_set_landmark_wins_over_find(tmp_path):
    _blender_or_skip()
    result = _landmark_points([
        {"action": "set_landmark", "params": {"object": "Head", "name": "Nose",
                                              "point": [0.3, -0.4, 1.2], "radius": 0.05}},
        {"action": "find_landmarks", "params": {"object": "Head"}},
    ])
    note = [s for s in result["result"]["steps"] if s.get("step") == "find_landmarks"][-1]
    assert "nose" in note["kept"]


def test_showing_landmarks_leaves_nothing_behind(tmp_path):
    _blender_or_skip()
    result = _landmark_points([{"action": "render_preview", "params": {
        "path": str(tmp_path / "m.png"), "look": "clay", "size": 96, "views": ["front"],
        "show_landmarks": True}}])
    scene = result["result"]["scene"]
    assert [o["name"] for o in scene["objects"]] == ["Head"]
    assert not any(m.startswith("ARIA_Preview") for m in scene["materials"])
    assert (tmp_path / "m.png").is_file()


# ======================================================
# Bodies
# ======================================================

@pytest.mark.parametrize("said, landmark, brush, direction", [
    ("make his shoulders broader", "shoulder_l", "grab", "outward"),
    ("give him bigger arms", "upper_arm_l", "inflate", None),
    ("make the arms longer", "hand_l", "grab", "along"),
    ("make the legs longer", "ankle_l", "grab", "along"),
    ("make the waist thinner", "waist_l", "grab", "in"),
    ("make the belly bigger", "belly", "inflate", None),
    ("flatten the stomach", "belly", "flatten", None),
    ("make the chest broader", "pec_l", "grab", "outward"),
    ("make his butt bigger", "buttock_l", "inflate", None),
    ("make the back flatter", "back", "flatten", None),
    ("make the neck thicker", "neck", "inflate", None),
    ("make the calves bigger", "calf_l", "inflate", None),
    ("pull the chin back", "chin", "grab", "back"),
])
def test_body_changes_are_aimed_at_body_landmarks(said, landmark, brush, direction):
    stroke = language.plan(said)["strokes"][0]
    assert (stroke["landmark"], stroke["brush"], stroke.get("direction")) == (landmark, brush, direction)


def test_the_back_of_the_head_is_not_the_back():
    assert language.plan("make the back of the head flatter")["strokes"][0]["landmark"] == "back_of_head"


def test_body_replies_say_the_parts_properly():
    assert language.plan("make the calves bigger")["summary"] == "calves bigger"
    assert language.plan("make the waist thinner")["summary"] == "waist smaller"


@pytest.fixture
def one_body(monkeypatch):
    runs = []
    entry = {"name": "Body", "type": "MESH", "dimensions": [0.8, 0.3, 1.8], "faces": 12502}
    monkeypatch.setattr(bs.Session, "describe", lambda self: {"success": True, "scene": {
        "objects": [entry]}})

    def run(self, actions, **kwargs):
        runs.append(actions)
        return {"success": True, "ran": True, "renders": ["C:/r.png"], "text": ""}

    monkeypatch.setattr(bs.Session, "run", run)
    return runs, entry


def test_a_standing_figure_is_marked_as_a_body_and_subdivided_first(one_body):
    runs, _ = one_body
    answer = bs.answer_command("make the nose bigger in Blender")
    names = [a["action"] for a in runs[0]]
    assert names[:3] == ["apply_subdivision", "smooth_shade", "find_landmarks"]
    assert runs[0][2]["params"]["kind"] == "body"
    assert "subdivided it once" in answer["text"] and "marked the body" in answer["text"]


def test_a_dense_model_is_not_subdivided(one_body):
    runs, entry = one_body
    entry["faces"] = 120000
    bs.answer_command("make the nose bigger in Blender")
    assert runs[0][0]["action"] == "find_landmarks"


def test_a_marked_model_is_not_subdivided_again(one_body):
    runs, entry = one_body
    entry["landmarks"] = ["nose"]
    bs.answer_command("make the nose bigger in Blender")
    assert [a["action"] for a in runs[0]] == ["sculpt_stroke"] * 3


def test_a_head_asked_about_its_arms_gets_body_marks(one_body):
    runs, entry = one_body
    entry.update(dimensions=[1.0, 1.0, 1.0], faces=120000, landmarks=["nose", "chin"])
    bs.answer_command("give him bigger arms in Blender")
    assert runs[0][0] == {"action": "find_landmarks", "params": {"object": "Body", "kind": "body"}}


# ======================================================
# Body landmarks on the real base mesh
# ======================================================

_BUNDLE = (__import__("pathlib").Path(__file__).resolve().parents[2]
           / "aria_models" / "blender" / "meshes" / "human_base_meshes_bundle.blend")


def _base_body(extra=()):
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
    ] + list(extra) + [{"action": "describe_scene"}], timeout=600)
    assert result["success"], result["output"][-2500:]
    return result


def test_body_heights_match_the_measured_base_mesh():
    """The same heights aria_models/measure_base_landmarks.py measured offline."""
    result = _base_body()
    note = next(s for s in result["result"]["steps"] if s.get("step") == "find_landmarks")
    import json
    from pathlib import Path
    table = json.loads((Path(__file__).resolve().parents[2] / "aria_models"
                        / "landmarks_human_base_meshes.json").read_text())
    measured = table["bases"]["GEO-body_male_stylized"]["z"]
    for name in ("crotch", "knee", "waist", "chest", "armpit", "neck"):
        assert note["heights"][name] == pytest.approx(measured[name], abs=0.03), name


def test_a_body_gets_body_face_and_limb_landmarks():
    result = _base_body()
    body = next(o for o in result["result"]["scene"]["objects"] if o["name"] == "Body")
    expected = {"neck", "chest", "belly", "back", "shoulder_l", "shoulder_r", "elbow_l", "hand_r",
                "knee_l", "calf_r", "foot_l", "waist_l", "hip_r", "nose", "chin", "ear_l"}
    assert expected <= set(body["landmarks"])


def test_longer_arms_reach_further_down():
    before = _base_body()
    after = _base_body([{"action": "sculpt_stroke", "params": {
        "object": "Body", "landmark": "hand_l", "brush": "grab", "direction": "along",
        "distance": 1.0, "size": 2.4, "mirror": "X"}}])
    ok = lambda r: next(s for s in r["result"]["steps"] if s.get("step") == "sculpt_stroke")
    assert ok(after)["vertices_moved"] > 100
    assert ok(after)["largest_move"] > 0.02


# ======================================================
# T-pose
# ======================================================

def _primitive_figure(arms: str, extra=()):
    """A figure welded from primitives: legs, hips, torso, neck, head, nose, arms.

    arms: "out" (T-pose), "down" (A-pose, 45 degrees) or "sides" (hanging
    against the body -- the pose that cannot be read).
    """
    _blender_or_skip()
    arm_steps = {
        "out": [("ArmL", [0.52, 0, 1.40], [0, 90, 0]), ("ArmR", [-0.52, 0, 1.40], [0, 90, 0])],
        "down": [("ArmL", [0.40, 0, 1.18], [0, 45, 0]), ("ArmR", [-0.40, 0, 1.18], [0, -45, 0])],
        "sides": [("ArmL", [0.20, 0, 1.12], [0, 0, 0]), ("ArmR", [-0.20, 0, 1.12], [0, 0, 0])],
    }[arms]
    steps = [
        {"action": "clear_scene"},
        {"action": "add_cylinder", "params": {"name": "LegL", "radius": 0.08, "depth": 0.86, "location": [0.1, 0, 0.43]}},
        {"action": "add_cylinder", "params": {"name": "LegR", "radius": 0.08, "depth": 0.86, "location": [-0.1, 0, 0.43]}},
        {"action": "add_sphere", "params": {"name": "Hips", "radius": 0.19, "location": [0, 0, 0.9]}},
        {"action": "add_cylinder", "params": {"name": "Torso", "radius": 0.17, "depth": 0.62, "location": [0, 0, 1.17]}},
        {"action": "add_cylinder", "params": {"name": "Neck", "radius": 0.055, "depth": 0.14, "location": [0, 0, 1.52]}},
        {"action": "add_sphere", "params": {"name": "Head", "radius": 0.12, "location": [0, 0, 1.68]}},
        {"action": "add_sphere", "params": {"name": "Nose", "radius": 0.03, "location": [0, -0.12, 1.66]}},
    ]
    for name, where, turn in arm_steps:
        steps.append({"action": "add_cylinder", "params": {"name": name, "radius": 0.05,
                                                          "depth": 0.72, "location": where}})
        steps.append({"action": "rotate", "params": {"object": name, "x": turn[0], "y": turn[1], "z": turn[2]}})
    steps += [
        {"action": "join_objects", "params": {"objects": ["Torso", "LegL", "LegR", "Hips", "Neck",
                                                         "Head", "Nose", "ArmL", "ArmR"]}},
        {"action": "voxel_remesh", "params": {"object": "Torso", "size": 0.015}},
        {"action": "find_landmarks", "params": {"object": "Torso", "kind": "body"}},
    ] + list(extra) + [{"action": "describe_scene"}]
    result = blender_actions.run_actions(steps, timeout=600)
    assert result["success"], result["output"][-2500:]
    note = next(s for s in result["result"]["steps"] if s.get("step") == "find_landmarks")
    figure = next(o for o in result["result"]["scene"]["objects"] if o["name"] == "Torso")
    return note, figure


def test_a_t_pose_is_recognised_and_its_arms_marked_along_them():
    note, figure = _primitive_figure("out")
    assert any("T-pose" in n for n in note["notes"])
    marks = figure["landmarks"]
    # Out along the arm, in order: upper arm, elbow, wrist, hand.
    for side, sign in (("l", 1), ("r", -1)):
        xs = [marks[f"{p}_{side}"][0] * sign for p in ("upper_arm", "elbow", "wrist", "hand")]
        assert xs == sorted(xs) and xs[0] > 0.2
        assert abs(marks[f"hand_{side}"][2] - 1.40) < 0.1          # still at shoulder height


def test_a_t_pose_chest_is_the_torso_not_the_arms():
    note, figure = _primitive_figure("out")
    marks = figure["landmarks"]
    assert abs(marks["pec_l"][0]) < 0.2 and abs(marks["pec_r"][0]) < 0.2
    assert 1.0 < note["heights"]["chest"] < 1.40
    assert marks["chest"][2] > marks["belly"][2]


def test_an_a_pose_figure_still_reads_as_one():
    note, figure = _primitive_figure("down")
    assert not any("T-pose" in n for n in note["notes"])
    assert {"elbow_l", "hand_r"} <= set(figure["landmarks"])
    assert figure["landmarks"]["hand_l"][2] < figure["landmarks"]["elbow_l"][2]


def test_arms_against_the_body_are_left_unmarked_and_it_says_so():
    note, figure = _primitive_figure("sides")
    assert any("not marked" in n for n in note["notes"])
    assert "hand_l" not in figure["landmarks"]
    assert "chest" in figure["landmarks"]


def test_longer_arms_on_a_t_pose_reach_further_out():
    _, before = _primitive_figure("out")
    _, after = _primitive_figure("out", [{"action": "sculpt_stroke", "params": {
        "object": "Torso", "landmark": "hand_l", "brush": "grab", "direction": "along",
        "distance": 0.8, "size": 2.4, "mirror": "X"}}])
    assert after["dimensions"][0] > before["dimensions"][0] + 0.05
    assert abs(after["dimensions"][2] - before["dimensions"][2]) < 0.02
