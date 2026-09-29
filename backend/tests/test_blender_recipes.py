"""The recipe library, and the translation that makes it buildable.

The library sat on disk unread for as long as it existed, so these
start from the assumption that nothing about it is known to work.
"""

import json

import pytest

from backend.blender import blender_nl_mapping as mapping
from backend.blender import blender_recipes as recipes
from backend.blender import blender_script_templates as templates


# ======================================================
# The library itself
# ======================================================

def test_the_library_is_there():
    assert recipes.RECIPE_ROOT.is_dir(), f"no library at {recipes.RECIPE_ROOT}"
    assert len(recipes.names()) >= 25


def test_visual_studio_leavings_are_not_recipes():
    """There is a .vs folder in the library with JSON in it."""
    assert not any(".vs" in str(path) for path in recipes._files())


@pytest.mark.parametrize("name", recipes.names())
def test_every_recipe_becomes_valid_blender_python(name):
    """The whole point. A recipe that cannot be built is a file, not a recipe."""
    source = templates.build_script(recipes.actions(name))

    compile(source, f"<{name}>", "exec")


# The three ways a recipe can put geometry in the scene. Primitives
# were the only one when the library was blockouts; a garment cut out
# of a base adds nothing at all -- it appends a body and copies it --
# so a test that only counted add_* called the new clothing empty.
MAKES_GEOMETRY = ("append_from_blend", "duplicate_object")


@pytest.mark.parametrize("name", recipes.names())
def test_every_recipe_makes_something(name):
    built = [step for step in recipes.actions(name)
             if step["action"].startswith("add_")
             or step["action"] in MAKES_GEOMETRY]

    assert built, f"{name} builds no objects"


# ======================================================
# The translation
# ======================================================

def test_a_box_is_a_scaled_cube():
    """Blender's primitive is a cube; a recipe's `size` is three numbers.

    Getting this wrong builds a model entirely out of cubes of the
    wrong shape, which reads as a modelling mistake rather than a
    translation one. Measured in Blender 5.0.1: the torso comes out
    0.34 x 0.22 x 0.46, exactly as base_humanoid asks.
    """
    steps = recipes.actions("base_humanoid")
    cube = next(s for s in steps
                if s["action"] == "add_cube" and s["params"]["name"] == "Humanoid_Torso")
    scale = next(s for s in steps
                 if s["action"] == "scale" and s["params"]["object"] == "Humanoid_Torso")

    assert cube["params"]["size"] == 1.0
    assert [scale["params"]["x"], scale["params"]["y"], scale["params"]["z"]] == [0.34, 0.22, 0.46]


def test_parts_are_named_for_their_recipe():
    assert recipes.part_names("base_pickaxe") == [
        "Pickaxe_Haft", "Pickaxe_Head", "Pickaxe_PickRight", "Pickaxe_PickLeft"]


def test_part_names_match_what_the_build_creates():
    """A rig names the mesh it binds to before the build has run, so the
    two have to agree or it binds to nothing.

    Both ways a recipe makes a mesh count: a primitive it adds, and a
    garment it cuts out of the base by copying it. The cut clothing is
    exactly what auto_bind is handed, so leaving duplicates out here
    would check the half that was never in doubt."""
    for name in recipes.names():
        made = [step["params"]["name"] for step in recipes.actions(name)
                if step["action"].startswith("add_")
                or step["action"] in ("duplicate_object", "create_mesh")]
        assert made == recipes.part_names(name), name


def test_the_base_prefix_is_not_carried_into_object_names():
    """"base_" says where the file sits in the library, not what the
    thing is, and it would end up in the Unity hierarchy."""
    assert recipes.prefix_for("base_humanoid") == "Humanoid"
    assert not any(part.startswith("Base") for part in recipes.part_names("base_humanoid"))


def test_transforms_are_applied_before_origins_move():
    """Shifting an origin on an object whose scale has not been baked
    leaves the two disagreeing."""
    steps = [s["action"] for s in recipes.actions("base_humanoid")]
    assert steps.index("apply_transforms") < steps.index("origin_to_geometry")


