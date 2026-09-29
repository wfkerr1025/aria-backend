"""Sculpting by hand: brushes as arithmetic on the vertices.

Blender's own sculpt brushes cannot run in --background (measured; see
sculpt_brush), so sculpt_stroke moves the vertices itself. The first
half checks what is refused before Blender starts. The second half
sculpts in the real Blender and measures the mesh afterwards -- a brush
is only a brush if the surface actually moved where it was put, and
nowhere else.
"""

from __future__ import annotations

import pytest

from backend.blender import blender_actions
from backend.blender import blender_script_templates as templates


def _script(**params):
    return templates.build_script([{"action": "sculpt_stroke",
                                    "params": {"object": "Head", **params}}])


# ======================================================
# Refused before Blender starts
# ======================================================

def test_every_brush_builds_a_script_that_compiles():
    for brush in sorted(templates.SCULPT_STROKE_BRUSHES):
        compile(_script(brush=brush, points=[[0, -0.5, 1]]), brush, "exec")


def test_points_on_a_picture_compile_with_a_view():
    compile(_script(points=[[0.4, 0.5], [0.6, 0.5]], view="front", mirror="x"), "v", "exec")


def test_a_stroke_needs_points():
    with pytest.raises(templates.BadValue, match="at least one point"):
        _script(points=[])


def test_picture_points_need_a_view():
    with pytest.raises(templates.BadValue, match="say which view"):
        _script(points=[[0.4, 0.5]])


def test_scene_and_picture_points_cannot_be_mixed():
    with pytest.raises(templates.BadValue, match="all"):
        _script(points=[[0.4, 0.5], [0, 0, 1]], view="front")


def test_only_orthographic_views_can_place_a_stroke():
    with pytest.raises(templates.BadValue):
        _script(points=[[0.4, 0.5]], view="three_quarter")


def test_an_unknown_brush_is_refused():
    with pytest.raises(templates.BadValue):
        _script(brush="spray", points=[[0, 0, 0]])


def test_a_mirror_is_an_axis():
    with pytest.raises(templates.BadValue, match="mirror axis"):
        _script(points=[[0, 0, 0]], mirror="sideways")


def test_a_name_cannot_become_code():
    source = _script(points=[[0, 0, 0]], brush="draw")
    hostile = templates.build_script([{"action": "sculpt_stroke", "params": {
        "object": '"); import os; os.system("x', "points": [[0, 0, 0]]}}])
    compile(hostile, "hostile", "exec")
    assert 'import os; os.system("x' in hostile      # present only as text
    assert source.count("\nimport os\n") == hostile.count("\nimport os\n")


def test_the_typed_call_reaches_the_brush():
    from backend.blender import blender_typed_calls as tc

    planned = tc.plan_blender_calls(
        'SculptStroke("Head", brush="crease", view="front", points=[[0.4, 0.7], [0.6, 0.7]], radius=0.03)')
    assert planned[0]["action"] == "sculpt_stroke"
    assert planned[0]["params"]["object"] == "Head"
    assert planned[0]["params"]["brush"] == "crease"


# ======================================================
# The real Blender
# ======================================================

def _blender_or_skip():
    try:
        blender_actions.blender_path()
    except Exception:
        pytest.skip("Blender is not configured on this machine")


_BALL = [
    {"action": "clear_scene"},
    {"action": "add_sphere", "params": {"name": "Ball", "radius": 0.5, "location": [0, 0, 0]}},
    {"action": "sculpt_ready", "params": {"object": "Ball", "detail": 60}},
]


def _run(*strokes):
    result = blender_actions.run_actions(
        _BALL + list(strokes) + [{"action": "describe_scene"}], timeout=300)
    assert result["success"], result["output"][-2500:]
    notes = [s for s in result["result"]["steps"] if s.get("step") == "sculpt_stroke"]
    ball = next(o for o in result["result"]["scene"]["objects"] if o["name"] == "Ball")
    return notes, ball


def _stroke(**params):
    return {"action": "sculpt_stroke", "params": {"object": "Ball", **params}}


