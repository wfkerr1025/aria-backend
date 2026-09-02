"""Turning what a generator made into something Unity can use.

WRITTEN AGAINST A MEASUREMENT, NOT A GUESS
------------------------------------------
One Ludo character was bought and inspected before any of this was
designed. What it showed:

    height 0.9941          Unity reads a unit as a metre, so a
                           character arrives at half size
    floor  -0.4962         the origin is at the model's centre, so
                           placing it on the ground buries it
    boundary edges 13,542  and NONE with three or more faces
    vertices 18,442        of which 9,790 are distinct

The boundary edges are not damage. glTF stores attributes per vertex,
so it splits vertices at every UV seam and each seam edge then reads
as a boundary. Merging them looks like free savings.

It is not free. On that model the merge created 727 edges with three
or more faces where there had been zero. So merging is off by default,
and when asked for it is checked and discarded if it does harm --
which is the single most important thing these tests hold in place.

BLENDER IS NOT RUN HERE. cli_runner is replaced, so what is tested is
the script that would be sent and the decisions around it. The stage
itself was verified against the real binary and the real model, and
those numbers are in the docstrings above.
"""

from __future__ import annotations

import json

import pytest

from backend.blender import blender_actions as actions
from backend.blender import blender_script_templates as templates


@pytest.fixture
def sent(monkeypatch, tmp_path):
    """Capture the script Blender would have been given."""
    from backend.core import cli_runner

    captured = {}

    def fake_run(executable, arguments, **kwargs):
        script = arguments[arguments.index("--python") + 1]
        captured["arguments"] = arguments
        captured["source"] = open(script, encoding="utf-8").read()
        body = json.dumps(captured.get("result") or {
            "created": [], "modified": [], "exported": [], "steps": [],
            "measurements": [
                {"meshes": 1, "vertices": 18442, "triangles": 20246,
                 "ngons": 0, "boundary_edges": 13542, "broken_edges": 0,
                 "loose_vertices": 0, "uv_layers": 1, "materials": 1,
                 "images": 2, "width": 0.8872, "depth": 0.3698,
                 "height": 0.9941, "floor": -0.4962},
                {"meshes": 1, "vertices": 18442, "triangles": 20246,
                 "ngons": 0, "boundary_edges": 13542, "broken_edges": 0,
                 "loose_vertices": 0, "uv_layers": 1, "materials": 1,
                 "images": 2, "width": 1.6066, "depth": 0.6697,
                 "height": 1.8, "floor": 0.0},
            ]})
        return {"success": True, "error": None,
                "output": f"{templates.RESULT_OPEN}{body}{templates.RESULT_CLOSE}"}

    monkeypatch.setattr(cli_runner, "run", fake_run)
    monkeypatch.setenv(actions.ENV_BLENDER, str(tmp_path / "blender.exe"))
    (tmp_path / "blender.exe").write_text("not really blender")
    return captured


@pytest.fixture
def model(tmp_path):
    path = tmp_path / "character.glb"
    path.write_bytes(b"glTF\x02\x00\x00\x00")
    return path


# ======================================================
# The stage
# ======================================================

def test_the_whole_stage_is_one_blender_launch(sent, model):
    """Blender starts cold every time and that start is seconds. Eight
    calls would pay it eight times -- and lose the scene between them,
    because nothing survives the process."""
    actions.clean_for_unity(str(model))

    assert sent["arguments"].count("--python") == 1


def test_it_scales_and_re_origins(sent, model):
    actions.clean_for_unity(str(model), height=1.8)

    source = sent["source"]
    assert "scale_to_height" in source
    assert "origin_to_floor" in source


def test_it_measures_before_and_after(sent, model):
    """The same measurement twice is the only honest way to say what a
    cleanup did."""
    answer = actions.clean_for_unity(str(model))

    assert answer["before"]["height"] == 0.9941
    assert answer["after"]["height"] == 1.8
    assert answer["after"]["floor"] == 0.0


def test_the_target_height_reaches_the_script(sent, model):
    actions.clean_for_unity(str(model), height=2.4)

    assert "2.4" in sent["source"]


def test_a_person_is_the_default_height(sent, model):
    actions.clean_for_unity(str(model))

    assert actions.DEFAULT_HEIGHT == 1.8
    assert "1.8" in sent["source"]


# ======================================================
# Merging, which is the part that can do harm
# ======================================================

def test_merging_is_off_by_default(sent, model):
    """Unity re-splits vertices on import, so the duplicates cost
    nothing there -- and merging them cost 727 broken edges on the one
    model this was measured against."""
    actions.clean_for_unity(str(model))

    assert "merge_by_distance" not in sent["source"]


def test_merging_happens_when_it_is_asked_for(sent, model):
    actions.clean_for_unity(str(model), merge=True)

    assert "merge_by_distance" in sent["source"]


def test_a_merge_that_breaks_the_mesh_is_discarded():
    """The guard, in the generated script. Verified against the real
    model too: it reported reverted_edges=727 and changed nothing,
    which is the exact number the probe predicted."""
    source = templates.build_script(
        [{"action": "merge_by_distance", "params": {}}])

    assert "_now_broken > _was_broken" in source
    assert "_reverted" in source


