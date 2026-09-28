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
    assert actions[0] == {"action": "add_shape_key", "params": {"object": "Head", "name": "Smile"}}
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