def test_clearing_is_optional_so_two_recipes_can_share_a_scene():
    """The miner needs his pickaxe built beside him, not instead of him."""
    together = recipes.build_many(["base_humanoid", "base_pickaxe"])

    assert together.count({"action": "clear_scene"}) == 1
    assert together[0] == {"action": "clear_scene"}
    names = [s["params"]["name"] for s in together if s["action"].startswith("add_")]
    assert "Humanoid_Torso" in names and "Pickaxe_Haft" in names


def test_an_unknown_recipe_says_what_there_is():
    with pytest.raises(recipes.UnknownRecipe) as raised:
        recipes.load("base_dragon")

    assert "base_humanoid" in str(raised.value)


def test_an_unsupported_primitive_is_refused_not_skipped(tmp_path, monkeypatch):
    """A part that quietly did not get built is found by noticing the
    model has no left arm."""
    shelf = tmp_path / "blender" / "oddments"
    shelf.mkdir(parents=True)
    (shelf / "recipe_odd.json").write_text(json.dumps({
        "name": "odd", "version": "1.0", "description": "", "units": "meters",
        "objects": [{"id": "blob", "type": "metaball", "location": [0, 0, 0]}],
        "cleanup": {}, "export": {},
    }), encoding="utf-8")
    monkeypatch.setattr(recipes, "RECIPE_ROOT", tmp_path / "blender")

    with pytest.raises(recipes.UnsupportedRecipe):
        recipes.actions("odd")


# ======================================================
# Reaching it from a sentence
# ======================================================

def test_the_library_adds_words_nothing_could_build_before():
    added = mapping.library_recipes()

    assert "helmet" in added and "pickaxe" in added and "quadruped" in added
    assert added["helmet"] == "base_helmet"


def test_the_library_never_takes_a_word_the_builders_already_answer():
    """"character" keeps building the Python one. Swapping it for
    base_humanoid would change a model nobody asked to change."""
    assert not (set(mapping.library_recipes()) & set(mapping.RECIPES))


def test_a_sentence_reaches_the_library():
    got = mapping.map_text("in blender build me a helmet")

    assert got is not None
    assert "recipe:helmet" in got["matched"]
    # However the helmet is made -- added as a primitive, or cut out of
    # a base by copying it -- something in the scene has to be called
    # after it, or the sentence reached a recipe that built nothing.
    assert any(str(s["params"].get("name", "")).startswith("Helmet")
               for s in got["actions"]
               if s["action"].startswith("add_")
               or s["action"] == "duplicate_object")


def test_a_sentence_still_reaches_the_hardcoded_builders():
    got = mapping.map_text("in blender create a character")

    assert got is not None
    assert "recipe:character" in got["matched"]


# ======================================================
# The base model library
# ======================================================

def test_the_base_models_are_findable():
    """A recipe says how to add detail; a base is what detail is added
    to. Both halves have to be reachable from code or neither is."""
    bases = recipes.base_models()

    assert bases, "no base models -- is aria_models/ there?"
    assert any(b["object"] == "GEO-body_male_stylized" for b in bases)


def test_every_base_says_whether_it_has_uvs():
    """The first character experiment died on a sphere with a
    smart-unwrap and a cut-out painting: no correspondence, black
    render. A base without usable UVs is not a base."""
    for base in recipes.base_models():
        assert base["uv"] is True, base["object"]


def test_every_base_carries_its_licence():
    """These ship in a commercial game. A base whose licence is not
    recorded beside it is one nobody can answer for later."""
    for base in recipes.base_models():
        assert base["licence"], base["object"]


def test_a_base_reports_whether_this_clone_actually_has_the_file():
    """The .blend is fetched, not committed, so it can legitimately be
    missing -- and a caller has to be able to tell."""
    for base in recipes.base_models():
        assert isinstance(base["available"], bool)
        assert base["file"].endswith(".blend")


def test_bases_can_be_narrowed_by_use():
    assert [b["object"] for b in recipes.base_models("stylised male")] == ["GEO-body_male_stylized"]


# ======================================================
# Landmarks
# ======================================================
#
# A garment is cut with a box in world metres, and those metres used to
# be written into the recipe -- which made every clothing recipe true
# of one body at one height. These cover the translation that lets a
# recipe say "the waist" instead.

