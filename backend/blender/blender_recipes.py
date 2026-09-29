"""ARIA Lite - the recipe library, turned into actions.

`aria_recipes/blender` holds twenty-nine hand-written recipes -- bipeds,
quadrupeds, houses, clothing, a pickaxe, a sword -- each a small JSON
description of a blockout: some primitives at some places, a few
modifiers, a cleanup pass and an export.

Nothing read them. The recipes ARIA actually built from were a separate
and much smaller set written as Python functions in
`blender_nl_mapping.py` -- car, character, tree, house, table, chair,
sword, barrel, rock -- so the library sat on disk, complete and unused.
This is the piece that was missing.

WHY A LOADER RATHER THAN MORE PYTHON FUNCTIONS
----------------------------------------------
Because the library is data and an action list is data, and the gap
between them is a translation, not a program. A recipe says WHAT stands
where; `blender_script_templates` knows HOW to say that to Blender.
Adding a thirtieth recipe should be a file, not a function -- which is
the whole reason the library was written as JSON.

WHAT THE RECIPES ACTUALLY CONTAIN
---------------------------------
Originally, blockouts: primitives at places, a smooth_shade or two, a
cleanup and an export. Every clothing recipe was a cube for the body
and two cylinders for the limbs, which is a fine way to say where a
jacket goes and no way at all to make one.

The clothing is now CUT OUT OF THE BODY THAT WEARS IT -- copy the
base, keep the box the garment covers, throw the rest away, smooth the
anatomy out of what is left, push it clear and thicken it. A trouser
leg is then already a leg, and a hem exists wherever the cut ended.
See `garment_actions` for why that replaced wrapping blanks onto a
figure, and `relax_surface` for why the smoothing is not optional.

So a recipe may carry:

    base        a named mesh out of aria_models, scaled and stood up.
                `keep: false` drops it again once the garment is cut,
                because export_fbx writes every mesh in the file
    objects     primitives, as before -- still the right way to make a
                hat, which is not the shape of a head
    join        several primitives welded into one mesh and remeshed
                into a single surface. What makes a rock a rock rather
                than four spheres inside each other
    garments    boxes to cut out of the base. `regions` for a garment
                that covers two places a single box cannot reach
                between, `relax` to take the anatomy out, `cast` to
                push the result toward a sphere or a cylinder
    fit         shrinkwrap a blank onto something (the older way)
    modifiers   shaping -- see MODIFIERS. A taper makes a roof out of
                a box, a boolean cuts a doorway instead of gluing a
                slab over one, a stamp puts lumps on a rock
    discard     scaffolding to delete before the export, such as the
                cube a boolean used as a cutter
    armature    bones, bind, auto_bind, ik
    animations  keyed poses and the shape of the curves between them
    cleanup     apply_transforms, merge_by_distance, origin_to_geometry
    export      format, path, apply_scale

Anything outside that is refused rather than skipped. A recipe whose
parts quietly failed to appear gets found by noticing the model has no
left arm, which is a slow and irritating way to learn it.

Bounds and positions may be NAMED rather than measured -- "waist"
instead of 1.07 -- which is what stops a garment belonging to one body
at one height. See the landmark section below.

A NOTE ON CUBES
---------------
A recipe's cube carries `size: [x, y, z]` and `add_cube` takes one
number, because Blender's primitive is a cube and a recipe means a box.
So a cube is added at 1x1x1 and scaled, which is the same thing and
leaves the scale on the object for `apply_transforms` to bake down.
Getting this wrong builds a model entirely out of cubes of the wrong
shape, which reads as a modelling mistake rather than a units one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "LANDMARK_FILE",
    "MODEL_ROOT",
    "RECIPE_ROOT",
    "UnknownRecipe",
    "UnsupportedRecipe",
    "actions",
    "landmarks",
    "resolve",
    "animation_actions",
    "base_actions",
    "base_models",
    "fit_actions",
    "garment_actions",
    "join_actions",
    "animation_names",
    "build_many",
    "catalogue",
    "load",
    "names",
    "part_names",
    "prefix_for",
    "rig_actions",
]

# backend/blender/blender_recipes.py -> the repo root -> the libraries.
RECIPE_ROOT = Path(__file__).resolve().parents[2] / "aria_recipes" / "blender"

# The other half: meshes to add detail TO, rather than instructions for
# adding it. See aria_models/README.md for why these are authored and
# not generated.
MODEL_ROOT = Path(__file__).resolve().parents[2] / "aria_models"

# Where each base body's parts are, measured rather than estimated, by
# aria_models/measure_base_landmarks.py. See `resolve`.
LANDMARK_FILE = MODEL_ROOT / "landmarks_human_base_meshes.json"

# What a recipe may be made of, and the action that makes it. The
# shaping of each one's parameters is in _one_object.
PRIMITIVES = {
    "cube": "add_cube",
    "sphere": "add_sphere",
    "cylinder": "add_cylinder",
    "cone": "add_cone",
    "plane": "add_plane",
    "torus": "add_torus",
}

# Modifier names a recipe may use, and the action each becomes.
#
# The first five were the whole vocabulary, and smooth_shade was the
# only one any recipe actually used -- which is why every prop in the
# library was a pile of untouched primitives. A rock was four spheres
# and read as four spheres. The rest are shaping: a taper turns a box
# into a roof, a cast rounds a car, a displace puts lumps on a rock,
# and a boolean cuts a doorway instead of gluing a slab over one.
MODIFIERS = {
    "smooth_shade": "smooth_shade",
    "subdivision": "apply_subdivision",
    "bevel": "apply_bevel",
    "mirror": "apply_mirror",
    "solidify": "apply_solidify",
    "cast": "apply_cast",
    "deform": "apply_simple_deform",
    "relax": "relax_surface",
    "decimate": "apply_decimate",
    "array": "apply_array",
    "boolean": "apply_boolean",
    "stamp": "stamp_detail",
    "voxel_remesh": "voxel_remesh",
    "quad_remesh": "quad_remesh",
}

# Modifier settings that name another OBJECT rather than a number, and
# what the action calls them. A recipe writes its own ids, which carry
# the recipe's prefix once they are in the scene -- so "with": "door"
# has to reach Blender as "House_Door" or the boolean cuts nothing.
MODIFIER_OBJECTS = {"with": "target"}

# Cleanup steps, in the order they have to run. Transforms are baked
# before the origins move: shifting an origin on an object whose scale
# has not been applied leaves the two disagreeing.
CLEANUP_ORDER = ("apply_transforms", "merge_by_distance", "origin_to_geometry")

EXPORT_ACTIONS = {
    "fbx": "export_fbx",
    "glb": "export_glb",
    "gltf": "export_glb",
    "obj": "export_obj",
}


class UnknownRecipe(KeyError):
    """No recipe by that name. The message lists the ones there are."""


class UnsupportedRecipe(ValueError):
    """A recipe asking for something this translation does not cover."""


def _pascal(text: str) -> str:
    """"arm_left" becomes "ArmLeft", so a hierarchy reads."""
    return "".join(part[:1].upper() + part[1:] for part in str(text).split("_") if part)


def _name(head: str, part: Any, fallback: str = "Part") -> str:
    """What one part of a recipe is called once it is in the scene.

    The recipe's prefix, then the part's own id -- except when those
    are the same word. A clothing recipe is usually ONE garment with
    the same name as the recipe it is in, and `base_pants` naming its
    trousers "Pants_Pants" carries that stutter into the FBX, the
    prefab and the Unity hierarchy, where somebody has to look at it.
    """
    tail = _pascal(part or fallback)
    return tail if tail == head else f"{head}_{tail}"


# ======================================================
# Landmarks: saying "the waist" instead of saying 1.07
# ======================================================
#
# A garment is cut with a box in world metres, so a recipe used to have
# to carry the numbers -- z 0.97 to 1.47 for a vest. Those numbers are
# true of ONE body at ONE height. Put the same recipe on the female
# base and the vest is a collar; scale the miner to 1.6m and the
# trousers start at his ribs.
#
# So a bound may instead be the NAME of a landmark on whatever base the
# recipe is built on:
#
#     "z_min": "waist"            the waist, wherever this body's is
#     "z_max": "chest+0.04"       four centimetres above the chest
#     "x_max": "hip_x"            the right edge of the hips
#     "x_min": "-hip_x"           and the left edge
#     "x_min": "-shoulder_x+0.02" negate first, then offset
#
# A bare name is a height; _x is a half-width and _y a depth. See
# _flatten for why the suffixes have to be there.
#
# Plain numbers still mean metres, so nothing that already worked
# stops working.

_SIGNS = {"+": 1.0, "-": -1.0}


def landmarks(base_object: str) -> Dict[str, Any]:
    """Every measured landmark of one base mesh, by its object name."""
    if not LANDMARK_FILE.is_file():
        return {}
    try:
        table = json.loads(LANDMARK_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        logger.warning("landmark table could not be read: %s", error)
        return {}
    return (table.get("bases") or {}).get(str(base_object)) or {}


def _flatten(marks: Dict[str, Any]) -> Dict[str, float]:
    """Heights, half-widths and depths in one namespace, by axis.

    A bare name is a HEIGHT -- "waist" is 1.07, the height of the waist
    -- because that is what almost every bound in a garment recipe is.
    Half-widths take _x and depths take _y:

        waist      1.07     how far up the waist is
        waist_x    0.1179   how far out from the middle it reaches
        face_y    -0.1154   how far forward the face is

    The suffixes are not decoration. Nearly every landmark exists on
    two axes -- waist, chest, hip, knee, neck, head and shoulder all
    have both a height and a half-width -- so one flat namespace would
    have silently answered "chest" with whichever table was read first.
    """
    found: Dict[str, float] = {}
    for section, suffix in (("z", ""), ("half_width", "_x"), ("y", "_y")):
        for name, value in (marks.get(section) or {}).items():
            if value is not None:
                found[f"{name}{suffix}"] = float(value)
    return found


def resolve(value: Any, marks: Dict[str, Any], where: str = "") -> Any:
    """One bound: a number as it is, a landmark name as its measurement.

    An unknown name is refused rather than dropped. A garment whose
    z_max quietly became "no upper bound" is a vest that reaches the
    character's eyebrows, and the recipe looks correct while it does it.
    """
    if value is None or isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return value

    text = value.strip()
    if not text:
        return None

    # A plain number written as a string is still a number.
    try:
        return float(text)
    except ValueError:
        pass

    sign = 1.0
    if text[:1] in _SIGNS:
        sign = _SIGNS[text[0]]
        text = text[1:].strip()

    offset = 0.0
    for mark in ("+", "-"):
        head, found, tail = text.partition(mark)
        if found and tail.strip():
            try:
                offset = _SIGNS[mark] * float(tail.strip())
            except ValueError:
                continue
            text = head.strip()
            break

    table = _flatten(marks)
    if not table:
        raise UnsupportedRecipe(
            f"{where or 'a bound'} names the landmark {value!r}, but no "
            f"landmarks are known for this recipe's base. Run "
            f"aria_models/measure_base_landmarks.py, or use metres.")
    if text not in table:
        raise UnsupportedRecipe(
            f"{where or 'a bound'} names {value!r}, and {text!r} is not a "
            f"landmark. This base has: {', '.join(sorted(table))}")

    return sign * table[text] + offset


def _marks_for(recipe: Dict[str, Any]) -> Dict[str, Any]:
    """The landmark table for whatever base this recipe stands on."""
    base = recipe.get("base") or {}
    if base.get("recipe"):
        return landmarks(str(base["recipe"]))
    return landmarks(base.get("object", "")) if base else {}


def _point(value: Any, marks: Dict[str, Any], where: str = "") -> Any:
    """An [x, y, z] whose components may each be a landmark."""
    if not isinstance(value, (list, tuple)):
        return value
    return [resolve(item, marks, where) for item in value]


def join_actions(name: str, prefix: Optional[str] = None) -> List[Dict[str, Any]]:
    """Several primitives into one solid thing.

    WHY A PROP NEEDS THIS. The library's rock was four spheres sitting
    inside each other, and it rendered as four spheres sitting inside
    each other -- because that is what it was. Overlapping primitives
    read as overlapping primitives however they are shaded: every
    sphere keeps its own silhouette, and the seams where they meet are
    creases running through the middle of the shape.

    Joining welds them into one mesh; a voxel remesh then rebuilds
    that mesh as a single surface wrapped round the whole union, which
    is the step that throws the seams away. What comes out has no
    spheres in it. A displace on top of THAT is a rock; a displace on
    four separate spheres is four bumpy spheres.

    The joined object keeps the name of `into`, so later modifiers and
    a rig can still find it. The absorbed parts are gone -- they are
    listed by `part_names` because the build really did create them,
    which is worth knowing when a rig cannot find one afterwards.
    """
    recipe = load(name)
    head = prefix or prefix_for(name)
    steps: List[Dict[str, Any]] = []

    for weld in recipe.get("join", []):
        into = _name(head, weld.get("into"))
        parts = [_name(head, part) for part in weld.get("parts", [])]
        if not parts:
            raise UnsupportedRecipe(
                f"{name}: join into {weld.get('into')!r} lists no parts")

        steps.append({"action": "join_objects",
                      "params": {"objects": [into] + parts}})

        # Welded, not merely in the same object: joining leaves two
        # vertices at every place the surfaces touched, and a remesh
        # of a mesh full of doubles keeps the crease it was meant to
        # remove.
        if weld.get("merge", 0.0):
            steps.append({"action": "merge_by_distance",
                          "params": {"distance": weld["merge"]}})

        remesh = weld.get("remesh")
        if remesh:
            steps.append({"action": "voxel_remesh",
                          "params": {"object": into,
                                     "size": remesh.get("size", 0.05),
                                     "adaptivity": remesh.get("adaptivity", 0.0)}})

        if weld.get("smooth", True):
            steps.append({"action": "smooth_shade", "params": {"object": into}})

    return steps


def _boxes(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The one or more boxes a garment is cut with.

    A garment usually covers one region and writes its bounds straight
    onto itself. Some cover two places that a single box cannot reach
    between -- gloves being the case that forced this -- and those list
    them under "regions" instead.
    """
    listed = item.get("regions")
    if isinstance(listed, list) and listed:
        return [box for box in listed if isinstance(box, dict)]
    return [item]