def test_the_merge_guard_can_be_overridden():
    source = templates.build_script(
        [{"action": "merge_by_distance", "params": {"allow_damage": True}}])

    assert "_allow_damage = bool(True)" in source


def test_the_merge_distance_is_the_one_asked_for():
    source = templates.build_script(
        [{"action": "merge_by_distance", "params": {"distance": 0.01}}])

    assert "_distance = 0.01" in source


# ======================================================
# Refusals, before Blender is started
# ======================================================

def test_a_file_that_is_not_there_is_refused(sent, tmp_path):
    answer = actions.clean_for_unity(str(tmp_path / "nothing.glb"))

    assert answer["success"] is False
    assert answer["ran"] is False
    assert "no file" in answer["error"]


def test_no_file_at_all_is_refused(sent):
    answer = actions.clean_for_unity("")

    assert answer["success"] is False
    assert answer["ran"] is False


def test_an_export_format_nobody_has_is_refused(sent, model):
    answer = actions.clean_for_unity(str(model), export="usdz")

    assert answer["success"] is False
    assert answer["ran"] is False
    assert "usdz" in answer["error"]


def test_each_export_format_uses_its_own_action(sent, model):
    for kind, action in actions.EXPORT_ACTIONS.items():
        actions.clean_for_unity(str(model), export=kind)
        assert action in sent["source"], kind


def test_the_output_sits_beside_the_input_by_default(sent, model):
    answer = actions.clean_for_unity(str(model))

    assert answer["output"].endswith("character_clean.fbx")


def test_a_named_output_is_used(sent, model, tmp_path):
    where = str(tmp_path / "elsewhere" / "hero.fbx")
    answer = actions.clean_for_unity(str(model), out=where)

    assert answer["output"] == where


# ======================================================
# The warnings, which are the gate a pipeline reads
# ======================================================

def test_a_good_model_warns_about_nothing():
    good = {"meshes": 1, "uv_layers": 1, "materials": 1, "images": 2,
            "broken_edges": 0, "ngons": 0, "triangles": 20246}

    assert actions._warnings_about(good, good) == []


def test_broken_edges_are_called_out_for_what_they_cost():
    warnings = actions._warnings_about(
        {"meshes": 1, "uv_layers": 1, "materials": 1, "images": 1,
         "broken_edges": 727, "ngons": 0}, {})

    assert any("727" in w and "rig" in w for w in warnings)


def test_a_model_with_no_uvs_is_called_out():
    warnings = actions._warnings_about(
        {"meshes": 1, "uv_layers": 0, "materials": 1, "images": 1}, {})

    assert any("UV" in w for w in warnings)


def test_a_model_with_no_texture_says_it_will_import_grey():
    warnings = actions._warnings_about(
        {"meshes": 1, "uv_layers": 1, "materials": 1, "images": 0}, {})

    assert any("grey" in w for w in warnings)


def test_an_empty_file_is_the_first_thing_said():
    warnings = actions._warnings_about({"meshes": 0}, {})

    assert "there is no mesh in the file" in warnings[0]


def test_a_cleanup_that_added_geometry_is_reported():
    """It should only ever remove. Growing means something ran that
    nobody asked for."""
    warnings = actions._warnings_about(
        {"meshes": 1, "uv_layers": 1, "materials": 1, "images": 1,
         "broken_edges": 0, "ngons": 0, "triangles": 40000},
        {"triangles": 20246})

    assert any("added geometry" in w for w in warnings)


def test_warnings_are_not_offered_when_the_run_failed(sent, model, monkeypatch):
    from backend.core import cli_runner
    monkeypatch.setattr(cli_runner, "run", lambda *a, **k: {
        "success": False, "error": "Blender crashed", "output": ""})

    answer = actions.clean_for_unity(str(model))

    assert answer["success"] is False
    assert answer["warnings"] == []
    assert answer["output"] == ""


# ======================================================
# The templates themselves
# ======================================================

def test_every_cleanup_template_produces_valid_python():
    for action in ("import_model", "measure_mesh", "remove_loose",
                   "recalculate_normals", "merge_by_distance",
                   "scale_to_height", "origin_to_floor"):
        params = {"path": "x.glb"} if action == "import_model" else {}
        compile(templates.build_script([{"action": action, "params": params}]),
                action, "exec")


def test_import_reads_the_format_from_the_name():
    source = templates.build_script(
        [{"action": "import_model", "params": {"path": "a.glb"}}])

    for spelling in ("import_scene.gltf", "import_scene.fbx", "wm.obj_import"):
        assert spelling in source


def test_measuring_changes_nothing():
    """It is the gate between stages, so it must be safe to call at
    any point, including twice in one run."""
    # The TEMPLATE's own output, not the whole script -- the shared
    # preamble defines _clear_scene, so asserting against the full file
    # matches Blender code this step never runs.
    source = templates.TEMPLATES["measure_mesh"]({})

    for mutating in ("bpy.ops.object.delete", "remove_doubles", ".location =",
                     ".scale =", "bmesh.ops."):
        assert mutating not in source


def test_scaling_refuses_a_height_of_zero():
    source = templates.build_script(
        [{"action": "scale_to_height", "params": {"height": 0}}])

    assert "is not a height" in source
