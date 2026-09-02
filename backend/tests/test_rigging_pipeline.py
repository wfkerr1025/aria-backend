"""A static model in, an animatable Unity prefab out.

WHAT WAS MEASURED BEFORE ANY OF THIS WAS WRITTEN
------------------------------------------------
The spec said the post-rig cleanup should be "identical to the
existing cleanup stage". A two-bone armature with automatic weights
was built and put through the static stage to check:

    bones             2 -> 2      vertex groups     2 -> 2
    weighted vertices 24 -> 24    ARMATURE modifier still bound

So it does not break a rig. What it does leave behind is why
blender_rigging exists at all:

    armature root scale  1.000 -> 0.900

scale_to_height multiplies object scale, which is nothing on a prop
and is a retargeting bug on a character. clean_rigged_model applies
transforms afterwards and the same measurement then reads 1.000.

NOTHING PAID AND NOTHING RENDERED RUNS HERE. The Ludo client, the
Blender binary and the Unity CLI are all replaced. Rigging is a
billable call and Unity needs a live Editor, so the parts that cannot
be faked have live instructions instead of tests.
"""

from __future__ import annotations

import json

import pytest

from backend.blender import blender_actions
from backend.blender import blender_rigging
from backend.blender import blender_script_templates as templates
from backend.ludo import ludo_actions
from backend.ludo import ludo_client
from backend.ludo import ludo_rigging
from backend.ludo import ludo_spend as spend
from backend.unity import unity_delivery
from backend.unity import unity_rigging


# The export path as the generated script spells it. _text() uses
# repr, which gives single quotes for an ordinary string, so a
# pattern that only knew double quotes matched nothing and the fake
# Blender silently exported nothing.
_PATH_LINE = r"""_path = (['\"])(.+?)\1"""

MODEL_URL = "https://cdn.ludo.ai/a/model.glb"
RIGGED_URL = "https://cdn.ludo.ai/a/rigged.glb"


# ======================================================
# Fixtures: one for each of the three things that cost
# ======================================================

class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""
        self.content = self.text.encode() if self.text else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def ludo(monkeypatch, tmp_path):
    """Every Ludo POST, recorded. The list length is the bill."""
    monkeypatch.setenv(ludo_client.ENV_KEY, "test-key-not-a-real-one")
    posts = []

    def fake_post(url, json=None, headers=None, timeout=None):
        posts.append({"url": url, "body": json or {}})
        return FakeResponse(200, {"model_url": RIGGED_URL})

    def fake_download(url, path, **kwargs):
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"glTF\x02\x00\x00\x00rigged")
        return {"success": True, "path": path}

    monkeypatch.setattr(ludo_client.requests, "post", fake_post)
    monkeypatch.setattr(ludo_client, "download", fake_download)
    return posts


@pytest.fixture
def blender(monkeypatch, tmp_path):
    """Blender, answering with a rig that survived."""
    from backend.core import cli_runner

    captured = {"rig": {"armatures": 1, "bones": 22, "vertex_groups": 22,
                        "weighted_vertices": 4000, "vertices": 4200,
                        "bound_meshes": ["Body"], "root_scales": [1.0],
                        "bone_names": ["Hips", "Spine"]}}

    def fake_run(executable, arguments, **kwargs):
        from pathlib import Path
        import re as _re
        script = arguments[arguments.index("--python") + 1]
        captured["source"] = open(script, encoding="utf-8").read()

        # Blender writes the file it was told to export. Without this
        # the next stage refuses a path that is not there -- correctly,
        # which is how this fixture gap was found.
        # _text() uses repr, so the path is single-quoted. Accept either.
        for match in _re.finditer(_PATH_LINE, captured["source"]):
            target = Path(match.group(2))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"exported by a fake blender")
        mesh = {"meshes": 1, "vertices": 4200, "triangles": 8000, "ngons": 0,
                "boundary_edges": 0, "broken_edges": 0, "loose_vertices": 0,
                "uv_layers": 1, "materials": 1, "images": 1,
                "width": 0.9, "depth": 0.4, "height": 1.8, "floor": 0.0}
        body = json.dumps({
            "created": [], "modified": [], "exported": [], "steps": [],
            "measurements": [dict(mesh, height=0.99, floor=-0.5), mesh],
            "rigs": [captured["rig"], captured["rig"]]})
        return {"success": True, "error": None,
                "output": f"{templates.RESULT_OPEN}{body}"
                          f"{templates.RESULT_CLOSE}"}

    monkeypatch.setattr(cli_runner, "run", fake_run)
    monkeypatch.setenv(blender_actions.ENV_BLENDER, str(tmp_path / "b.exe"))
    (tmp_path / "b.exe").write_text("not blender")
    return captured