MALE = "GEO-body_male_stylized"


def test_the_landmark_table_was_measured():
    """Without it, every clothing recipe in the library is unbuildable."""
    marks = recipes.landmarks(MALE)

    assert marks, "no landmarks -- run aria_models/measure_base_landmarks.py"
    assert marks["z"]["crown"] == pytest.approx(1.8, abs=0.01)
    assert marks["faces"] == "-Y"


@pytest.mark.parametrize("base", [
    "GEO-body_male_stylized", "GEO-body_female_stylized",
    "GEO-body_male_realistic", "GEO-body_female_realistic"])
def test_the_landmarks_are_in_the_order_a_body_is(base):
    """A table that says the knee is above the chest is a table that
    builds trousers onto a torso, silently. This is the check that
    caught the derivation reading a crotch inside a skull."""
    heights = recipes.landmarks(base)["z"]
    order = ["sole", "ankle", "calf", "knee", "crotch", "waist",
             "chest", "armpit", "neck", "chin", "head", "crown"]
    got = [heights[key] for key in order]

    assert got == sorted(got), dict(zip(order, got))


def test_a_number_is_still_metres():
    assert recipes.resolve(1.07, recipes.landmarks(MALE)) == 1.07
    assert recipes.resolve(None, recipes.landmarks(MALE)) is None


def test_a_name_is_the_measurement():
    marks = recipes.landmarks(MALE)

    assert recipes.resolve("waist", marks) == marks["z"]["waist"]


def test_a_name_can_carry_an_offset():
    marks = recipes.landmarks(MALE)
    waist = marks["z"]["waist"]

    assert recipes.resolve("waist+0.04", marks) == pytest.approx(waist + 0.04)
    assert recipes.resolve("waist-0.04", marks) == pytest.approx(waist - 0.04)


def test_a_leading_minus_mirrors_before_it_offsets():
    """A recipe reads the sign as "which side", so the offset after it
    has to keep meaning "inward" rather than flipping with it."""
    marks = recipes.landmarks(MALE)
    reach = marks["half_width"]["shoulder"]

    assert recipes.resolve("-shoulder_x", marks) == pytest.approx(-reach)
    assert recipes.resolve("-shoulder_x+0.02", marks) == pytest.approx(-reach + 0.02)


def test_heights_and_widths_do_not_collide():
    """Nearly every landmark exists on two axes. One flat namespace
    would have answered "chest" with whichever table was read first,
    and a chest HEIGHT used as a chest width is a garment 1.2m across."""
    marks = recipes.landmarks(MALE)

    assert recipes.resolve("chest", marks) == marks["z"]["chest"]
    assert recipes.resolve("chest_x", marks) == marks["half_width"]["chest"]
    assert recipes.resolve("chest_front_y", marks) == marks["y"]["chest_front"]
    assert recipes.resolve("chest", marks) != recipes.resolve("chest_x", marks)


def test_an_unknown_landmark_is_refused_and_lists_the_real_ones():
    """A bound that quietly became "no bound at all" is a vest up to
    the eyebrows, and the recipe looks correct while it does it."""
    with pytest.raises(recipes.UnsupportedRecipe) as raised:
        recipes.resolve("shoulderblade", recipes.landmarks(MALE), "vest z_max")

    assert "shoulderblade" in str(raised.value)
    assert "vest z_max" in str(raised.value)
    assert "waist" in str(raised.value)


def test_a_landmark_with_no_table_says_how_to_get_one():
    with pytest.raises(recipes.UnsupportedRecipe) as raised:
        recipes.resolve("waist", {}, "vest z_max")

    assert "measure_base_landmarks" in str(raised.value)


def test_a_recipe_is_cut_where_the_body_actually_is():
    """The end of it: base_pants says "ankle+0.04", and what reaches
    Blender has to be the measured ankle rather than a typed number."""
    marks = recipes.landmarks(MALE)
    region = next(step for step in recipes.actions("base_pants")
                  if step["action"] == "vertex_group_by_region")

    assert region["params"]["z_min"] == pytest.approx(marks["z"]["ankle"] + 0.04)
    assert region["params"]["z_max"] == pytest.approx(marks["z"]["waist"] + 0.02)


