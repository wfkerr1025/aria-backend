"""Shape keys (expressions) and weight transfer (clothing that follows the body).

Everything that matters here was measured in the real Blender, so most
of these run it: an expression is only an expression if dialling it in
moves the mesh and dialling it out puts it back; a garment only follows
the body if bending the body bends the garment; and a shape key only
reaches Unity if it survives the FBX -- which, with a live modifier on
the mesh, it did not, silently.
"""

from __future__ import annotations

import pytest

from backend.blender import blender_actions
from backend.blender import blender_script_templates as templates


def _blender_or_skip():
    try:
        blender_actions.blender_path()
    except Exception:
        pytest.skip("Blender is not configured on this machine")


def _run(steps, **kwargs):
    result = blender_actions.run_actions(list(steps) + [{"action": "describe_scene"}],
                                         timeout=300, **kwargs)
    return result


def _obj(result, name):
    return next(o for o in result["result"]["scene"]["objects"] if o["name"] == name)


def _step(action, **params):
    return {"action": action, "params": params}


_BALL = [
    _step("clear_scene"),
    _step("add_sphere", name="Ball", radius=0.5, location=[0, 0, 0]),
    _step("sculpt_ready", object="Ball", detail=50),
]


# ======================================================
# Without Blender
# ======================================================

@pytest.mark.parametrize("action, params", [
    ("add_shape_key", {"object": "Ball", "name": "Smile", "from_mix": True}),
    ("set_shape_key", {"object": "Ball", "name": "Smile", "value": 0.5, "frame": 12}),
    ("remove_shape_key", {"object": "Ball", "name": "Smile"}),
    ("mirror_shape_key", {"object": "Ball", "name": "Blink_L", "axis": "X"}),
    ("transfer_weights", {"source": "Body", "target": "Shirt", "method": "projected"}),
    ("apply_modifiers", {"object": "Ball"}),
])
def test_the_scripts_compile(action, params):
    compile(templates.build_script([_step(action, **params)]), action, "exec")


def test_an_unknown_transfer_method_is_refused():
    with pytest.raises(templates.BadValue, match="nearest_face"):
        templates.build_script([_step("transfer_weights", source="A", target="B", method="glue")])


def test_a_mirror_axis_is_an_axis():
    with pytest.raises(templates.BadValue):
        templates.build_script([_step("mirror_shape_key", object="A", name="k", axis="W")])


def test_every_typed_call_takes_what_its_template_reads():
    """AddBone's wrapper never grew parent= although the template reads it."""
    from backend.blender import blender_typed_calls as tc

    planned = tc.plan_blender_calls("AddBone('Rig', 'Lower', head=[0,0,1], tail=[0,0,2], parent='Upper')")
    assert planned[0]["params"]["parent"] == "Upper"


# ======================================================
# Shape keys, in Blender
# ======================================================

def test_an_expression_moves_the_mesh_only_when_dialled_in():
    _blender_or_skip()
    bump = [_step("add_shape_key", object="Ball", name="Puff"),
            _step("sculpt_stroke", object="Ball", shape_key="Puff", brush="draw",
                  points=[[0.5, 0, 0]], radius=0.2, strength=4.0)]
    rest = _run(_BALL + bump)
    assert rest["success"], rest["output"][-2500:]
    on = _run(_BALL + bump + [_step("set_shape_key", object="Ball", name="Puff", value=1.0)])
    assert on["success"], on["output"][-2500:]
    base = _obj(rest, "Ball")
    assert base["shape_keys"] == ["Basis", "Puff"]
    assert _obj(on, "Ball")["dimensions"][0] > base["dimensions"][0] + 0.02
    plain = _run(_BALL)
    assert abs(base["dimensions"][0] - _obj(plain, "Ball")["dimensions"][0]) < 1e-4


def test_adding_a_key_twice_is_not_an_error():
    _blender_or_skip()
    result = _run(_BALL + [_step("add_shape_key", object="Ball", name="Puff"),
                           _step("add_shape_key", object="Ball", name="Puff")])
    assert result["success"], result["output"][-2500:]
    assert _obj(result, "Ball")["shape_keys"] == ["Basis", "Puff"]


def test_the_base_cannot_be_sculpted_under_keys():
    _blender_or_skip()
    result = _run(_BALL + [_step("add_shape_key", object="Ball", name="Puff"),
                           _step("sculpt_stroke", object="Ball", brush="draw", points=[[0.5, 0, 0]])])
    assert not result["success"]
    assert "name one with shape_key" in result["output"]


def test_the_basis_goes_last():
    _blender_or_skip()
    result = _run(_BALL + [_step("add_shape_key", object="Ball", name="Puff"),
                           _step("remove_shape_key", object="Ball", name="Basis")])
    assert not result["success"]
    assert "the basis goes last" in result["output"]


def test_a_keyframed_expression_is_animated():
    _blender_or_skip()
    result = _run(_BALL + [_step("add_shape_key", object="Ball", name="Puff"),
                           _step("set_shape_key", object="Ball", name="Puff", value=0, frame=1),
                           _step("set_shape_key", object="Ball", name="Puff", value=1, frame=12)])
    assert result["success"], result["output"][-2500:]
    assert result["result"]["scene"]["actions"]