@pytest.fixture
def unity(monkeypatch, tmp_path):
    """The Unity CLI, recorded, answering the way it does."""
    from backend.unity import unity_cli_engine

    root = tmp_path / "Project"
    (root / "Assets").mkdir(parents=True)
    monkeypatch.setattr(unity_delivery, "unity_project_root", lambda: root)

    calls = []

    def fake(command, args=None, **kwargs):
        argv = list(args or [])
        named = {argv[i].lstrip("-"): argv[i + 1]
                 for i in range(0, len(argv) - 1, 2)}
        calls.append({"command": command.replace("cmd ", ""), "args": named})
        data = {"globalId": "GlobalObjectId_V1-2-abc-123-0",
                "hierarchyPath": "/" + named.get("name", "Object")}
        if command.endswith("create_prefab"):
            data = {"assetPath": named.get("path")}
        return {"success": True, "output": "", "error": None,
                "json": {"success": True, "data": data, "errors": []}}

    monkeypatch.setattr(unity_cli_engine, "run_invocation", fake)
    return calls


@pytest.fixture
def work(tmp_path):
    folder = tmp_path / "work"
    folder.mkdir()
    return folder


def commands(calls):
    return [c["command"] for c in calls]


def args_for(calls, command):
    for call in calls:
        if call["command"] == command:
            return call["args"]
    return {}


# ======================================================
# 1. It rigs the model
# ======================================================

def test_it_rigs_model(ludo, work):
    answer = ludo_rigging.rig_character(MODEL_URL, name="hero", folder=work)

    assert answer["success"] is True
    assert len(ludo) == 1
    assert ludo[0]["url"].endswith("/assets/3d-model/rig")


def test_it_rigs_as_a_humanoid_with_mixamo_names(ludo, work):
    """"realistic" is not a rig_type this API has -- the options are
    general, humanoid, game and two humanoid templates. Humanoid with
    mixamo names is what Unity's avatar mapper handles best."""
    ludo_rigging.rig_character(MODEL_URL, name="hero", folder=work)

    body = ludo[0]["body"]
    assert body["rig_type"] == "humanoid"
    assert body["joint_naming"] == "mixamo"
    assert body["model"] == MODEL_URL


def test_a_rig_type_the_api_does_not_have_costs_nothing(ludo, work):
    """The enum is checked before the request, so a wrong value is
    refused for free rather than paid for and then rejected."""
    answer = ludo_rigging.rig_character(MODEL_URL, name="hero", folder=work,
                                        rig_type="realistic")

    assert answer["success"] is False
    assert len(ludo) == 0, "it paid a credit to be told the enum was wrong"


def test_the_rigged_model_lands_on_disk(ludo, work):
    answer = ludo_rigging.rig_character(MODEL_URL, name="hero", folder=work)

    assert (work / "hero_rigged.glb").is_file()
    assert answer["rigged_path"] == str(work / "hero_rigged.glb")


# ======================================================
# 2. It reuses rigging
# ======================================================