def _files() -> List[Path]:
    if not RECIPE_ROOT.is_dir():
        return []
    # Visual Studio leaves a .vs folder in there. It is not a recipe.
    return sorted(p for p in RECIPE_ROOT.glob("**/*.json") if ".vs" not in p.parts)


def _index() -> Dict[str, Path]:
    found: Dict[str, Path] = {}
    for path in _files():
        try:
            name = json.loads(path.read_text(encoding="utf-8")).get("name")
        except (OSError, ValueError) as error:
            logger.warning("recipe %s could not be read: %s", path.name, error)
            continue
        if name:
            found[str(name)] = path
    return found


def names() -> List[str]:
    """Every recipe the library holds, by its own name."""
    return sorted(_index())


def catalogue() -> List[Dict[str, str]]:
    """Name, category and description for each, for "what can you build"."""
    listing = []
    for name, path in sorted(_index().items()):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        listing.append({
            "name": name,
            "category": path.parent.name,
            "description": str(data.get("description", "")),
        })
    return listing


def load(name: str) -> Dict[str, Any]:
    """One recipe, as it is written on disk."""
    index = _index()
    path = index.get(str(name))
    if path is None:
        raise UnknownRecipe(
            f"no recipe called {name!r}. The library has: "
            f"{', '.join(sorted(index)) or 'nothing'}")
    return json.loads(path.read_text(encoding="utf-8"))


