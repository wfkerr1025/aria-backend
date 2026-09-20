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


@pytest.mark.parametrize("name", recipes.names())
def test_every_recipe_makes_something(name):
    built = [step for step in recipes.actions(name)
             if step["action"].startswith("add_")]

    assert built, f"{name} builds no objects"


# ======================================================
# The translation
# ======================================================

def test_a_box_is_a_scaled_cube():
    """Blender's primitive is a cube; a recipe's `size` is three numbers.

    Getting this wrong builds a model entirely out of cubes of the
    wrong shape, which reads as a modelling mistake rather than a
    translation one. Measured in Blender 5.0.1: the torso comes out
    0.32 x 0.20 x 0.45, exactly as base_humanoid asks.
    """
    steps = recipes.actions("base_humanoid")
    cube = next(s for s in steps
                if s["action"] == "add_cube" and s["params"]["name"] == "Humanoid_Torso")
    scale = next(s for s in steps
                 if s["action"] == "scale" and s["params"]["object"] == "Humanoid_Torso")

    assert cube["params"]["size"] == 1.0
    assert [scale["params"]["x"], scale["params"]["y"], scale["params"]["z"]] == [0.32, 0.20, 0.45]


def test_parts_are_named_for_their_recipe():
    assert recipes.part_names("base_pickaxe") == [
        "Pickaxe_Handle", "Pickaxe_HeadCenter", "Pickaxe_PickLeft", "Pickaxe_PickRight"]


def test_part_names_match_what_the_build_creates():
    """A rig names the mesh it binds to before the build has run, so the
    two have to agree or it binds to nothing."""
    for name in recipes.names():
        made = [s["params"]["name"] for s in recipes.actions(name)
                if s["action"].startswith("add_")]
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
    assert "Humanoid_Torso" in names and "Pickaxe_Handle" in names


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
    assert any(s["params"].get("name", "").startswith("Helmet_")
               for s in got["actions"] if s["action"].startswith("add_"))


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