def test_it_reuses_rigging(ludo, work):
    """The same model rigged the same way twice is one purchase --
    ludo_client keys every billable call by what it asks for."""
    ludo_rigging.rig_character(MODEL_URL, name="a", folder=work)
    again = ludo_rigging.rig_character(MODEL_URL, name="b", folder=work)

    assert len(ludo) == 1, "it paid to rig the same model twice"
    assert again["success"] is True
    assert again["cost"]["reused"] == 1


def test_a_reused_rig_says_so(ludo, work):
    ludo_rigging.rig_character(MODEL_URL, name="a", folder=work)
    again = ludo_rigging.rig_character(MODEL_URL, name="b", folder=work)

    assert any("no credit was spent" in w for w in again["warnings"])


def test_a_different_joint_naming_is_a_different_purchase(ludo, work):
    ludo_rigging.rig_character(MODEL_URL, name="a", folder=work)
    ludo_rigging.rig_character(MODEL_URL, name="b", folder=work,
                               joint_naming="rigify")

    assert len(ludo) == 2


# ======================================================
# 3. It respects the ceilings
# ======================================================

def test_it_respects_rigging_ceilings(ludo, work, monkeypatch):
    monkeypatch.setenv(spend.ENV_RUN_CALLS, "1")

    ludo_rigging.rig_character(MODEL_URL, name="a", folder=work,
                               run_id="job-1")
    refused = ludo_rigging.rig_character(
        "https://cdn.ludo.ai/a/other.glb", name="b", folder=work,
        run_id="job-1")

    assert refused["success"] is False
    assert refused["ran"] is False, "a ceiling refusal implied a spend"
    assert len(ludo) == 1


def test_the_run_id_reaches_the_paid_call(ludo, work, monkeypatch):
    """Without it the per-run ceiling has nothing to count against and
    only the daily one applies."""
    monkeypatch.setenv(spend.ENV_RUN_CALLS, "5")
    ludo_rigging.rig_character(MODEL_URL, name="a", folder=work,
                               run_id="job-7")

    assert spend.spent(run_id="job-7") == 1


def test_the_ledger_records_the_rig(ludo, work):
    ludo_rigging.rig_character(MODEL_URL, name="a", folder=work)

    assert [e["endpoint"] for e in spend.history()] == ["model_3d_rig"]


# ======================================================
# 4. It is idempotent
# ======================================================

def test_it_is_idempotent(ludo, work):
    """Reuse remembers within the ledger. This is the check that still
    works when the run is repeated a week later."""
    (work / "hero_rigged.glb").write_bytes(b"already rigged")

    answer = ludo_rigging.rig_character(MODEL_URL, name="hero", folder=work)

    assert answer["reused"] is True
    assert answer["ran"] is False
    assert len(ludo) == 0, "it re-rigged a model that was already rigged"


def test_idempotency_can_be_switched_off(ludo, work):
    (work / "hero_rigged.glb").write_bytes(b"already rigged")

    ludo_rigging.rig_character(MODEL_URL, name="hero", folder=work,
                               reuse=False)

    assert len(ludo) == 1


def test_an_empty_rigged_file_is_not_treated_as_done(ludo, work):
    """A zero-byte file is a download that died, not a rigged model."""
    (work / "hero_rigged.glb").write_bytes(b"")

    ludo_rigging.rig_character(MODEL_URL, name="hero", folder=work)

    assert len(ludo) == 1


# ======================================================
# 5-8. Through Blender and into Unity
# ======================================================

def test_it_imports_rigged_fbx(ludo, blender, unity, work):
    answer = unity_rigging.rig_to_unity(MODEL_URL, name="Hero",
                                        work_folder=work)

    assert answer["success"] is True
    assert "import_asset" in commands(unity)
    assert args_for(unity, "import_asset")["source"].endswith("_unity.fbx")


def test_it_creates_prefab_from_rigged_asset(ludo, blender, unity, work):
    """create_prefab takes a SCENE object. Handing it the asset path is
    the mistake the whole Unity ordering exists to avoid."""
    unity_rigging.rig_to_unity(MODEL_URL, name="Hero", work_folder=work)

    source = args_for(unity, "create_prefab")["source"]
    assert source == "GlobalObjectId_V1-2-abc-123-0"
    assert not source.endswith(".fbx")