def prefix_for(name: str) -> str:
    """"base_humanoid" becomes "Humanoid" -- its parts are Humanoid_Torso.

    The "base_" every file carries says where it sits in the library,
    not what the thing is, and carrying it into the object names would
    carry it into the Unity hierarchy too.
    """
    bare = str(name)
    if bare.startswith("base_"):
        bare = bare[len("base_"):]
    return _pascal(bare)


def part_names(name: str, prefix: Optional[str] = None) -> List[str]:
    """What the meshes will be called, without building anything.

    So a rig can name the mesh it binds to before the build has run.

    Garments count. They are meshes the build creates, they are named
    the same way objects are, and `auto_bind` binds them by name -- so
    a list that left them out was a list a rig could not be checked
    against, which is the one job this has.

    In build order: primitives first, then the garments cut from the
    base, because that is the order `actions` emits them in.
    """
    recipe = load(name)
    head = prefix or prefix_for(name)
    garments: List[str] = []
    for item in recipe.get("garments", []):
        worn = _name(head, item.get("id"), "Garment")
        garments.append(worn)
        # A neckline cutter is made and deleted inside the build; it is
        # still a mesh the build created, in this order.
        garments += [f"{worn}_Cut{n}" for n in range(1, len(item.get("cuts", [])) + 1)]
    drapes = [_name(head, item.get("id"), "Drape") for item in recipe.get("drapes", [])]
    return [_name(head, item.get("id")) for item in recipe.get("objects", [])] + garments + drapes


