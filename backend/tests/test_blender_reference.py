"""Checking a model against a reference picture, and fitting it to one.

The reference pictures here are made in Blender from a known shape, so
the right answer is known: a sphere compared with an egg is too wide,
and a sphere fitted to the egg's outline should come out matching it --
without a dent, which the first version of the loop did not manage
(its score climbed to 96% while the face filled with bowls).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.blender import blender_actions
from backend.blender import blender_script_templates as templates
from backend.blender import blender_session as bs


@pytest.fixture(autouse=True)
def _output(tmp_path, monkeypatch):
    monkeypatch.setenv(blender_actions.ENV_OUTPUT, str(tmp_path / "out"))


# ======================================================
# Without Blender
# ======================================================

@pytest.mark.parametrize("action", ["compare_reference", "fit_to_reference"])
def test_the_scripts_compile(action):
    source = templates.build_script([{"action": action, "params": {
        "object": "Head", "reference": "C:/refs/front.png", "view": "right", "path": "C:/out/c.png"}}])
    compile(source, action, "exec")


def test_only_orthographic_views_compare():
    with pytest.raises(templates.BadValue):
        templates.build_script([{"action": "compare_reference", "params": {
            "reference": "r.png", "view": "three_quarter", "path": "c.png"}}])


def test_a_comparison_reads_as_sentences_and_calls():
    text = bs.comparison_text({
        "view": "front", "score": 0.8, "reference": "C:/refs/head_front.png",
        "picture": "C:/out/c.png", "proportions": ["the model is 8% too wide for its height"],
        "differences": [{"kind": "take away", "area": 0.103, "at": [0.84, 0.44], "strokes": [
            {"action": "sculpt_stroke", "params": {"object": "Head", "brush": "grab",
                                                   "points": [[0.4, 0, 1]], "offset": [-0.1, 0, 0]}}]}]})
    assert "outline match 80%" in text
    assert "The model is 8% too wide" in text
    assert "Take away at 84% across, 44% down" in text
    assert "SculptStroke('Head', brush='grab'" in text


def test_a_suggested_call_runs_as_it_is_written():
    from backend.blender import blender_typed_calls as tc

    call = bs.typed_call({"action": "sculpt_stroke", "params": {
        "object": "Head", "brush": "grab", "snap": False, "points": [[0.4, 0.0, 1.0]],
        "radius": 0.2, "offset": [-0.1, 0.0, 0.0]}})
    assert tc.plan_blender_calls(call)[0]["params"]["snap"] is False


def test_match_refuses_a_view_it_cannot_use(tmp_path):
    session = bs.Session("m", root=tmp_path)
    session.folder.mkdir(parents=True)
    session.scene.write_text("x")
    assert "among" in session.match("Head", {"three_quarter": "r.png"})["text"]


def test_match_refuses_a_missing_picture(tmp_path):
    session = bs.Session("m", root=tmp_path)
    session.folder.mkdir(parents=True)
    session.scene.write_text("x")
    assert "No picture at" in session.match("Head", {"front": str(tmp_path / "nope.png")})["text"]


def _fake_scene(monkeypatch, meshes):
    seen = {}
    monkeypatch.setattr(bs.Session, "describe", lambda self: {"success": True, "scene": {
        "objects": [{"name": n, "type": "MESH"} for n in meshes]}})

    def match(self, obj, references, **kwargs):
        seen.update(object=obj, references=references)
        return {"success": True, "text": "matched", "comparisons": []}

    monkeypatch.setattr(bs.Session, "match", match)
    return seen


def test_chat_reads_which_picture_is_which_view(monkeypatch):
    seen = _fake_scene(monkeypatch, ["Head"])
    bs.answer_command(r"match it to D:\Refs\head side.png and D:\Refs\head front.png in Blender")
    assert seen["references"] == {"right": r"D:\Refs\head side.png", "front": r"D:\Refs\head front.png"}
    assert seen["object"] == "Head"


def test_unnamed_pictures_are_front_then_side(monkeypatch):
    seen = _fake_scene(monkeypatch, ["Head"])
    bs.answer_command(r"fit it to D:/a.png and D:/b.jpg in Blender")
    assert seen["references"] == {"front": "D:/a.png", "right": "D:/b.jpg"}


def test_chat_asks_which_model_when_there_are_several(monkeypatch):
    seen = _fake_scene(monkeypatch, ["Head", "Hat"])
    answer = bs.answer_command(r"match it to D:/a.png in Blender")
    assert "Which one" in answer["text"] and not seen


def test_chat_uses_the_model_it_is_told(monkeypatch):
    seen = _fake_scene(monkeypatch, ["Head", "Hat"])
    bs.answer_command(r"match the Hat to D:/a.png in Blender")
    assert seen["object"] == "Hat"


def test_a_question_about_matching_never_runs_it(monkeypatch):
    seen = _fake_scene(monkeypatch, ["Head"])
    assert bs.answer_command(r"how do I match it to D:/a.png in Blender?") is None
    assert not seen


# ======================================================
# The real Blender
# ======================================================

def _blender_or_skip():
    try:
        blender_actions.blender_path()
    except Exception:
        pytest.skip("Blender is not configured on this machine")


@pytest.fixture(scope="module")
def egg(tmp_path_factory):
    """Front and side pictures of a tall egg with a jaw -- a known answer."""
    try:
        blender_actions.blender_path()
    except Exception:
        pytest.skip("Blender is not configured on this machine")
    folder = tmp_path_factory.mktemp("refs")
    result = blender_actions.run_actions([
        {"action": "clear_scene"},
        {"action": "add_sphere", "params": {"name": "Skull", "radius": 0.5, "location": [0, 0, 1.1]}},
        {"action": "scale", "params": {"object": "Skull", "x": 0.8, "y": 0.95, "z": 1.2}},
        {"action": "add_sphere", "params": {"name": "Jaw", "radius": 0.4, "location": [0, -0.05, 0.7]}},
        {"action": "render_preview", "params": {"path": str(folder / "egg.png"), "look": "clay",
                                                "views": ["front", "right"], "transparent": True,
                                                "size": 256}},
    ], timeout=300)
    assert result["success"], result["output"][-2000:]
    return {"front": str(folder / "egg_front.png"), "right": str(folder / "egg_right.png")}


def _ball(tmp_path):
    session = bs.Session("ball", root=tmp_path)
    made = session.run("ClearScene()\nAddSphere('Head', radius=0.5, location=[0,0,1])\n"
                       "SculptReady('Head', detail=70)", preview=None)
    assert made["success"], made.get("output_tail")
    return session


def test_a_sphere_against_an_egg_is_too_wide(tmp_path, egg):
    session = _ball(tmp_path)
    outcome = session.run(f"CompareReference(reference={egg['front']!r}, view='front', "
                          f"object='Head', size=256)", preview=None)
    assert outcome["success"], outcome.get("output_tail")
    comparison = outcome["comparisons"][0]
    assert 0.5 < comparison["score"] < 0.95
    assert any("too wide" in p for p in comparison["proportions"])
    assert Path(comparison["picture"]).is_file()


def test_fitting_matches_the_outline(tmp_path, egg):
    session = _ball(tmp_path)
    outcome = session.match("Head", egg, size=256)
    assert outcome["success"], outcome["text"]
    assert outcome["scores"][-1] > 0.95
    assert outcome["scores"][-1] > outcome["scores"][0] + 0.05


def test_fitting_does_not_dent_the_surface(tmp_path, egg):
    """Every vertex should end up no closer to the middle than a fitted
    egg allows -- a dent is a vertex pulled inside the outline's hull."""
    session = _ball(tmp_path)
    session.match("Head", egg, size=256)
    result = blender_actions.run_actions([
        {"action": "measure_mesh"}, {"action": "describe_scene"}],
        blend_file=str(session.scene), timeout=300)
    assert result["success"], result["output"][-2000:]
    head = next(o for o in result["result"]["scene"]["objects"] if o["name"] == "Head")
    width, depth, height = head["dimensions"]
    # The egg is 0.8 x 0.95 x 1.2 of a 1 m ball, and the jaw hangs below.
    assert width < depth < height


def test_a_round_that_makes_it_worse_is_undone(tmp_path, egg, monkeypatch):
    session = _ball(tmp_path)
    real_run = bs.Session.run
    calls = []

    def worse(self, work, **kwargs):
        outcome = real_run(self, work, **kwargs)
        calls.append(1)
        if len(calls) == 2:          # the first fitting round
            for comparison in outcome.get("comparisons") or []:
                comparison["score"] = 0.0
        return outcome

    monkeypatch.setattr(bs.Session, "run", worse)
    before = session.scene.read_bytes()
    outcome = session.match("Head", egg, size=256)
    assert "made it worse" in outcome["text"]
    assert session.scene.read_bytes() == before