def test_it_assigns_animator_component(ludo, blender, unity, work):
    answer = unity_rigging.rig_to_unity(MODEL_URL, name="Hero",
                                        work_folder=work)

    assert answer["animator"] is True
    assert args_for(unity, "add_component")["type"] == "Animator"


def test_it_does_not_create_an_animator_controller(ludo, blender, unity, work):
    """That belongs to the Animation Pipeline. A controller invented
    here is one this pipeline cannot maintain and the next has to
    undo."""
    unity_rigging.rig_to_unity(MODEL_URL, name="Hero", work_folder=work)

    assert not any("animator_controller" in c for c in commands(unity))
    assert not any("controller" in str(c["args"]).lower() for c in unity)


def test_it_places_rigged_character_in_scene(ludo, blender, unity, work):
    answer = unity_rigging.rig_to_unity(
        MODEL_URL, name="Hero", work_folder=work,
        scene_path="Assets/Scenes/Forest.unity")

    assert commands(unity) == ["import_asset", "instantiate_prefab",
                               "create_prefab", "set_transform",
                               "add_component"]
    assert args_for(unity, "instantiate_prefab")["scene_path"] == \
        "Assets/Scenes/Forest.unity"
    assert args_for(unity, "set_transform")["position"] == "[0,0,0]"


def test_the_instance_is_a_global_id(ludo, blender, unity, work):
    """Stable across a scene reload, unlike a hierarchy path."""
    answer = unity_rigging.rig_to_unity(MODEL_URL, name="Hero",
                                        work_folder=work)

    assert answer["instance"].startswith("GlobalObjectId")


def test_the_answer_has_every_field_the_spec_asks_for(
        ludo, blender, unity, work):
    answer = unity_rigging.rig_to_unity(MODEL_URL, name="Hero",
                                        work_folder=work)

    for field in ("asset_path", "rigged_path", "prefab_path", "instance",
                  "animator", "reused", "steps", "warnings"):
        assert field in answer, field
    assert answer["steps"][0].startswith("ludo:")
    assert "blender:clean_rigged_model" in answer["steps"]


def test_the_cleanup_runs_after_the_rig_not_before(ludo, blender, unity, work):
    """A rig applied after the height is normalised would be built
    around the wrong scale."""
    answer = unity_rigging.rig_to_unity(MODEL_URL, name="Hero",
                                        work_folder=work)

    order = answer["steps"]
    assert order.index("ludo:model_3d_rig") < \
        order.index("blender:clean_rigged_model")
    assert order.index("blender:clean_rigged_model") < \
        order.index("unity:import_asset")


# ======================================================
# 9. Failure surfaces
# ======================================================

def test_failure_surface_rigging(ludo, blender, unity, work, monkeypatch):
    def refuse(url, json=None, headers=None, timeout=None):
        return FakeResponse(500, {"message": "rigger offline"})

    monkeypatch.setattr(ludo_client.requests, "post", refuse)
    answer = unity_rigging.rig_to_unity(MODEL_URL, name="Hero",
                                        work_folder=work)

    assert answer["success"] is False
    assert "Rigging failed" in answer["error"]
    assert "rigger offline" in answer["error"]
    assert commands(unity) == [], "it went on to Unity after the rig failed"


def test_a_rig_refused_by_a_ceiling_reports_nothing_spent(
        ludo, blender, unity, work, monkeypatch):
    monkeypatch.setenv(spend.ENV_DAILY_CALLS, "0")

    answer = unity_rigging.rig_to_unity(MODEL_URL, name="Hero",
                                        work_folder=work)

    assert answer["success"] is False
    assert answer["ran"] is False
    assert len(ludo) == 0