def test_a_mirrored_key_moves_the_other_side():
    _blender_or_skip()
    left = [_step("add_shape_key", object="Ball", name="Bump_L"),
            _step("sculpt_stroke", object="Ball", shape_key="Bump_L", brush="draw",
                  points=[[0.5, 0, 0]], radius=0.2, strength=4.0),
            _step("mirror_shape_key", object="Ball", name="Bump_L")]
    one = _run(_BALL + left + [_step("set_shape_key", object="Ball", name="Bump_L", value=1)])
    both = _run(_BALL + left + [_step("set_shape_key", object="Ball", name="Bump_L", value=1),
                                _step("set_shape_key", object="Ball", name="Bump_R", value=1)])
    plain = _run(_BALL)
    assert one["success"] and both["success"], (one["output"] + both["output"])[-2500:]
    note = next(s for s in both["result"]["steps"] if s.get("step") == "mirror_shape_key")
    assert note["made"] == "Bump_R"
    assert note["vertices_without_a_twin"] == 0
    grew_one = _obj(one, "Ball")["dimensions"][0] - _obj(plain, "Ball")["dimensions"][0]
    grew_both = _obj(both, "Ball")["dimensions"][0] - _obj(plain, "Ball")["dimensions"][0]
    assert grew_both == pytest.approx(grew_one * 2, rel=0.1)


def test_fbx_refuses_to_drop_shape_keys(tmp_path):
    _blender_or_skip()
    result = _run(_BALL + [_step("add_shape_key", object="Ball", name="Puff"),
                           _step("apply_subdivision", object="Ball", levels=1),
                           _step("export_fbx", path=str(tmp_path / "a.fbx"))])
    assert not result["success"]
    assert "would drop the shape keys" in result["output"]
    assert not (tmp_path / "a.fbx").exists()


def test_applying_modifiers_keeps_the_keys_all_the_way_through_fbx(tmp_path):
    _blender_or_skip()
    path = str(tmp_path / "a.fbx")
    built = _run(_BALL + [
        _step("add_shape_key", object="Ball", name="Puff"),
        _step("sculpt_stroke", object="Ball", shape_key="Puff", brush="draw",
              points=[[0.5, 0, 0]], radius=0.2, strength=4.0),
        _step("apply_subdivision", object="Ball", levels=1),
        _step("apply_modifiers", object="Ball"),
        _step("export_fbx", path=path)])
    assert built["success"], built["output"][-2500:]
    ball = _obj(built, "Ball")
    assert "modifiers" not in ball and ball["shape_keys"] == ["Basis", "Puff"]

    back = _run([_step("clear_scene"), _step("import_model", path=path),
                 _step("set_shape_key", object="Ball", name="Puff", value=1)])
    assert back["success"], back["output"][-2500:]
    imported = _obj(back, "Ball")
    assert imported["shape_keys"] == ["Basis", "Puff"]
    assert imported["faces"] == ball["faces"]
    assert imported["dimensions"][0] > 1.02


# ======================================================
# Weight transfer, in Blender
# ======================================================

_ARM = [
    _step("clear_scene"),
    _step("add_cylinder", name="Arm", radius=0.15, depth=2, location=[0, 0, 1]),
    _step("sculpt_ready", object="Arm", detail=50),
    _step("create_armature", name="Rig"),
    _step("add_bone", armature="Rig", name="Upper", head=[0, 0, 0], tail=[0, 0, 1]),
    _step("add_bone", armature="Rig", name="Lower", head=[0, 0, 1], tail=[0, 0, 2],
          parent="Upper", connect=True),
    _step("auto_weights", mesh="Arm", armature="Rig"),
    _step("add_cylinder", name="Sleeve", radius=0.19, depth=0.9, location=[0, 0, 1.1]),
    _step("sculpt_ready", object="Sleeve", detail=35, smooth=False),
]
_BEND = _step("set_pose", armature="Rig", bone="Lower", rotation=[70, 0, 0], frame=1)


def test_a_sleeve_follows_the_arm():
    _blender_or_skip()
    still = _run(_ARM + [_BEND])
    moved = _run(_ARM + [_step("transfer_weights", source="Arm", target="Sleeve"), _BEND])
    assert still["success"] and moved["success"], (still["output"] + moved["output"])[-2500:]
    note = next(s for s in moved["result"]["steps"] if s.get("step") == "transfer_weights")
    assert note["armature"] == "Rig"
    assert note["unweighted"] == 0
    sleeve = _obj(moved, "Sleeve")
    assert sleeve["parent"] == "Rig"
    # Unrigged, the sleeve stands straight while the arm bends; rigged,
    # it bends too -- deeper front to back, shorter top to bottom.
    assert sleeve["dimensions"][1] > _obj(still, "Sleeve")["dimensions"][1] + 0.1
    assert sleeve["dimensions"][2] < _obj(still, "Sleeve")["dimensions"][2] - 0.05


def test_weights_are_taken_at_rest_even_when_posed():
    """Posed first or not, the sleeve gets the same weights."""
    _blender_or_skip()
    rest_first = _run(_ARM + [_step("transfer_weights", source="Arm", target="Sleeve"), _BEND])
    posed_first = _run(_ARM + [_BEND, _step("transfer_weights", source="Arm", target="Sleeve")])
    assert rest_first["success"] and posed_first["success"]
    a, b = _obj(rest_first, "Sleeve")["dimensions"], _obj(posed_first, "Sleeve")["dimensions"]
    assert a == pytest.approx(b, abs=0.01)


def test_a_body_with_no_weights_is_refused():
    _blender_or_skip()
    result = _run([_step("clear_scene"), _step("add_cube", name="Body"),
                   _step("add_cube", name="Shirt", size=2.2),
                   _step("transfer_weights", source="Body", target="Shirt")])
    assert not result["success"]
    assert "no weights to copy" in result["output"]