# ======================================================
# Cutting a garment out of a body
# ======================================================

def test_several_regions_become_one_group():
    """Gloves are the case that forced it: two hands at the same
    height with the hips between them, so no single box reaches both.
    The same group name each time, and a vertex group is a union."""
    cuts = [step for step in recipes.actions("base_gloves")
            if step["action"] == "vertex_group_by_region"]

    assert len(cuts) == 2
    assert len({step["params"]["name"] for step in cuts}) == 1
    assert cuts[0]["params"]["x_min"] > 0
    assert cuts[1]["params"]["x_max"] < 0


def test_smoothing_runs_before_the_garment_is_pushed_off_the_body():
    """Smoothing shrinks as it works, so after the inflate it would
    pull the garment back into the body it was just lifted off."""
    order = [step["action"] for step in recipes.actions("base_pants")]

    assert order.index("relax_surface") < order.index("inflate")
    assert order.index("inflate") < order.index("apply_solidify")


def test_smoothing_is_held_back_at_the_hem():
    """An unweighted smooth drags an open boundary into a frill. The
    group the cut was made with already fades out at its own edges, so
    handing it back is what keeps the waistband straight."""
    steps = recipes.actions("base_pants")
    relax = next(s for s in steps if s["action"] == "relax_surface")
    cut = next(s for s in steps if s["action"] == "vertex_group_by_region")

    assert relax["params"]["group"] == cut["params"]["name"]


def test_a_cast_shapes_the_garment_after_it_is_lifted_clear():
    """base_helmet is the one that uses it -- a skullcap nudged toward
    a sphere. Before the inflate it would be casting the body."""
    order = [step["action"] for step in recipes.actions("base_helmet")]

    assert order.index("inflate") < order.index("apply_cast")
    assert order.index("apply_cast") < order.index("apply_solidify")


def test_scaffolding_is_dropped_before_the_export():
    """A garment recipe has to stand a body up to cut the garment out
    of, and export_fbx writes every mesh in the file. Without the drop,
    base_pants exports a naked man wearing them."""
    steps = recipes.actions("base_pants", export=True)
    order = [step["action"] for step in steps]
    dropped = next(s for s in steps if s["action"] == "delete_object")

    assert dropped["params"]["object"] == "Pants_Body"
    assert order.index("apply_mask") < order.index("delete_object")
    assert order.index("delete_object") < order.index("export_fbx")


def test_a_body_recipe_keeps_its_body():
    """base_stylized_body IS the body. Dropping it would export air."""
    assert not any(step["action"] == "delete_object"
                   for step in recipes.actions("base_stylized_body"))


def test_a_part_does_not_stutter_its_own_recipe_name():
    """base_pants calling its trousers "Pants_Pants" carries the
    stutter into the FBX, the prefab and the Unity hierarchy."""
    assert recipes.part_names("base_pants") == ["Pants"]
    assert recipes.part_names("base_underwear_female") == [
        "UnderwearFemale_Top", "UnderwearFemale_Briefs"]


def test_a_rig_can_be_placed_off_the_body_it_is_for():
    """A rig written in metres belongs to one body at one height."""
    marks = recipes.landmarks(MALE)
    bone = next(step for step in recipes.rig_actions("base_stylized_body")
                if step["action"] == "add_bone"
                and step["params"]["name"] == "Head")

    assert bone["params"]["head"][2] == pytest.approx(marks["z"]["neck"])
    assert bone["params"]["tail"][2] == pytest.approx(marks["z"]["crown"])


# ======================================================
# Props
# ======================================================
#
# Props stay primitives -- a rock does not want a human base mesh --
# but primitives left untouched read as primitives, and for a long
# time smooth_shade was the only modifier any recipe in the library
# used. These cover the shaping that replaced that.

def test_a_rock_is_one_surface_and_not_four_spheres():
    """Overlapping primitives read as overlapping primitives however
    they are shaded: each keeps its own silhouette and the seams run
    through the middle of the shape. The weld makes one mesh and the
    remesh rebuilds it as one surface."""
    steps = recipes.actions("base_rock")
    order = [step["action"] for step in steps]
    weld = next(s for s in steps if s["action"] == "join_objects")

    assert weld["params"]["objects"][0] == "Rock_Core"
    assert len(weld["params"]["objects"]) == 4
    assert order.index("join_objects") < order.index("voxel_remesh")