def test_a_failed_cleanup_stops_before_unity(ludo, blender, unity, work,
                                             monkeypatch):
    from backend.core import cli_runner
    monkeypatch.setattr(cli_runner, "run", lambda *a, **k: {
        "success": False, "error": "Blender crashed", "output": ""})

    answer = unity_rigging.rig_to_unity(MODEL_URL, name="Hero",
                                        work_folder=work)

    assert answer["success"] is False
    assert "Cleanup failed" in answer["error"]
    assert commands(unity) == []


def test_a_lost_skeleton_stops_before_unity(ludo, blender, unity, work):
    """Unity will happily import and prefab a mesh with no usable rig.
    Everything downstream then succeeds and the failure surfaces when
    somebody tries to animate it, which is the worst available time."""
    blender["rig"] = dict(blender["rig"], bones=0, weighted_vertices=0)

    answer = unity_rigging.rig_to_unity(MODEL_URL, name="Hero",
                                        work_folder=work)

    assert answer["success"] is False
    assert "no usable skeleton" in answer["error"]
    assert commands(unity) == []


def test_weights_lost_but_bones_kept_is_still_a_failure(
        ludo, blender, unity, work):
    """A rig can survive with every bone present and nothing weighted
    to any of them. It looks right and does nothing."""
    blender["rig"] = dict(blender["rig"], weighted_vertices=0)

    answer = unity_rigging.rig_to_unity(MODEL_URL, name="Hero",
                                        work_folder=work)

    assert answer["success"] is False
    assert commands(unity) == []


def test_a_failed_unity_step_still_reports_the_rigged_file(
        ludo, blender, unity, work, monkeypatch):
    """The rig was paid for. An answer that loses the path to it makes
    somebody buy it again."""
    from backend.unity import unity_cli_engine
    monkeypatch.setattr(unity_cli_engine, "run_invocation",
                        lambda *a, **k: {"success": False, "output": "",
                                         "json": None,
                                         "error": "No Pipeline instance found"})

    answer = unity_rigging.rig_to_unity(MODEL_URL, name="Hero",
                                        work_folder=work)

    assert answer["success"] is False
    assert answer["rigged_path"].endswith("_rigged.glb")


# ======================================================
# The cleanup, which is where the measurement went
# ======================================================

def test_the_rigged_cleanup_applies_transforms(blender, work):
    """The static stage leaves the armature root at 0.9. Unity reads
    that when it builds an avatar."""
    path = work / "hero_rigged.glb"
    path.write_bytes(b"glTF")

    blender_rigging.clean_rigged_model(str(path))

    assert "apply_transforms" in blender["source"]


def test_transforms_are_applied_after_the_moves_not_before(blender, work):
    """Applying a transform that is about to change again does
    nothing."""
    path = work / "hero_rigged.glb"
    path.write_bytes(b"glTF")

    blender_rigging.clean_rigged_model(str(path))

    source = blender["source"]
    assert source.index("scale_to_height") < source.index("apply_transforms")
    assert source.index("origin_to_floor") < source.index("apply_transforms")


def test_the_rig_is_measured_before_and_after(blender, work):
    path = work / "hero_rigged.glb"
    path.write_bytes(b"glTF")

    answer = blender_rigging.clean_rigged_model(str(path))

    assert answer["rig_before"]["bones"] == 22
    assert answer["rig_after"]["bones"] == 22


def test_merging_stays_off_for_a_skinned_mesh(blender, work):
    """Welding two vertices with different weights has to discard one
    of them."""
    path = work / "hero_rigged.glb"
    path.write_bytes(b"glTF")

    blender_rigging.clean_rigged_model(str(path))

    assert "merge_by_distance" not in blender["source"]


def test_a_scaled_armature_root_is_a_warning():
    warnings = blender_rigging.rig_warnings(
        {"armatures": 1, "bones": 22, "weighted_vertices": 100,
         "bound_meshes": ["Body"], "root_scales": [0.9]}, {})

    assert any("scale 0.9" in w and "retargeted" in w for w in warnings)


