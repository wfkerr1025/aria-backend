"""Building shapes: meshes from points, extrude, inset, bevel, loop cuts, curves, text.

Each one is measured in the real Blender -- a face count, a height, a
thickness -- because a modelling operation that "ran" but left the
mesh as it was is the failure that looks like success.
"""

from __future__ import annotations

import pytest

from backend.blender import blender_actions
from backend.blender import blender_script_templates as templates


def _build(action, **params):
    return templates.build_script([{"action": action, "params": params}])


# ======================================================
# Refused before Blender starts
# ======================================================

def test_faces_must_be_described():
    with pytest.raises(templates.BadValue, match="say which faces"):
        _build("extrude_faces", object="Box", distance=1)


def test_a_face_direction_is_a_known_one():
    with pytest.raises(templates.BadValue, match="not a direction"):
        _build("extrude_faces", object="Box", faces={"normal": "sideways"})


def test_a_mesh_face_cannot_name_a_missing_vertex():
    with pytest.raises(templates.BadValue, match="not there"):
        _build("create_mesh", vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], faces=[[0, 1, 5]])


def test_a_curve_needs_two_points():
    with pytest.raises(templates.BadValue, match="two"):
        _build("add_curve", points=[[0, 0, 0]])


def test_text_needs_text():
    with pytest.raises(templates.BadValue):
        _build("add_text", text="  ")


@pytest.mark.parametrize("action, params", [
    ("create_mesh", {"name": "Tri", "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]], "faces": [[0, 1, 2]]}),
    ("extrude_faces", {"object": "Box", "faces": {"box": [[-1, -1, 0.9], [1, 1, 1.1]]}, "distance": 0.5,
                       "direction": [0, 0, 1]}),
    ("inset_faces", {"object": "Box", "faces": {"all": True}, "thickness": 0.1, "depth": -0.05}),
    ("bevel_edges", {"object": "Box", "width": 0.02, "segments": 3, "angle": 0, "profile": 0.5}),
    ("loop_cut", {"object": "Box", "axis": "Y", "count": 4}),
    ("add_curve", {"name": "C", "points": [[0, 0, 0], [1, 1, 1]], "kind": "poly", "thickness": 0.1,
                   "closed": False, "resolution": 8}),
    ("add_text", {"name": "T", "text": "Hi", "size": 1, "extrude": 0.1, "standing": False, "align": "left"}),
    ("convert_to_mesh", {"object": "C"}),
])
def test_every_script_compiles(action, params):
    compile(_build(action, **params), action, "exec")


# ======================================================
# The real Blender
# ======================================================

def _run(*steps):
    try:
        blender_actions.blender_path()
    except Exception:
        pytest.skip("Blender is not configured on this machine")
    result = blender_actions.run_actions(
        [{"action": "clear_scene"}] + [{"action": a, "params": p} for a, p in steps]
        + [{"action": "describe_scene"}], timeout=300)
    assert result["success"], result["output"][-2500:]
    return {o["name"]: o for o in result["result"]["scene"]["objects"]}


_BOX = ("add_cube", {"name": "Box", "size": 2, "location": [0, 0, 1]})


def test_create_mesh_makes_exactly_the_shape_given():
    made = _run(("create_mesh", {"name": "Pyramid", "vertices": [
        [-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0], [0, 0, 1.5]],
        "faces": [[0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4], [3, 2, 1, 0]]}))["Pyramid"]
    assert made["vertices"] == 5 and made["faces"] == 5
    assert made["dimensions"] == pytest.approx([2, 2, 1.5], abs=1e-3)


def test_extruding_the_top_raises_it_by_the_distance():
    box = _run(_BOX, ("extrude_faces", {"object": "Box", "faces": {"normal": "up"}, "distance": 0.6}))["Box"]
    assert box["dimensions"][2] == pytest.approx(2.6, abs=1e-3)
    assert box["faces"] == 6 + 4                                    # a side wall per top edge


def test_a_negative_extrude_makes_a_recess():
    """Inward, the rim stays at the front and a floor sits 0.5 m back --
    the box's outside is unchanged, and it has four new inner walls."""
    box = _run(_BOX, ("extrude_faces", {"object": "Box", "faces": {"normal": "front"},
                                        "distance": -0.5}))["Box"]
    assert box["dimensions"] == pytest.approx([2, 2, 2], abs=1e-3)
    assert box["faces"] == 6 + 4


def test_insetting_the_top_adds_a_border():
    box = _run(_BOX, ("inset_faces", {"object": "Box", "faces": {"normal": "up"}, "thickness": 0.2}))["Box"]
    assert box["faces"] == 6 + 4


def test_bevelling_rounds_every_sharp_edge():
    box = _run(_BOX, ("bevel_edges", {"object": "Box", "width": 0.1, "segments": 3}))["Box"]
    assert box["faces"] > 6 * 4
    assert box["dimensions"] == pytest.approx([2, 2, 2], abs=1e-3)   # same size, softer


def test_loop_cuts_add_rings():
    box = _run(_BOX, ("loop_cut", {"object": "Box", "axis": "Z", "count": 3}))["Box"]
    assert box["faces"] == 2 + 4 * 4


def test_a_thick_curve_becomes_a_tube_mesh():
    made = _run(("add_curve", {"name": "Pipe", "points": [[0, 0, 0], [0, 0, 1], [1, 0, 2]],
                               "thickness": 0.1}),
                ("convert_to_mesh", {"object": "Pipe"}))["Pipe"]
    assert made["type"] == "MESH" and made["faces"] > 50
    assert made["dimensions"][1] == pytest.approx(0.2, abs=0.02)     # the tube's diameter


def test_text_stands_facing_the_front():
    sign = _run(("add_text", {"name": "Sign", "text": "ARIA", "size": 0.5, "extrude": 0.05}))["Sign"]
    # World bounds: an object's own dimensions ignore its rotation.
    low, high = sign["bounds"]
    width, depth, height = (high[i] - low[i] for i in range(3))
    assert width > height > depth                                    # upright, facing -Y


def test_faces_that_match_nothing_say_so():
    try:
        blender_actions.blender_path()
    except Exception:
        pytest.skip("Blender is not configured on this machine")
    result = blender_actions.run_actions([
        {"action": "clear_scene"}, {"action": "add_cube", "params": {"name": "Box"}},
        {"action": "extrude_faces", "params": {"object": "Box", "faces": {"box": [[5, 5, 5], [6, 6, 6]]}}},
    ], timeout=300)
    assert not result["success"]
    assert "no faces of 'Box' match" in result["output"]