def _one_object(item: Dict[str, Any], head: str,
                marks: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    kind = str(item.get("type", "")).lower()
    action = PRIMITIVES.get(kind)
    if action is None:
        raise UnsupportedRecipe(
            f"{item.get('id')!r} is a {kind!r}, and this translation knows "
            f"{', '.join(sorted(PRIMITIVES))}")

    marks = marks or {}
    name = _name(head, item.get("id"))
    where = f"{item.get('id')}"
    location = _point(item.get("location") or [0, 0, 0], marks, where + " location")

    def dim(key, default):
        """A radius or a depth, which may also be named off the body."""
        return resolve(item.get(key, default), marks, f"{where} {key}")

    size = _point(item.get("size"), marks, where + " size")
    steps: List[Dict[str, Any]] = []

    if kind == "cube":
        # One number to Blender, three to the scale afterwards. See the
        # note at the top of the module.
        steps.append({"action": "add_cube",
                      "params": {"name": name, "size": 1.0, "location": location}})
        if isinstance(size, (list, tuple)):
            x, y, z = (list(size) + [1.0, 1.0, 1.0])[:3]
            steps.append({"action": "scale",
                          "params": {"object": name, "x": x, "y": y, "z": z}})
        elif size is not None:
            steps[-1]["params"]["size"] = resolve(size, marks, where + " size")

    elif kind == "sphere":
        steps.append({"action": "add_sphere",
                      "params": {"name": name, "radius": dim("radius", 1.0),
                                 "location": location}})

    elif kind == "cylinder":
        steps.append({"action": "add_cylinder",
                      "params": {"name": name, "radius": dim("radius", 1.0),
                                 "depth": dim("depth", 2.0),
                                 "location": location}})

    elif kind == "cone":
        steps.append({"action": "add_cone",
                      "params": {"name": name, "radius": dim("radius", 1.0),
                                 "radius_top": dim("radius_top", 0.0),
                                 "depth": dim("depth", 2.0),
                                 "location": location}})

    elif kind == "plane":
        steps.append({"action": "add_plane",
                      "params": {"name": name, "size": dim("size", 2.0),
                                 "location": location}})

    elif kind == "torus":
        steps.append({"action": "add_torus",
                      "params": {"name": name,
                                 "major_radius": dim("major_radius", 1.0),
                                 "minor_radius": dim("minor_radius", 0.25),
                                 "location": location}})

    rotation = item.get("rotation")
    if rotation:
        x, y, z = (list(rotation) + [0.0, 0.0, 0.0])[:3]
        steps.append({"action": "rotate",
                      "params": {"object": name, "x": x, "y": y, "z": z}})

    # Left unapplied, so the blockout stays editable and a later pass
    # can still move the cage rather than the smoothed result.
    levels = item.get("subdivision")
    if levels:
        steps.append({"action": "apply_subdivision",
                      "params": {"object": name, "levels": levels, "apply": False}})

    return steps


def actions(name: str, *, prefix: Optional[str] = None,
            clear: bool = True, cleanup: bool = True,
            rig: bool = False, animate: Optional[str] = None,
            export: bool = False) -> List[Dict[str, Any]]:
    """A recipe, as the list of actions that builds it.

    `clear` empties the scene first, which is almost always right:
    Blender's default scene has a cube, a camera and a light in it, and
    a humanoid built around a stray cube is the sort of thing noticed
    after the export. Turn it off to build a second thing beside the
    first, which is how the miner gets his pickaxe.
    """
    recipe = load(name)
    head = prefix or prefix_for(name)
    marks = _marks_for(recipe)

    steps: List[Dict[str, Any]] = []
    if clear:
        steps.append({"action": "clear_scene"})

    # The base first: blanks are placed around a body, so the body has
    # to be standing there before they are.
    steps.extend(base_actions(name, prefix=head))
    steps.extend(proportions_actions(name, prefix=head))

    for item in recipe.get("objects", []):
        steps.extend(_one_object(item, head, marks))

    # Joining happens BEFORE the modifiers, so a modifier can act on
    # the thing that came out of it. Stamping lumps onto a rock is the
    # case: four separate spheres each get their own noise and stay
    # four spheres, while one joined and remeshed rock takes the noise
    # across its whole surface and becomes a rock.
    steps.extend(join_actions(name, prefix=head))

    for modifier in recipe.get("modifiers", []):
        kind = str(modifier.get("type", "")).lower()
        action = MODIFIERS.get(kind)
        if action is None:
            raise UnsupportedRecipe(
                f"modifier {kind!r} on {modifier.get('target')!r} is not one of "
                f"{', '.join(sorted(MODIFIERS))}")

        params = {key: value for key, value in modifier.items()
                  if key not in ("type", "target") and key not in MODIFIER_OBJECTS}
        params["object"] = _name(head, modifier.get("target"))
        for written, called in MODIFIER_OBJECTS.items():
            if modifier.get(written) is not None:
                params[called] = _name(head, modifier[written])
        steps.append({"action": action, "params": params})

    # Garments are cut from the base, so the base has to exist and the
    # blanks must not have been merged into anything yet.
    steps.extend(garment_actions(name, prefix=head))

    # Fitting happens before cleanup, because origin_to_geometry moves
    # every origin and a wrap done afterwards would be aiming at a body
    # that has since shifted under it.
    steps.extend(fit_actions(name, prefix=head))
    steps.extend(drape_actions(name, prefix=head))

    # And now the scaffolding can go. Two kinds of it, and the reason
    # is the same for both: export_fbx writes every mesh in the file,
    # not a selection. A garment recipe has to stand a body up to cut
    # the garment out of -- without the drop, base_pants exports a
    # naked man wearing them. A boolean has to keep its cutter around
    # until the hole is made -- without the drop, base_house exports a
    # solid slab of door floating in its own doorway.
    base = recipe.get("base") or {}
    if base and not base.get("keep", True):
        steps.append({"action": "delete_object",
                      "params": {"object": base.get("as")
                                 or f"{head}_Base"}})

    for spent in recipe.get("discard", []):
        steps.append({"action": "delete_object",
                      "params": {"object": _name(head, spent)}})

    if cleanup:
        asked = recipe.get("cleanup") or {}
        for step in CLEANUP_ORDER:
            if not asked.get(step):
                continue
            if step == "merge_by_distance":
                steps.append({"action": step, "params": {"distance": 0.0001}})
            else:
                steps.append({"action": step, "params": {}})

    # The rig goes on after the cleanup: origin_to_geometry moves every
    # mesh origin, and doing that to a mesh already bound to an armature
    # moves the mesh out from under its own weights.
    if rig or animate:
        steps.extend(rig_actions(name, prefix=head))

    if animate:
        steps.extend(animation_actions(name, animate, prefix=head))

    if export:
        spec = recipe.get("export") or {}
        fmt = str(spec.get("format", "fbx")).lower().lstrip(".")
        action = EXPORT_ACTIONS.get(fmt)
        if action is None:
            raise UnsupportedRecipe(
                f"export format {fmt!r} is not one of "
                f"{', '.join(sorted(EXPORT_ACTIONS))}")
        steps.append({"action": action, "params": {"path": spec.get("path", "")}})

    return steps


def build_many(wanted: Iterable[str]) -> List[Dict[str, Any]]:
    """Several recipes into one scene: the first clears it, the rest do not.

    Cleanup is left to the caller, because applying transforms and
    recentring origins halfway through a build moves parts that a later
    recipe is about to be positioned against.
    """
    steps: List[Dict[str, Any]] = []
    for index, name in enumerate(wanted):
        steps.extend(actions(name, clear=(index == 0), cleanup=False))
    return steps


def outfit_actions(body: str, pieces: Iterable[str], *, rig: bool = True,
                   clips: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """A body dressed in a set of kit pieces, all on ONE skeleton.

    WHY ONE SKELETON. Clothes are only exchangeable if every piece moves
    with the same bones: in Unity the character is then one rig with a
    mesh per piece, and changing outfit is switching meshes on and off.
    So the body is rigged once (its recipe's `armature`), and every mesh
    a piece made takes the weights of the body surface under it
    (transfer_weights, at rest) and is parented to that rig -- a sleeve
    bends exactly as the arm inside it does.

    `clips` adds motions (walk, idle, wave...) to the shared rig, so the
    whole outfit can be watched moving together.
    """
    pieces = list(pieces)
    steps = build_many([body] + pieces)
    if not rig:
        return steps

    spec = load(body)
    steps.extend(rig_actions(body))
    body_mesh = (spec.get("base") or {}).get("as") or f"{prefix_for(body)}_Base"
    armature = (spec.get("armature") or {}).get("name") or f"{prefix_for(body)}_Rig"

    # Only the meshes that survive the build: a neckline cutter is made
    # and deleted inside it, and has nothing to be weighted.
    deleted = {step["params"].get("object") for step in steps
               if step["action"] == "delete_object"}
    for piece in pieces:
        for part in part_names(piece):
            if part in deleted:
                continue
            steps.append({"action": "transfer_weights",
                          "params": {"source": body_mesh, "target": part,
                                     "armature": armature}})
    for clip in clips:
        steps.append({"action": "add_clip", "params": {"armature": armature, "clip": clip}})
    return steps


# ======================================================
# Rig and motion
# ======================================================
#
# A recipe may carry two more sections. Both are optional and most of
# the library has neither -- a helmet does not need a skeleton.
#
#   "armature": {
#     "name": "Miner_Rig",
#     "bones": [{"name": "Spine", "head": [...], "tail": [...],
#                "parent": "Hips", "connect": true}, ...],
#     "bind":  {"chest": "Spine", "upper_arm_r": "UpperArm_R", ...},
#     "ik":    [{"bone": "Forearm_L", "subtarget": "HaftHold",
#                "chain_count": 2}]
#   },
#   "animations": [
#     {"name": "swing", "frames": 24,
#      "keys":  [{"frame": 1, "bone": "Shoulder_R", "rotation": [x, y, z]}],
#      "holds": [{"frame": 6, "bone": "Shoulder_R", "interpolation": "CONSTANT"}]}
#   ]
#
# `bind` maps a recipe's own object ids to bone names, and every part
# goes to exactly ONE bone at full weight. That is deliberate: these
# models are blockouts made of separate solid parts, and `auto_weights`
# guesses influence by distance, which on a figure whose upper arm
# overlaps its chest guesses wrong -- the shoulder drags a corner of the
# chest with it and the seam pulls open. A forearm is a rigid thing and
# belongs to the forearm bone.
#
# `holds` is where the timing lives. Blender keys BEZIER, so a posed rig
# already eases between frames and most of a motion wants nothing else;
# a hold is for the moment before a chop, which has to sit dead still or
# the blow has no break in it.


def rig_actions(name: str, prefix: Optional[str] = None) -> List[Dict[str, Any]]:
    """The skeleton, the binding and the IK, for a recipe that has them."""
    recipe = load(name)
    spec = recipe.get("armature")
    if not spec:
        return []

    head = prefix or prefix_for(name)
    arm = spec.get("name") or f"{head}_Rig"
    marks = _marks_for(recipe)

    # A reshaped body has moved away from its base's landmark table, so
    # bones placed by those landmarks would sit where the OLD body was.
    # auto_rig measures the body that is actually there instead.
    if spec.get("auto_rig"):
        body = spec.get("object") or (recipe.get("base") or {}).get("as") or f"{head}_Base"
        options = spec["auto_rig"] if isinstance(spec["auto_rig"], dict) else {}
        # Only the options this build's auto_rig reads: a recipe written
        # for a newer rig (keep_off_arms, cut_bridges...) must not stop an
        # older one from rigging at all.
        from backend.blender import blender_script_templates as _templates
        known = set(_templates.parameters("auto_rig"))
        options = {key: value for key, value in options.items() if key in known}
        return [{"action": "find_landmarks", "params": {"object": body, "kind": "body"}},
                {"action": "auto_rig", "params": {"object": body, "name": arm, **options}}]

    steps: List[Dict[str, Any]] = [
        {"action": "create_armature",
         "params": {"name": arm,
                    "location": _point(spec.get("location") or [0, 0, 0], marks,
                                       f"{name} armature location")}},
    ]

    # Bones may be placed by landmark too, which is what stops a rig
    # from belonging to one body: "Neck" at ["neck"] is at the neck of
    # whatever this recipe stands on.
    for bone in spec.get("bones", []):
        where = f"{name}/{bone.get('name')}"
        steps.append({"action": "add_bone",
                      "params": {"armature": arm,
                                 "name": bone.get("name"),
                                 "head": _point(bone.get("head"), marks, where + " head"),
                                 "tail": _point(bone.get("tail"), marks, where + " tail"),
                                 "parent": bone.get("parent"),
                                 "connect": bool(bone.get("connect"))}})

    known = {item.get("id") for item in recipe.get("objects", [])}
    for part, bone in (spec.get("bind") or {}).items():
        if part not in known:
            raise UnsupportedRecipe(
                f"{name}: bind names {part!r}, which is not one of its objects "
                f"({', '.join(sorted(str(k) for k in known))})")
        mesh = _name(head, part)
        steps.append({"action": "bind_to_bone",
                      "params": {"mesh": mesh, "bone": bone, "weight": 1.0}})
        steps.append({"action": "parent_mesh_to_armature",
                      "params": {"mesh": mesh, "armature": arm}})

    # A single continuous mesh -- a real base body, or a garment wrapped
    # onto one -- cannot be nailed to one bone the way a blockout's
    # separate forearm can. It needs weights that fall off across the
    # joint, which is what auto_weights computes.
    # Garments are named the same way objects are -- by id, with the
    # recipe's prefix -- so auto_bind has to know about both lists or a
    # garment's name goes through untouched and finds nothing.
    known_ids = ({item.get("id") for item in recipe.get("objects", [])} |
                 {item.get("id") for item in recipe.get("garments", [])})
    base_called = (recipe.get("base") or {}).get("as")

    for whole in spec.get("auto_bind", []):
        # A recipe's own object ids get the recipe's prefix. The base's
        # name does not: it is written in the base section as the thing
        # it will be called, and prefixing it invented CrewMiner_Miner
        # Body, which nothing had ever made.
        if whole in known_ids:
            mesh = _name(head, whole)
        elif whole == base_called or not whole.startswith(head):
            mesh = whole
        else:
            mesh = whole
        steps.append({"action": "auto_weights",
                      "params": {"mesh": mesh, "armature": arm}})
        steps.append({"action": "normalize_weights", "params": {"mesh": mesh}})

    for pull in spec.get("ik", []):
        steps.append({"action": "add_ik_constraint",
                      "params": {"armature": arm,
                                 "bone": pull.get("bone"),
                                 # The target is the rig itself unless a
                                 # recipe says otherwise: the thing a hand
                                 # reaches for is a bone on the same
                                 # skeleton, like the grip on a haft.
                                 "target": pull.get("target") or arm,
                                 "subtarget": pull.get("subtarget"),
                                 "chain_count": pull.get("chain_count", 2),
                                 "pole_target": pull.get("pole_target"),
                                 "pole_subtarget": pull.get("pole_subtarget"),
                                 "pole_angle": pull.get("pole_angle")}})

    return steps


def animation_names(name: str) -> List[str]:
    """What motions this recipe carries."""
    return [str(clip.get("name")) for clip in load(name).get("animations", [])]


def animation_actions(name: str, clip: str,
                      prefix: Optional[str] = None) -> List[Dict[str, Any]]:
    """One named motion, as poses and the shape of the curves between them."""
    recipe = load(name)
    head = prefix or prefix_for(name)
    arm = (recipe.get("armature") or {}).get("name") or f"{head}_Rig"

    for found in recipe.get("animations", []):
        if str(found.get("name")) == str(clip):
            break
    else:
        raise UnknownRecipe(
            f"{name} has no motion called {clip!r}. It has: "
            f"{', '.join(animation_names(name)) or 'none'}")

    steps: List[Dict[str, Any]] = []

    frames = found.get("frames")
    if frames:
        steps.append({"action": "set_frame", "params": {"frame": 1}})

    for key in found.get("keys", []):
        steps.append({"action": "set_pose",
                      "params": {"armature": arm,
                                 "bone": key.get("bone"),
                                 "rotation": key.get("rotation") or [0, 0, 0],
                                 "frame": key.get("frame", 1)}})

    # After the keys exist, because interpolation is a property OF a key.
    for hold in found.get("holds", []):
        steps.append({"action": "set_interpolation",
                      "params": {"object": arm,
                                 "bone": hold.get("bone"),
                                 "frame": hold.get("frame"),
                                 "interpolation": hold.get("interpolation", "CONSTANT"),
                                 "easing": hold.get("easing")}})

    return steps


# ======================================================
# The base model library
# ======================================================

def base_models(kind: str = "") -> List[Dict[str, Any]]:
    """Every authored base mesh on hand, with where it lives.

    Read from the manifests in `aria_models/` rather than by walking
    the folder, because what matters about a base is not that a file
    exists but what is IN it -- which object to append, how many polys
    it carries, whether it has UVs. A 49MB .blend holding 382 meshes
    answers none of that by being on disk.

    The .blend itself may legitimately be absent: it is fetched and
    checksum-verified rather than committed, so `available` says
    whether this clone has actually got it yet.
    """
    found: List[Dict[str, Any]] = []
    if not MODEL_ROOT.is_dir():
        return found

    for path in sorted(MODEL_ROOT.glob("manifest_*.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            logger.warning("model manifest %s could not be read: %s", path.name, error)
            continue

        blend = MODEL_ROOT / str(spec.get("file", {}).get("path", ""))

        for base in spec.get("bases", []):
            if kind and kind.lower() not in str(base.get("use", "")).lower():
                continue
            found.append({
                "bundle": spec.get("name"),
                "object": base.get("object"),
                "use": base.get("use"),
                "verts": base.get("verts"),
                "polys": base.get("polys"),
                "uv": base.get("uv"),
                "licence": (spec.get("licence") or {}).get("id"),
                "file": str(blend),
                "available": blend.is_file(),
            })

    return found


# ======================================================
# Starting from a base, and fitting things to it
# ======================================================

def _library_blend(library: str) -> str:
    """The .blend a named library lives in, from its manifest."""
    for base in base_models():
        if base.get("bundle") == library:
            return base["file"]
    known = sorted({b.get("bundle") for b in base_models()})
    raise UnknownRecipe(
        f"no base library called {library!r}. There is: {', '.join(str(k) for k in known) or 'nothing'}")


def base_actions(name: str, prefix: Optional[str] = None,
                 called: Optional[str] = None) -> List[Dict[str, Any]]:
    """Bring in the recipe's base mesh, sized and stood on the floor.

    A recipe with no `base` gets nothing, which is right for a rock or
    a crate -- those are still better made out of primitives, and
    aria_recipes/blender builds them that way.
    """
    recipe = load(name)
    spec = recipe.get("base")
    if not spec:
        return []

    called = called or spec.get("as") or f"{prefix or prefix_for(name)}_Base"

    # A body another recipe makes -- the anime figures are a base
    # reshaped by `proportions` -- is built by that recipe's own steps,
    # then renamed to what this recipe calls it. Its landmarks are filed
    # under the recipe's name (measure_base_landmarks RECIPE_BODIES).
    if spec.get("recipe"):
        maker = str(spec["recipe"])
        # Built under THIS recipe's name, so it cannot collide with the
        # body itself when both are in one scene (build_many).
        return base_actions(maker, called=called) + proportions_actions(maker, called=called)

    blend = spec.get("blend") or _library_blend(str(spec.get("library", "")))

    steps: List[Dict[str, Any]] = [
        {"action": "append_from_blend",
         "params": {"blend": blend, "object": spec.get("object"),
                    "name": called, "location": spec.get("location") or [0, 0, 0]}},
    ]

    # Scaled BEFORE anything is measured against it, because the
    # landmark table describes the body at its recipe height and a
    # garment box cut against an unscaled one would miss entirely.

    if spec.get("height"):
        steps.append({"action": "scale_to_height",
                      "params": {"object": called, "height": spec["height"]}})
        steps.append({"action": "apply_transforms", "params": {}})
        steps.append({"action": "origin_to_floor", "params": {"object": called}})

    return steps


_BOX_KEYS = ("x_min", "x_max", "y_min", "y_max", "z_min", "z_max")


def proportions_actions(name: str, prefix: Optional[str] = None,
                        called: Optional[str] = None) -> List[Dict[str, Any]]:
    """Reshape the base into a style: longer legs, slimmer limbs, a smaller head.

    A style is mostly proportions, so a recipe can take a base body and
    re-proportion it rather than needing a new mesh for every look.
    `parts` are boxes on the body AS THE BASE ARRIVES (landmarks mean
    the base's own heights), `heights` remaps altitude afterwards, and
    `height` rescales the result to the size the character should be.
    See reshape_body for what each pivot does.

    Garment recipes still measure the ORIGINAL base: landmarks are not
    re-measured after a reshape, so clothes cut for base_stylized_body
    will not fit an anime body without their own bounds.
    """
    recipe = load(name)
    spec = recipe.get("proportions")
    base = recipe.get("base") or {}
    if not spec or not base:
        return []

    marks = _marks_for(recipe)
    called = called or base.get("as") or f"{prefix or prefix_for(name)}_Base"

    parts = []
    for part in spec.get("parts", []):
        where = f"{name} proportions/{part.get('name')}"
        clean = {key: resolve(part.get(key), marks, where) for key in _BOX_KEYS
                 if part.get(key) is not None}
        clean.update({key: part[key] for key in ("name", "soft", "scale") if key in part})
        pivot = part.get("pivot", "center")
        clean["pivot"] = _point(pivot, marks, where + " pivot") if isinstance(pivot, list) else pivot
        parts.append(clean)

    heights = [[resolve(old, marks, f"{name} heights"), new]
               for old, new in spec.get("heights", [])]

    steps: List[Dict[str, Any]] = [
        {"action": "reshape_body",
         "params": {"object": called, "parts": parts, "heights": heights}}]
    if spec.get("height"):
        steps.append({"action": "scale_to_height",
                      "params": {"object": called, "height": spec["height"]}})
        steps.append({"action": "apply_transforms", "params": {}})
        steps.append({"action": "origin_to_floor", "params": {"object": called}})
    if spec.get("smooth", True):
        steps.append({"action": "smooth_shade", "params": {"object": called}})
    return steps


def _drape_mesh(spec: Dict[str, Any], marks: Dict[str, Any], where: str):
    """Points and faces of a flared, open-fronted skirt round the body.

    Rings from `top` down to `bottom`, each an ellipse: half-width
    `width` and depth `front`/`back` at the top, growing by `flare`
    toward the hem (eased, so it hangs straight first and swings out
    lower down, the way a long coat does). The front is left open over
    `opening` degrees either side of straight ahead, widening from
    `opening[0]` at the top to `opening[1]` at the hem.
    """
    import math

    top = float(resolve(spec.get("top", "hip"), marks, where))
    bottom = float(resolve(spec.get("bottom", "knee"), marks, where))
    width = float(resolve(spec.get("width", "hip_x+0.04"), marks, where))
    front = float(resolve(spec.get("front", 0.14), marks, where))
    back = float(resolve(spec.get("back", 0.15), marks, where))
    flare = [float(v) for v in (spec.get("flare") or [0.1, 0.08])]
    opening = [math.radians(float(v)) for v in (spec.get("opening") or [20, 50])]
    rows = max(4, int(spec.get("rows", 24)))
    columns = max(8, int(spec.get("columns", 48)))
    ease = float(spec.get("ease", 1.6))
    centre_y = float(spec.get("centre_y", 0.0))

    # A `profile` says the shape outright, [height, half-width, front,
    # back] at several heights, top first -- a coat follows the waist,
    # clears the hips, then swings out. Without one, width + flare.
    profile = [[float(resolve(v, marks, where)) for v in point]
               for point in spec.get("profile") or []]
    if profile:
        top, bottom = profile[0][0], profile[-1][0]

    def shape_at(z: float, t: float):
        if not profile:
            swing = t ** ease
            return (width + flare[0] * swing, front + flare[1] * swing,
                    back + flare[1] * swing)
        for upper, lower in zip(profile, profile[1:]):
            if lower[0] <= z <= upper[0]:
                f = (upper[0] - z) / ((upper[0] - lower[0]) or 1e-9)
                return tuple(upper[k] + (lower[k] - upper[k]) * f for k in (1, 2, 3))
        edge = profile[0] if z >= profile[0][0] else profile[-1]
        return edge[1], edge[2], edge[3]

    vertices, faces = [], []
    for r in range(rows + 1):
        t = r / rows
        z = top + (bottom - top) * t
        rx, rf, rb = shape_at(z, t)
        gap = opening[0] + (opening[1] - opening[0]) * t
        for c in range(columns + 1):
            # Angle measured from straight ahead (-Y), round the back
            # and out the other side, stopping short of the opening.
            a = gap + (2 * math.pi - 2 * gap) * c / columns
            depth = rf if math.cos(a) > 0 else rb
            vertices.append([round(rx * math.sin(a), 5),
                             round(centre_y - depth * math.cos(a), 5),
                             round(z, 5)])
    # A torn hem: `hem` {"teeth": n, "depth": metres, "seed": k} drops the
    # bottom rows into a ragged row of points, each tooth its own length
    # (seeded, so the same recipe tears the same way every build).
    hem = spec.get("hem")
    if hem:
        import random
        chance = random.Random(int(hem.get("seed", 1)))
        teeth = max(2, int(hem.get("teeth", 9)))
        depth = float(hem.get("depth", 0.08))
        peaks = [0.35 + 0.65 * chance.random() for _ in range(teeth + 1)]
        for c in range(columns + 1):
            along = c / columns * teeth
            k = int(along)
            f = along - k
            nxt = peaks[min(k + 1, teeth)]
            # Points at the middle of each tooth, notches between them.
            drop = depth * (1 - abs(2 * f - 1)) * (peaks[k] * (1 - f) + nxt * f)
            for back, share in ((0, 1.0), (1, 0.4), (2, 0.15)):
                r = rows - back
                if r >= 0:
                    vertices[r * (columns + 1) + c][2] = round(
                        vertices[r * (columns + 1) + c][2] - drop * share, 5)

    stride = columns + 1
    for r in range(rows):
        for c in range(columns):
            i = r * stride + c
            faces.append([i, i + 1, i + 1 + stride, i + stride])
    return vertices, faces


def _colour_steps(worn: str, item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A flat colour for one piece, when the recipe gives one.

    So a kit piece can be judged against its design in colour -- a
    black coat, a purple lining, teal cuffs -- instead of all-grey clay.
    The real look comes from the texture later; this is the stand-in.
    """
    colour = item.get("color")
    if not colour:
        return []
    material = f"{worn}_Colour"
    return [{"action": "create_material",
             "params": {"name": material, "color": list(colour)[:3],
                        "roughness": item.get("roughness", 0.8)}},
            {"action": "assign_material", "params": {"object": worn, "material": material}}]


def drape_actions(name: str, prefix: Optional[str] = None) -> List[Dict[str, Any]]:
    """Cloth that hangs where the body has nothing to cut it from.

    A garment cut from the body can only give back surface the body
    has, so a coat cut that way stops at the hips -- below them there is
    nothing but two legs, and a cut there is a pair of trousers. The
    tails of a long coat hang in the air, so they are generated: a
    flared, open-fronted skirt placed from the body's own landmarks (see
    _drape_mesh), then thickened and shaded like any garment.
    """
    recipe = load(name)
    head = prefix or prefix_for(name)
    marks = _marks_for(recipe)
    steps: List[Dict[str, Any]] = []
    for item in recipe.get("drapes", []):
        worn = _name(head, item.get("id"), "Drape")
        vertices, faces = _drape_mesh(item, marks, f"{name}/{item.get('id')}")
        steps.append({"action": "create_mesh",
                      "params": {"name": worn, "vertices": vertices, "faces": faces}})
        if item.get("thickness"):
            steps.append({"action": "apply_solidify",
                          "params": {"object": worn, "thickness": item["thickness"],
                                     "apply": True}})
        steps.append({"action": "smooth_shade", "params": {"object": worn}})
        if item.get("unwrap", True):
            steps.append({"action": "smart_uv_project",
                          "params": {"object": worn, "angle_limit": 1.15, "margin": 0.02}})
        steps.extend(_colour_steps(worn, item))
    return steps


def fit_actions(name: str, prefix: Optional[str] = None) -> List[Dict[str, Any]]:
    """Wrap each blank onto what it is being worn by, then thicken it.

    This is where a cylinder becomes a vest. The blank is a rough shape
    roughly where the garment goes; the wrap takes it onto the body's
    own surface so it fits the character it is being worn by, and the
    solidify gives it thickness so it is cloth rather than paint.

    Order matters and is not obvious: wrap first, THEN thicken. Thicken
    first and the wrap pulls both surfaces onto the body and the
    garment has no thickness left at all.
    """
    recipe = load(name)
    head = prefix or prefix_for(name)
    marks = _marks_for(recipe)
    steps: List[Dict[str, Any]] = []

    for item in recipe.get("fit", []):
        worn = _name(head, item.get("object"))
        onto = item.get("onto")
        if onto and not onto.startswith(head) and onto not in {o.get("id") for o in recipe.get("objects", [])}:
            body = onto                      # a base's own name, used as written
        else:
            body = _name(head, onto, "Base")

        steps.append({"action": "apply_shrinkwrap",
                      "params": {"object": worn, "target": body,
                                 "method": item.get("method", "NEAREST_SURFACEPOINT"),
                                 "offset": resolve(item.get("offset", 0.014), marks,
                                                   f"{name} fit offset"),
                                 "apply": True}})

        thickness = item.get("thickness")
        if thickness:
            steps.append({"action": "apply_solidify",
                          "params": {"object": worn, "thickness": thickness, "apply": True}})

        if item.get("smooth", True):
            steps.append({"action": "smooth_shade", "params": {"object": worn}})

    return steps


def garment_actions(name: str, prefix: Optional[str] = None) -> List[Dict[str, Any]]:
    """Cut a garment out of the body it is worn by.

    Copy the body, keep the box the garment covers, throw the rest
    away, push what is left out along its own normals, and thicken it.
    The result fits exactly because it IS the body -- there is no
    wrapping and so nothing to choose wrongly.

    This is what replaced the blanks. A cylinder wrapped onto a torso
    came out a crop top; a cylinder wrapped onto two legs bridged them
    into a skirt; every hem was ragged where the wrap ran out. A cut
    has a hem wherever the mask ends and legs wherever the body has
    them.

    The bounds are in WORLD metres, the same ones the body was measured
    in -- waist 1.02, chest 1.26 to 1.44, knee 0.48 -- so a recipe says
    what it means.
    """
    recipe = load(name)
    head = prefix or prefix_for(name)
    base_called = (recipe.get("base") or {}).get("as") or f"{head}_Base"
    marks = _marks_for(recipe)

    steps: List[Dict[str, Any]] = []

    for item in recipe.get("garments", []):
        worn = _name(head, item.get("id"), "Garment")
        body = item.get("from") or base_called
        group = "Region_" + _pascal(item.get("id") or "Garment")

        steps.append({"action": "duplicate_object",
                      "params": {"object": body, "name": worn}})

        # A garment may be cut from several boxes at once. The same
        # group name every time, and a vertex group is a union, so the
        # boxes add up. This is how a pair of GLOVES is one garment:
        # the hands are two places at the same height, and the single
        # box that would hold both would hold the hips between them.
        for box in _boxes(item):
            region = {"object": worn, "name": group,
                      "soft": resolve(box.get("soft", item.get("soft", 0.015)),
                                      marks, f"{name}/{item.get('id')} soft")}
            for key in ("x_min", "x_max", "y_min", "y_max", "z_min", "z_max"):
                if box.get(key) is not None:
                    region[key] = resolve(box[key], marks,
                                          f"{name}/{item.get('id')} {key}")
            steps.append({"action": "vertex_group_by_region", "params": region})

        steps.append({"action": "apply_mask",
                      "params": {"object": worn, "group": group,
                                 "threshold": item.get("threshold", 0.05), "apply": True}})

        # Smoothing goes BEFORE the inflate and before the thickness.
        # It is what turns a leg-shaped cut into a trouser leg -- see
        # relax_surface -- and it shrinks slightly as it works, so the
        # inflate that follows is also what puts back what it took.
        easing = item.get("relax")
        if easing:
            steps.append({"action": "relax_surface",
                          "params": {"object": worn,
                                     "factor": easing.get("factor", 0.5),
                                     "iterations": easing.get("iterations", 6),
                                     "axis": easing.get("axis", "xyz"),
                                     # The group the cut was made with,
                                     # which fades out at the hem.
                                     "group": group,
                                     "apply": True}})

        # Out along its own normals, NOT by shrinkwrapping onto the body
        # it was copied from: every vertex would find itself at distance
        # zero, where there is no direction to offset along, and the two
        # surfaces would stay coincident and z-fight into speckle.
        steps.append({"action": "inflate",
                      "params": {"object": worn,
                                 "distance": item.get("offset", 0.012), "apply": True}})

        # Optional, and the answer to clothing that fits so well it
        # reads as paint. A garment cut from a body is body-shaped by
        # construction, so trousers come out as leggings and a coat as
        # a wetsuit; a cast toward a cylinder gives the shape back the
        # bulk that says cloth rather than skin.
        shaping = item.get("cast")
        if shaping:
            steps.append({"action": "apply_cast",
                          "params": {"object": worn,
                                     "shape": shaping.get("shape", "CYLINDER"),
                                     "factor": shaping.get("factor", 0.25),
                                     "radius": shaping.get("radius", 0.0),
                                     "size": shaping.get("size", 0.0),
                                     "apply": True}})

        if item.get("thickness"):
            steps.append({"action": "apply_solidify",
                          "params": {"object": worn, "thickness": item["thickness"],
                                     "apply": True}})

        # A clean edge where a box cut cannot give one. A mask follows
        # the body's own faces, so an edge that crosses them at an
        # angle -- a neckline round a neck -- comes out torn. `cuts`
        # carves the finished garment with a smooth oval instead:
        # {"at": [x, y, z], "size": [x, y, z]} are its centre and radii,
        # landmark names allowed. After the solidify, so the carve goes
        # clean through both faces of the cloth.
        for number, cut in enumerate(item.get("cuts", []), 1):
            where = f"{name}/{item.get('id')} cut {number}"
            cutter = f"{worn}_Cut{number}"
            radii = _point(cut.get("size") or [0.1, 0.1, 0.1], marks, where + " size")
            steps.append({"action": "add_sphere",
                          "params": {"name": cutter, "radius": 1.0,
                                     "location": _point(cut.get("at"), marks, where + " at")}})
            steps.append({"action": "scale",
                          "params": {"object": cutter, "x": radii[0], "y": radii[1], "z": radii[2]}})
            # "keep": true keeps what is INSIDE the oval instead -- how a
            # lapel is trimmed to a band along the coat's opening.
            steps.append({"action": "apply_boolean",
                          "params": {"object": worn, "target": cutter,
                                     "operation": "INTERSECT" if cut.get("keep") else "DIFFERENCE",
                                     "apply": True, "self_intersection": True}})
            steps.append({"action": "delete_object", "params": {"object": cutter}})

        if item.get("smooth", True):
            steps.append({"action": "smooth_shade", "params": {"object": worn}})
        steps.extend(_colour_steps(worn, item))


    return steps