def test_joining_happens_before_the_modifiers_that_shape_the_result():
    """A displace on four separate spheres is four bumpy spheres. The
    same displace on one remeshed rock is a rock."""
    order = [step["action"] for step in recipes.actions("base_rock")]

    assert order.index("voxel_remesh") < order.index("stamp_detail")


def test_a_cast_runs_before_the_displace_it_would_otherwise_undo():
    """Cast then displace puts flat faces on a lumpy rock. Displace
    then cast smooths the lumps straight back off again."""
    order = [step["action"] for step in recipes.actions("base_rock")]

    assert order.index("apply_cast") < order.index("stamp_detail")


def test_a_join_with_no_parts_is_refused(tmp_path, monkeypatch):
    """Silently joining nothing leaves the parts scattered, and the
    only symptom is a prop that still looks like a pile of spheres."""
    shelf = tmp_path / "blender" / "oddments"
    shelf.mkdir(parents=True)
    (shelf / "recipe_lonely.json").write_text(json.dumps({
        "name": "lonely", "version": "1.0", "description": "",
        "units": "meters",
        "objects": [{"id": "core", "type": "sphere", "radius": 1.0,
                     "location": [0, 0, 0]}],
        "join": [{"into": "core", "parts": []}],
        "cleanup": {}, "export": {},
    }), encoding="utf-8")
    monkeypatch.setattr(recipes, "RECIPE_ROOT", tmp_path / "blender")

    with pytest.raises(recipes.UnsupportedRecipe) as raised:
        recipes.actions("lonely")

    assert "no parts" in str(raised.value)


def test_a_boolean_names_another_part_of_the_same_recipe():
    """A recipe writes its own ids; they carry the recipe's prefix once
    they are in the scene. Passing "door_cut" through untouched leaves
    the boolean aiming at an object that was never made, and it cuts
    nothing at all."""
    cuts = [step for step in recipes.actions("base_house")
            if step["action"] == "apply_boolean"]

    assert len(cuts) == 3
    assert {step["params"]["object"] for step in cuts} == {"House_Walls"}
    assert "House_DoorCut" in {step["params"]["target"] for step in cuts}


def test_a_boolean_is_applied_before_its_cutter_is_discarded():
    """A live boolean holds a pointer to its cutter. Delete the cutter
    and the modifier aims at nothing, which evaluates to no walls at
    all -- the render was a roof floating over a foundation."""
    steps = recipes.actions("base_house")
    order = [step["action"] for step in steps]
    cut = next(s for s in steps if s["action"] == "apply_boolean")

    assert cut["params"]["apply"] is True
    assert order.index("apply_boolean") < order.index("delete_object")


def test_scaffolding_is_discarded_by_name():
    """export_fbx writes every mesh in the file, so a cutter left in
    the scene ships as a solid slab of door in its own doorway."""
    dropped = [step["params"]["object"] for step in recipes.actions("base_house")
               if step["action"] == "delete_object"]

    assert dropped == ["House_DoorCut", "House_WindowLeftCut",
                       "House_WindowRightCut"]


def test_a_wheel_is_smoothed_with_an_angle_limit():
    """Plain shade_smooth smooths the rim where a cylinder's flat cap
    meets its barrel, so the cap blends into the side and a tyre
    renders as a ball. Every cylinder in the library had this."""
    wheels = [step for step in recipes.actions("base_car")
              if step["action"] == "smooth_shade"
              and "Wheel" in step["params"]["object"]]

    assert len(wheels) == 4
    assert all(step["params"].get("angle") for step in wheels)


def test_the_shaping_modifiers_are_all_reachable_from_a_recipe():
    """The library had the actions and the recipes could not say them,
    which is why every prop was a pile of untouched primitives."""
    for wanted in ("cast", "deform", "boolean", "stamp", "voxel_remesh"):
        assert wanted in recipes.MODIFIERS

    for action in recipes.MODIFIERS.values():
        assert action in templates.TEMPLATES, action