def test_a_rig_with_no_weights_is_a_warning():
    warnings = blender_rigging.rig_warnings(
        {"armatures": 1, "bones": 22, "weighted_vertices": 0,
         "bound_meshes": [], "root_scales": [1.0]}, {})

    assert any("no vertex is weighted" in w for w in warnings)


def test_losing_bones_in_the_cleanup_is_a_warning():
    warnings = blender_rigging.rig_warnings(
        {"armatures": 1, "bones": 18, "weighted_vertices": 100,
         "bound_meshes": ["Body"], "root_scales": [1.0]},
        {"bones": 22, "weighted_vertices": 100})

    assert any("4 bones were lost" in w for w in warnings)


def test_a_clean_rig_warns_about_nothing():
    good = {"armatures": 1, "bones": 22, "weighted_vertices": 4000,
            "bound_meshes": ["Body"], "root_scales": [1.0]}

    assert blender_rigging.rig_warnings(good, good) == []


def test_measure_rig_changes_nothing():
    """It runs twice in every cleanup, so it has to be safe to run at
    any point."""
    source = templates.TEMPLATES["measure_rig"]({})

    for mutating in ("bpy.ops.object.delete", "remove_doubles", ".location =",
                     "transform_apply", "bmesh.ops."):
        assert mutating not in source


def test_measure_rig_counts_weights_not_just_bones():
    source = templates.TEMPLATES["measure_rig"]({})

    assert "weighted_vertices" in source
    assert "bound_meshes" in source


# ======================================================
# What a real Ludo rig turned out to carry
#
# One rig was bought and inspected. Everything below is a fact from
# that model, not a guess about it:
#
#   45 bones, all 18,442 vertices weighted, modifier bound, root at 1.0
#   19 bones named mixamorig:Hips / Spine / Spine2 / Neck / Head /
#      Left+RightShoulder, Arm, ForeArm, Hand, UpLeg, Leg, Foot
#      -- Unity's complete required humanoid set
#   26 bones named bone_N, which are fingers and toes Unity leaves
#      unmapped, and which are NOT a sign the naming failed
#   1 Icosphere: 42 vertices, no weights, no parent, 46 users
# ======================================================

def test_a_bone_display_shape_is_not_shipped(blender, work):
    """Blender gives bones a custom display shape and the glTF importer
    materialises it as a real object -- 42 vertices, no weights, no
    parent, one user per bone. Harmless in Blender, and real geometry
    the moment it reaches FBX, where Unity imports it as a sphere
    floating in the prefab."""
    path = work / "hero_rigged.glb"
    path.write_bytes(b"glTF")

    blender_rigging.clean_rigged_model(str(path))

    assert "remove_stray_meshes" in blender["source"]


def test_strays_go_before_the_measurement(blender, work):
    """Otherwise the counts describe a model that is not the one
    exported."""
    path = work / "hero_rigged.glb"
    path.write_bytes(b"glTF")

    blender_rigging.clean_rigged_model(str(path))

    source = blender["source"]
    assert source.index("remove_stray_meshes") < source.index("measure_rig")


def test_stray_removal_needs_a_rig_and_a_bound_mesh():
    """The rule is "this file is a rigged character and this mesh is
    not part of it" -- not "delete anything unparented", which would
    take a separate prop somebody meant to keep."""
    source = templates.TEMPLATES["remove_stray_meshes"]({})

    assert "if _rigs and _bound:" in source
    assert "_m.parent is not None" in source
    assert "len(_m.vertex_groups)" in source


def test_what_was_removed_is_named():
    """A cleanup that silently deletes geometry is one nobody can trust
    with the geometry they care about."""
    source = templates.TEMPLATES["remove_stray_meshes"]({})

    assert "removed=_dropped" in source


def test_a_bound_mesh_is_never_a_stray():
    source = templates.TEMPLATES["remove_stray_meshes"]({})

    assert "if _m in _bound:" in source
    assert "continue" in source