def test_sculpt_ready_makes_a_dense_even_mesh():
    _blender_or_skip()
    result = blender_actions.run_actions(_BALL, timeout=300)
    assert result["success"], result["output"][-2500:]
    ready = next(s for s in result["result"]["steps"] if s.get("step") == "sculpt_ready")
    assert ready["faces"] > ready["faces_before"] * 4


def test_draw_raises_the_surface_where_it_was_put():
    _blender_or_skip()
    untouched, ball = _run()
    notes, drawn = _run(_stroke(brush="draw", points=[[0, -0.5, 0]], radius=0.15, strength=3.0))
    assert notes[0]["vertices_moved"] > 0
    # Pushed out toward -Y: the ball is deeper front to back, no wider.
    assert drawn["dimensions"][1] > ball["dimensions"][1] + 0.01
    assert abs(drawn["dimensions"][0] - ball["dimensions"][0]) < 0.005


def test_a_negative_strength_pushes_in():
    _blender_or_skip()
    _, ball = _run()
    _, dented = _run(_stroke(brush="draw", points=[[0, -0.5, 0]], radius=0.15, strength=-3.0))
    assert dented["dimensions"][1] < ball["dimensions"][1] - 0.005


def test_mirror_sculpts_both_sides():
    _blender_or_skip()
    one, _ = _run(_stroke(brush="draw", points=[[0.3, -0.4, 0]], radius=0.1, strength=2.0))
    both, _ = _run(_stroke(brush="draw", points=[[0.3, -0.4, 0]], radius=0.1, strength=2.0, mirror="X"))
    assert both[0]["vertices_moved"] > one[0]["vertices_moved"] * 1.6


def test_grab_carries_the_region_by_the_offset():
    _blender_or_skip()
    notes, ball = _run(_stroke(brush="grab", points=[[0, -0.5, 0]], radius=0.2, offset=[0, -0.2, 0]))
    assert notes[0]["dabs"] == 1
    assert abs(notes[0]["largest_move"] - 0.2) < 0.02
    assert ball["dimensions"][1] > 1.15


def test_smoothing_takes_a_bump_back_down():
    _blender_or_skip()
    bump = _stroke(brush="draw", points=[[0, -0.5, 0]], radius=0.1, strength=3.0)
    _, bumped = _run(bump)
    _, smoothed = _run(bump, *[_stroke(brush="smooth", points=[[0, -0.5, 0]], radius=0.2,
                                       strength=1.0)] * 6)
    assert smoothed["dimensions"][1] < bumped["dimensions"][1]


def test_a_point_on_the_front_view_lands_on_the_front():
    _blender_or_skip()
    _, ball = _run()
    _, drawn = _run(_stroke(brush="draw", view="front", points=[[0.5, 0.5]], radius=0.15, strength=3.0))
    assert drawn["dimensions"][1] > ball["dimensions"][1] + 0.01      # the front grew
    assert abs(drawn["dimensions"][2] - ball["dimensions"][2]) < 0.005   # not the top


def test_a_point_on_empty_space_says_so():
    _blender_or_skip()
    result = blender_actions.run_actions(_BALL + [
        _stroke(brush="draw", view="front", points=[[0.02, 0.02]], radius=0.1)], timeout=300)
    assert not result["success"]
    assert "empty space" in result["output"]


def test_a_mesh_with_shape_keys_is_refused_not_torn():
    """No action adds shape keys yet, so this cannot be run in Blender;
    it checks the refusal is in every stroke's script."""
    assert "has shape keys" in _script(points=[[0, 0, 0]])


def test_a_snake_hook_drags_along_a_free_path_and_snaps_only_its_start():
    """A lock of hair runs out into space: only its root is on the surface."""
    from backend.blender import blender_script_templates as templates
    script = templates.sculpt_stroke({
        "object": "Hair", "brush": "snake_hook", "radius": 0.03, "taper": 0.4, "pinch": 0.0,
        "points": [[0, 0, 1.8], [0.05, 0, 1.85], [0.1, 0, 1.82]]})
    compile(script, "snake_hook", "exec")
    assert "_aria_snake(_sculpt" in script
    assert "!= \"snake_hook\" or _i == 0" in script
    assert "snake_hook" in templates.SCULPT_STROKE_BRUSHES