def test_a_creature_limb_is_attached_to_the_body_it_belongs_to():
    """base_humanoid rotated its arms 90 degrees about Z to lay them
    out, and rotating a Z-aligned cylinder about Z does nothing -- so
    they hung 12cm off its shoulders. Same bug in both creatures."""
    marks = {}
    for step in recipes.actions("base_humanoid"):
        if step["action"] in ("add_cylinder", "add_cube"):
            marks[step["params"]["name"]] = step["params"]["location"]

    torso_half = 0.34 / 2.0
    arm = marks["Humanoid_ArmLeft"]

    assert abs(arm[0]) < torso_half + 0.07, "the arm floats off the shoulder"


def test_an_anime_body_is_reshaped_after_it_arrives_and_rigged_by_measurement():
    """The reshape runs on the base as it arrives; the rig measures the result.

    Fixed landmark bones would sit where the unreshaped body was, so the
    anime bodies are rigged by auto_rig, after find_landmarks.
    """
    for name in ("anime_female_body", "anime_male_body"):
        order = [step["action"] for step in recipes.actions(name, rig=True)]
        assert order.index("origin_to_floor") < order.index("reshape_body")
        assert order.index("reshape_body") < order.index("find_landmarks") < order.index("auto_rig")
        assert "create_armature" not in order


def test_proportions_resolve_landmark_names_to_heights():
    steps = recipes.proportions_actions("anime_female_body")
    reshape = steps[0]["params"]
    marks = recipes._flatten(recipes.landmarks("GEO-body_female_stylized"))
    legs = next(p for p in reshape["parts"] if p["name"] == "leg R")
    assert legs["z_max"] == marks["crotch"]
    assert [marks["crotch"], 0.82] in reshape["heights"]
    assert all(isinstance(old, float) or old == 0 for old, _ in reshape["heights"])


def test_reshape_body_refuses_a_pivot_it_does_not_know():
    with pytest.raises(templates.BadValue):
        templates.reshape_body({"object": "Body", "parts": [{"pivot": "__import__('os')"}]})
    with pytest.raises(templates.BadValue):
        templates.reshape_body({"object": "Body", "heights": [[0, 0]]})


def test_a_garment_cut_carves_after_the_cloth_is_thickened_and_leaves_no_cutter():
    steps = recipes.garment_actions("anime_tshirt")
    order = [step["action"] for step in steps]
    boolean = order.index("apply_boolean")
    assert order.index("apply_solidify") < boolean < order.index("smooth_shade")
    cutter = steps[boolean]["params"]["target"]
    assert {"action": "delete_object", "params": {"object": cutter}} in steps


def test_a_garment_can_be_cut_from_a_recipe_body_named_for_itself():
    """build_many puts the body and its clothes in one scene: the garment's
    copy of the body must not share the body's name, and is measured alone."""
    steps = recipes.base_actions("anime_tshirt")
    names = {s["params"].get("name") for s in steps if s["action"] == "append_from_blend"}
    assert names == {"Tee_Body"}
    for step in steps:
        if step["action"] in ("scale_to_height", "origin_to_floor"):
            assert step["params"]["object"] == "Tee_Body"


def test_an_outfit_puts_every_piece_on_the_bodys_one_skeleton():
    """Exchangeable clothes: one rig, every surviving mesh weighted to it."""
    steps = recipes.outfit_actions("anime_male_body",
                                   ["anime_tshirt", "anime_long_coat"], clips=["walk"])
    order = [step["action"] for step in steps]
    assert order.index("auto_rig") < order.index("transfer_weights")
    moved = [s["params"] for s in steps if s["action"] == "transfer_weights"]
    assert {p["source"] for p in moved} == {"AnimeMale"}
    assert {p["armature"] for p in moved} == {"AnimeMale_Rig"}
    targets = {p["target"] for p in moved}
    assert {"AnimeTshirt_Tshirt", "AnimeLongCoat_Coat", "AnimeLongCoat_Tails"} <= targets
    assert not any("_Cut" in target for target in targets)     # cutters are gone
    assert steps[-1] == {"action": "add_clip",
                         "params": {"armature": "AnimeMale_Rig", "clip": "walk"}}
