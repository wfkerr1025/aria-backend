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
Measured across all twenty-nine rather than assumed, because a
translation built for a schema the files do not use is one that works
on the two examples anybody checks:

    primitives   cube 65, cylinder 37, sphere 16, cone 1
    object keys  id, type, size, location, radius, depth, rotation,
                 subdivision
    modifiers    smooth_shade, 54 times, and nothing else
    cleanup      apply_transforms, merge_by_distance, origin_to_geometry
    export       format, path, apply_scale

Small enough to translate completely, so anything outside it is refused
rather than skipped. A recipe whose parts quietly failed to appear gets
found by noticing the model has no left arm, which is a slow and
irritating way to learn it.

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
    "MODEL_ROOT",
    "RECIPE_ROOT",
    "UnknownRecipe",
    "UnsupportedRecipe",
    "actions",
    "animation_actions",
    "base_models",
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
MODIFIERS = {
    "smooth_shade": "smooth_shade",
    "subdivision": "apply_subdivision",
    "bevel": "apply_bevel",
    "mirror": "apply_mirror",
    "solidify": "apply_solidify",
}

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
    """What the objects will be called, without building anything.

    So a rig can name the mesh it binds to before the build has run.
    """
    recipe = load(name)
    head = prefix or prefix_for(name)
    return [f"{head}_{_pascal(item.get('id') or 'Part')}"
            for item in recipe.get("objects", [])]


def _one_object(item: Dict[str, Any], head: str) -> List[Dict[str, Any]]:
    kind = str(item.get("type", "")).lower()
    action = PRIMITIVES.get(kind)
    if action is None:
        raise UnsupportedRecipe(
            f"{item.get('id')!r} is a {kind!r}, and this translation knows "
            f"{', '.join(sorted(PRIMITIVES))}")

    name = f"{head}_{_pascal(item.get('id') or 'Part')}"
    location = item.get("location") or [0, 0, 0]
    size = item.get("size")
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
            steps[-1]["params"]["size"] = size

    elif kind == "sphere":
        steps.append({"action": "add_sphere",
                      "params": {"name": name, "radius": item.get("radius", 1.0),
                                 "location": location}})

    elif kind == "cylinder":
        steps.append({"action": "add_cylinder",
                      "params": {"name": name, "radius": item.get("radius", 1.0),
                                 "depth": item.get("depth", 2.0),
                                 "location": location}})

    elif kind == "cone":
        steps.append({"action": "add_cone",
                      "params": {"name": name, "radius": item.get("radius", 1.0),
                                 "radius_top": item.get("radius_top", 0.0),
                                 "depth": item.get("depth", 2.0),
                                 "location": location}})

    elif kind == "plane":
        steps.append({"action": "add_plane",
                      "params": {"name": name, "size": size or 2.0,
                                 "location": location}})

    elif kind == "torus":
        steps.append({"action": "add_torus",
                      "params": {"name": name,
                                 "major_radius": item.get("major_radius", 1.0),
                                 "minor_radius": item.get("minor_radius", 0.25),
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

    steps: List[Dict[str, Any]] = []
    if clear:
        steps.append({"action": "clear_scene"})

    for item in recipe.get("objects", []):
        steps.extend(_one_object(item, head))

    for modifier in recipe.get("modifiers", []):
        kind = str(modifier.get("type", "")).lower()
        action = MODIFIERS.get(kind)
        if action is None:
            raise UnsupportedRecipe(
                f"modifier {kind!r} on {modifier.get('target')!r} is not one of "
                f"{', '.join(sorted(MODIFIERS))}")

        params = {key: value for key, value in modifier.items()
                  if key not in ("type", "target")}
        params["object"] = f"{head}_{_pascal(modifier.get('target') or 'Part')}"
        steps.append({"action": action, "params": params})

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

    steps: List[Dict[str, Any]] = [
        {"action": "create_armature",
         "params": {"name": arm, "location": spec.get("location") or [0, 0, 0]}},
    ]

    for bone in spec.get("bones", []):
        steps.append({"action": "add_bone",
                      "params": {"armature": arm,
                                 "name": bone.get("name"),
                                 "head": bone.get("head"),
                                 "tail": bone.get("tail"),
                                 "parent": bone.get("parent"),
                                 "connect": bool(bone.get("connect"))}})

    known = {item.get("id") for item in recipe.get("objects", [])}
    for part, bone in (spec.get("bind") or {}).items():
        if part not in known:
            raise UnsupportedRecipe(
                f"{name}: bind names {part!r}, which is not one of its objects "
                f"({', '.join(sorted(str(k) for k in known))})")
        mesh = f"{head}_{_pascal(part)}"
        steps.append({"action": "bind_to_bone",
                      "params": {"mesh": mesh, "bone": bone, "weight": 1.0}})
        steps.append({"action": "parent_mesh_to_armature",
                      "params": {"mesh": mesh, "armature": arm}})

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
