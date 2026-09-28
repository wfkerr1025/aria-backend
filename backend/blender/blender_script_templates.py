"""ARIA Lite - the Python that gets sent to Blender.

Every Blender operation ARIA performs is a fragment generated here,
assembled into one script, written to a temp file and run by
`blender --background --python`.

WHY A TEMPLATE LAYER AND NOT GENERATED CODE
-------------------------------------------
The obvious design is to let the model write the bpy script. It is
also the one that cannot be made safe or reliable, and this session
has the evidence for both halves:

  Reliability -- asked for `blender --background --python <script>`,
  phi-3-mini produced `def main:` (not valid Python), invented
  `bpy.ops.preferences.addonSettings`, and repeated one line eleven
  times. A small model does not know bpy; it knows what bpy looks like.

  Safety -- a script is arbitrary code on the user's machine. A model
  that writes the script decides what runs.

So the shape of every statement is fixed here, and only leaf values
vary. Those are typed on the way in: numbers through float(), strings
through repr(), enums checked against an allowlist. A caller cannot
inject a statement because there is nowhere for a statement to go.

VERIFIED AGAINST BLENDER 5.0.1
------------------------------
Not written from memory. Every operator below was checked against the
installed build by listing bpy.ops, and the scripts are executed in
the tests. Two things that memory would have got wrong:

  mesh.use_auto_smooth was REMOVED in 4.1 and is absent in 5.0. Shading
  is set with object.shade_smooth() alone.

  io_scene_fbx and io_scene_gltf2 are enabled by default in this build,
  so FBX and glTF export need no add-on step. OBJ export is
  wm.obj_export -- the modern spelling; export_scene.obj still exists
  but is the older one.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "RESULT_CLOSE",
    "RESULT_OPEN",
    "TEMPLATES",
    "build_script",
    "describe_action",
    "known_actions",
    "parameters",
]

# The generated script prints its findings between these, so the runner
# can pick a result out of Blender's very chatty stdout. Same technique
# unity_ops uses against the Editor, for the same reason.
RESULT_OPEN = "###ARIA_BLENDER_RESULT_OPEN###"
RESULT_CLOSE = "###ARIA_BLENDER_RESULT_CLOSE###"

# What a modifier may be. Checked against the enum this Blender build
# actually accepts, rather than trusted from the caller.
MODIFIER_TYPES = frozenset({
    "MASK",
    "SUBSURF", "BEVEL", "MIRROR", "ARRAY", "BOOLEAN", "SOLIDIFY",
    "DECIMATE", "MULTIRES", "ARMATURE", "SHRINKWRAP", "LATTICE",
    "SIMPLE_DEFORM", "CAST",
})

# How a shrinkwrap finds the surface it is wrapping to. NEAREST_SURFACE
# POINT is the one for clothing: every vertex goes to the closest point
# on the body, so a rough shape takes the body's form.
WRAP_METHODS = frozenset({
    "NEAREST_SURFACEPOINT", "PROJECT", "NEAREST_VERTEX", "TARGET_PROJECT",
})

DEFORM_METHODS = frozenset({"TWIST", "BEND", "TAPER", "STRETCH"})

# What a cast pushes a shape toward. SPHERE is how a blocky thing
# becomes a soft one without touching a vertex by hand.
CAST_TYPES = frozenset({"SPHERE", "CYLINDER", "CUBOID"})

# How points get spread over a surface. POISSON keeps them apart, which
# is what scattered things actually look like; RANDOM lets them clump
# and overlap, which reads as a mistake more often than as nature.
DISTRIBUTE_METHODS = frozenset({"RANDOM", "POISSON"})

BOOLEAN_OPERATIONS = frozenset({"DIFFERENCE", "UNION", "INTERSECT"})
AXES = frozenset({"X", "Y", "Z"})
SCULPT_BRUSHES = frozenset({
    "DRAW", "CLAY", "CLAY_STRIPS", "INFLATE", "BLOB", "CREASE",
    "SMOOTH", "FLATTEN", "GRAB", "SNAKE_HOOK", "PINCH",
})
KEYFRAME_PATHS = frozenset({"location", "rotation_euler", "scale"})

# How a keyframe gets from its own value to the next one. Blender's own
# enum; BEZIER is its default and is why keyed poses already ease rather
# than tick. The named curves (SINE..ELASTIC) are the ones worth asking
# for by hand: CONSTANT holds a pose dead still until it breaks, which
# is what anticipation is made of.
INTERPOLATIONS = frozenset({
    "CONSTANT", "LINEAR", "BEZIER", "SINE", "QUAD", "CUBIC", "QUART",
    "QUINT", "EXPO", "CIRC", "BACK", "BOUNCE", "ELASTIC",
})

# Which end of the curve the easing happens at. Only meaningful for the
# named curves above; BEZIER and LINEAR ignore it.
EASINGS = frozenset({"AUTO", "EASE_IN", "EASE_OUT", "EASE_IN_OUT"})

# Bones an IK solver may walk up from the constrained one. Blender takes
# 0 to mean "all the way to the root", which on a full skeleton means an
# arm that drags the spine with it; the cap is against a typo doing that
# by accident.
IK_CHAIN_MAX = 8

# How a texture is sampled between its pixels. "Closest" is the one that
# matters here: painted game art is read at a size the artist chose, and
# smoothing it is how hand-drawn work starts looking like a photograph of
# hand-drawn work.
INTERPOLATIONS_IMAGE = frozenset({"Linear", "Closest", "Cubic", "Smart"})

# Where a picture may be plugged in. Base Color is the ordinary answer;
# Emission is how a surface carries its own painted light instead of
# taking the scene's, which is what a cut-out flat does and what a model
# standing beside one has to match.
TEXTURE_SLOTS = frozenset({"Base Color", "Emission", "Roughness", "Metallic", "Alpha", "Normal"})

# What Cycles can bake. NORMAL and AO are the two that carry sculpted
# detail down onto a low-poly mesh, which is the entire point of
# sculpting on a base model and then not shipping ten million triangles.
BAKE_TYPES = frozenset({
    "COMBINED", "AO", "SHADOW", "POSITION", "NORMAL", "UV", "ROUGHNESS",
    "EMIT", "ENVIRONMENT", "DIFFUSE", "GLOSSY", "TRANSMISSION",
})

# Inputs on a Principled BSDF worth setting by name. Not the full set:
# these are the ones a recipe has any business touching, and an
# allowlist beats a caller inventing "Roughtness" and being ignored.
SHADER_INPUTS = frozenset({
    "Base Color", "Metallic", "Roughness", "IOR", "Alpha", "Normal",
    "Emission Color", "Emission Strength", "Specular IOR Level",
    "Coat Weight", "Sheen Weight", "Subsurface Weight",
})

# Brush textures a sculpt stamp may wear.
STAMP_TEXTURE_TYPES = frozenset({"CLOUDS", "NOISE", "MUSGRAVE", "VORONOI", "DISTORTED_NOISE", "IMAGE"})

# How a displacement texture is laid over a surface. LOCAL follows the
# object, so detail stays put when it moves; UV follows the unwrap,
# which is what an image stamp wants.
DISPLACE_COORDS = frozenset({"LOCAL", "GLOBAL", "OBJECT", "UV"})

# Lamps Blender will make. SUN is the one that matters for baking a
# painted look: it is directional, so every part of a figure takes the
# light from the same angle, which is what an artist painting a
# character does by hand.
LIGHT_TYPES = frozenset({"SUN", "POINT", "SPOT", "AREA"})


# ======================================================
# Values
# ======================================================

class BadValue(ValueError):
    """A leaf value that is the wrong kind of thing.

    Raised while building, so it lands before Blender starts rather
    than halfway through a run -- the same reason build_script refuses
    an unknown action up front. A partial run leaves a scene nobody
    asked for.

    An absent value is not a bad one: None and "" take the default,
    which is how every optional parameter works.
    """


def _num(value: Any, default: float = 0.0) -> str:
    """A number, as a Python literal.

    float() is the whole defence: a caller can pass "1.5" or 1.5 and
    gets 1.5, and anything that is not a number becomes the default
    rather than reaching the script as text.
    """
    if value is None or value == "":
        return repr(float(default))
    try:
        return repr(float(value))
    except (TypeError, ValueError):
        raise BadValue(
            f"{value!r} is not a number. Numeric slots take numbers; "
            f"leave one out to get its default.") from None


def _int(value: Any, default: int = 1, low: int = 0, high: int = 1_000_000) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return repr(max(low, min(high, number)))


def _text(value: Any) -> str:
    """A string, as a Python literal.

    repr() is what makes a name safe. A caller passing
    `"); import os; os.system("rm -rf /` gets a string containing that
    text, because repr escapes the quote rather than closing it.
    """
    return repr("" if value is None else str(value))


def _vector(value: Any, default: Sequence[float] = (0.0, 0.0, 0.0)) -> str:
    """Three numbers. Anything shorter is padded from the default."""
    try:
        parts = list(value)
    except TypeError:
        parts = []
    parts = (parts + list(default))[:3]
    return "(" + ", ".join(_num(part) for part in parts) + ")"


def _choice(value: Any, allowed: frozenset, default: str) -> str:
    """One of a fixed set, or the default. Never the caller's string.

    Matched without regard to case and returned in the allowlist's own
    casing, because the lists are not all the same shape: modifier
    types and brushes are Blender enums and shout (SUBSURF, DRAW),
    while keyframe data paths are attribute names and do not
    (location, rotation_euler).

    An earlier version upper-cased the candidate before comparing,
    which meant no keyframe path ever matched and every request
    silently became the default. `insert_keyframe` therefore keyed
    location whatever it was asked for -- invisible for as long as the
    fallback was silent, which is the argument for it not being.
    """
    if value is None or str(value).strip() == "":
        return repr(default)

    canonical = {str(option).upper(): option for option in allowed}
    match = canonical.get(str(value).strip().upper())
    if match is None:
        raise BadValue(
            f"{value!r} is not one of {', '.join(sorted(allowed))}.")
    return repr(match)


# ======================================================
# The preamble every script carries
# ======================================================

PREAMBLE = '''"""Generated by ARIA Lite. Do not edit -- it is rewritten per run."""
import bpy
import mathutils
import json
import sys

_RESULT = {"created": [], "modified": [], "exported": [], "steps": []}


def _note(step, **fields):
    entry = {"step": step}
    entry.update(fields)
    _RESULT["steps"].append(entry)


def _obj(name):
    """The named object, or a clear error naming what was asked for.

    Blender's own message for a missing key is a KeyError on a dict
    nobody can see, which tells the user nothing about what ARIA was
    trying to do.
    """
    found = bpy.data.objects.get(name)
    if found is None:
        raise RuntimeError(
            "no object called %r -- the scene has: %s"
            % (name, ", ".join(sorted(o.name for o in bpy.data.objects)) or "nothing"))
    return found


def _active(obj):
    """Make one object the active selection, which most operators need.

    The mode_set is guarded because it fails its poll when NOTHING is
    active -- which is the state a scene is in right after a base mesh
    has been appended, since linking an object into a collection does
    not select it.
    """
    if bpy.context.view_layer.objects.active is not None:
        bpy.ops.object.mode_set(mode="OBJECT")
    for other in bpy.context.selected_objects:
        other.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    return obj


def _clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for block in (bpy.data.meshes, bpy.data.materials, bpy.data.armatures):
        for item in list(block):
            if item.users == 0:
                block.remove(item)
'''

EPILOGUE = '''
print({open!r})
print(json.dumps(_RESULT))
print({close!r})
'''


# ======================================================
# Modelling
# ======================================================

def _named(params: Dict[str, Any], fallback: str) -> str:
    return _text(params.get("name") or fallback)


def add_cube(params: Dict[str, Any]) -> str:
    return (f'bpy.ops.mesh.primitive_cube_add(size={_num(params.get("size"), 2.0)}, '
            f'location={_vector(params.get("location"))})\n'
            f'bpy.context.active_object.name = {_named(params, "Cube")}\n'
            f'_RESULT["created"].append(bpy.context.active_object.name)\n'
            f'_note("add_cube", name=bpy.context.active_object.name)')


def add_sphere(params: Dict[str, Any]) -> str:
    return (f'bpy.ops.mesh.primitive_uv_sphere_add('
            f'radius={_num(params.get("radius"), 1.0)}, '
            f'location={_vector(params.get("location"))})\n'
            f'bpy.context.active_object.name = {_named(params, "Sphere")}\n'
            f'bpy.ops.object.shade_smooth()\n'
            f'_RESULT["created"].append(bpy.context.active_object.name)\n'
            f'_note("add_sphere", name=bpy.context.active_object.name)')


def add_cylinder(params: Dict[str, Any]) -> str:
    return (f'bpy.ops.mesh.primitive_cylinder_add('
            f'radius={_num(params.get("radius"), 1.0)}, '
            f'depth={_num(params.get("depth"), 2.0)}, '
            f'location={_vector(params.get("location"))})\n'
            f'bpy.context.active_object.name = {_named(params, "Cylinder")}\n'
            f'_RESULT["created"].append(bpy.context.active_object.name)\n'
            f'_note("add_cylinder", name=bpy.context.active_object.name)')


def add_plane(params: Dict[str, Any]) -> str:
    return (f'bpy.ops.mesh.primitive_plane_add(size={_num(params.get("size"), 2.0)}, '
            f'location={_vector(params.get("location"))})\n'
            f'bpy.context.active_object.name = {_named(params, "Plane")}\n'
            f'_RESULT["created"].append(bpy.context.active_object.name)\n'
            f'_note("add_plane", name=bpy.context.active_object.name)')


def add_torus(params: Dict[str, Any]) -> str:
    return (f'bpy.ops.mesh.primitive_torus_add('
            f'major_radius={_num(params.get("major_radius"), 1.0)}, '
            f'minor_radius={_num(params.get("minor_radius"), 0.25)}, '
            f'location={_vector(params.get("location"))})\n'
            f'bpy.context.active_object.name = {_named(params, "Torus")}\n'
            f'_RESULT["created"].append(bpy.context.active_object.name)\n'
            f'_note("add_torus", name=bpy.context.active_object.name)')


# ======================================================
# Modifiers
#
# Added and left unapplied unless asked. An applied modifier cannot be
# adjusted afterwards, and a caller who wanted two levels of subsurf
# and got it baked into the mesh has lost something they cannot get
# back without undoing the whole run.
# ======================================================

def _modifier(params: Dict[str, Any], kind: str, settings: str,
              label: str) -> str:
    obj = _text(params.get("object") or params.get("obj"))
    apply_it = bool(params.get("apply"))
    lines = [
        f'_target = _active(_obj({obj}))',
        f'_mod = _target.modifiers.new(name={_text(label)}, type={_choice(kind, MODIFIER_TYPES, "SUBSURF")})',
        settings,
        f'_RESULT["modified"].append(_target.name)',
        f'_note({_text(label)}, object=_target.name)',
    ]
    if apply_it:
        lines.append('bpy.ops.object.modifier_apply(modifier=_mod.name)')
    return "\n".join(line for line in lines if line)


def apply_subdivision(params: Dict[str, Any]) -> str:
    return _modifier(params, "SUBSURF",
                     f'_mod.levels = {_int(params.get("levels"), 2, 0, 6)}\n'
                     f'_mod.render_levels = {_int(params.get("levels"), 2, 0, 6)}',
                     "subdivision")


def apply_bevel(params: Dict[str, Any]) -> str:
    return _modifier(params, "BEVEL",
                     f'_mod.width = {_num(params.get("amount"), 0.02)}\n'
                     f'_mod.segments = {_int(params.get("segments"), 2, 1, 32)}',
                     "bevel")


def apply_mirror(params: Dict[str, Any]) -> str:
    axis = str(params.get("axis") or "X").strip().upper()
    index = {"X": 0, "Y": 1, "Z": 2}.get(axis, 0)
    return _modifier(params, "MIRROR",
                     f'_mod.use_axis = (False, False, False)\n'
                     f'_mod.use_axis[{index}] = True',
                     "mirror")


def apply_array(params: Dict[str, Any]) -> str:
    return _modifier(params, "ARRAY",
                     f'_mod.count = {_int(params.get("count"), 3, 1, 512)}\n'
                     f'_mod.relative_offset_displace = {_vector(params.get("offset"), (1.0, 0.0, 0.0))}',
                     "array")


def apply_boolean(params: Dict[str, Any]) -> str:
    return _modifier(params, "BOOLEAN",
                     f'_mod.object = _obj({_text(params.get("target"))})\n'
                     f'_mod.operation = {_choice(params.get("operation"), BOOLEAN_OPERATIONS, "DIFFERENCE")}',
                     "boolean")


def apply_solidify(params: Dict[str, Any]) -> str:
    return _modifier(params, "SOLIDIFY",
                     f'_mod.thickness = {_num(params.get("thickness"), 0.05)}',
                     "solidify")


def apply_decimate(params: Dict[str, Any]) -> str:
    return _modifier(params, "DECIMATE",
                     f'_mod.ratio = {_num(params.get("ratio"), 0.5)}',
                     "decimate")


# ======================================================
# Transforms
# ======================================================

def move(params: Dict[str, Any]) -> str:
    return (f'_target = _obj({_text(params.get("object") or params.get("obj"))})\n'
            f'_target.location = {_vector([params.get("x"), params.get("y"), params.get("z")])}\n'
            f'_note("move", object=_target.name)')


def rotate(params: Dict[str, Any]) -> str:
    """Degrees in, radians out. Blender stores radians and nobody says
    "rotate it by 1.57"."""
    return (f'import math\n'
            f'_target = _obj({_text(params.get("object") or params.get("obj"))})\n'
            f'_target.rotation_euler = tuple(math.radians(a) for a in '
            f'{_vector([params.get("x"), params.get("y"), params.get("z")])})\n'
            f'_note("rotate", object=_target.name)')


def scale(params: Dict[str, Any]) -> str:
    return (f'_target = _obj({_text(params.get("object") or params.get("obj"))})\n'
            f'_target.scale = {_vector([params.get("x"), params.get("y"), params.get("z")], (1.0, 1.0, 1.0))}\n'
            f'_note("scale", object=_target.name)')


def parent(params: Dict[str, Any]) -> str:
    return (f'_child = _obj({_text(params.get("child"))})\n'
            f'_parent = _obj({_text(params.get("parent"))})\n'
            f'_child.parent = _parent\n'
            f'_child.matrix_parent_inverse = _parent.matrix_world.inverted()\n'
            f'_note("parent", child=_child.name, parent=_parent.name)')


# ======================================================
# UV
# ======================================================

def smart_uv_project(params: Dict[str, Any]) -> str:
    return (f'_target = _active(_obj({_text(params.get("object") or params.get("obj"))}))\n'
            f'bpy.ops.object.mode_set(mode="EDIT")\n'
            f'bpy.ops.mesh.select_all(action="SELECT")\n'
            f'bpy.ops.uv.smart_project(angle_limit={_num(params.get("angle_limit"), 1.15)}, '
            f'island_margin={_num(params.get("margin"), 0.02)})\n'
            f'bpy.ops.object.mode_set(mode="OBJECT")\n'
            f'_note("smart_uv_project", object=_target.name)')


def mark_seams(params: Dict[str, Any]) -> str:
    """Seams on the sharp edges, which is what a person means by "mark
    seams" without pointing at any."""
    return (f'_target = _active(_obj({_text(params.get("object") or params.get("obj"))}))\n'
            f'bpy.ops.object.mode_set(mode="EDIT")\n'
            f'bpy.ops.mesh.select_all(action="DESELECT")\n'
            f'bpy.ops.mesh.select_mode(type="EDGE")\n'
            f'bpy.ops.mesh.edges_select_sharp(sharpness={_num(params.get("sharpness"), 0.9)})\n'
            f'bpy.ops.mesh.mark_seam(clear=False)\n'
            f'bpy.ops.object.mode_set(mode="OBJECT")\n'
            f'_note("mark_seams", object=_target.name)')


def unwrap(params: Dict[str, Any]) -> str:
    return (f'_target = _active(_obj({_text(params.get("object") or params.get("obj"))}))\n'
            f'bpy.ops.object.mode_set(mode="EDIT")\n'
            f'bpy.ops.mesh.select_all(action="SELECT")\n'
            f'bpy.ops.uv.unwrap(method="ANGLE_BASED", margin={_num(params.get("margin"), 0.02)})\n'
            f'bpy.ops.object.mode_set(mode="OBJECT")\n'
            f'_note("unwrap", object=_target.name)')


# ======================================================
# Materials
# ======================================================

def create_material(params: Dict[str, Any]) -> str:
    """Built through the node tree, because Principled BSDF is where
    metallic and roughness actually live in 5.0 -- material.metallic
    does not exist.

    use_nodes is never touched. Measured on 5.0: a new material
    already arrives with use_nodes True and a Principled BSDF in its
    tree, so setting it is redundant -- and the property is deprecated
    ("expected to be removed in Blender 6.0"), which it warns about on
    READ as well as on write. Guarding the write with `if not
    _mat.use_nodes` therefore still produced the warning it was meant
    to avoid. Going through node_tree directly touches nothing
    deprecated.
    """
    colour = list(params.get("color") or (0.8, 0.8, 0.8))
    colour = (colour + [0.8, 0.8, 0.8])[:3] + [1.0]
    return (f'_mat = bpy.data.materials.new(name={_text(params.get("name") or "Material")})\n'
            f'_tree = getattr(_mat, "node_tree", None)\n'
            f'_bsdf = _tree.nodes.get("Principled BSDF") if _tree else None\n'
            f'if _bsdf is not None:\n'
            f'    _bsdf.inputs["Base Color"].default_value = ('
            f'{_num(colour[0])}, {_num(colour[1])}, {_num(colour[2])}, 1.0)\n'
            f'    _bsdf.inputs["Metallic"].default_value = {_num(params.get("metallic"), 0.0)}\n'
            f'    _bsdf.inputs["Roughness"].default_value = {_num(params.get("roughness"), 0.5)}\n'
            f'_note("create_material", name=_mat.name)')


def assign_material(params: Dict[str, Any]) -> str:
    return (f'_target = _obj({_text(params.get("object") or params.get("obj"))})\n'
            f'_mat = bpy.data.materials.get({_text(params.get("material"))})\n'
            f'if _mat is None:\n'
            f'    raise RuntimeError("no material called %r" % {_text(params.get("material"))})\n'
            f'if _target.data.materials:\n'
            f'    _target.data.materials[0] = _mat\n'
            f'else:\n'
            f'    _target.data.materials.append(_mat)\n'
            f'_note("assign_material", object=_target.name, material=_mat.name)')


# ======================================================
# Rigging
# ======================================================

def create_armature(params: Dict[str, Any]) -> str:
    """An empty armature, ready for add_bone.

    EMPTY IS THE POINT. `armature_add` does not make a bare armature --
    it makes one carrying a default bone called "Bone", and nothing used
    to take it away, so every rig built through here has had a spare
    bone standing in it at the origin. That is not cosmetic once
    auto_weights runs: the stray takes vertex weights off the bones that
    should have had them, and drags that part of the mesh with it.
    Measured in Blender 5.0.1, along with the missing parents below.
    """
    return (f'bpy.ops.object.armature_add(location={_vector(params.get("location"))})\n'
            f'_arm = bpy.context.active_object\n'
            f'_arm.name = {_named(params, "Armature")}\n'
            f'bpy.ops.object.mode_set(mode="EDIT")\n'
            f'for _spare in list(_arm.data.edit_bones):\n'
            f'    _arm.data.edit_bones.remove(_spare)\n'
            f'bpy.ops.object.mode_set(mode="OBJECT")\n'
            f'_RESULT["created"].append(_arm.name)\n'
            f'_note("create_armature", name=_arm.name)')


def add_bone(params: Dict[str, Any]) -> str:
    """Bones are created through edit_bones, not an operator.

    armature.bone_primitive_add exists but names the bone itself and
    places it at the origin, so building a named skeleton means the
    data API either way.

    PARENT IS WHAT MAKES IT A SKELETON
    ----------------------------------
    A bone with no parent moves alone. Without this a rig was a pile of
    bones that happened to be touching: turn the shoulder and the
    forearm stays hanging in the air where it started. It was invisible
    because posing ONE bone looks perfectly right, and it is only when a
    limb has to swing that the rig turns out never to have been one.
    Measured in Blender 5.0.1 -- every bone came back with parent None.

    `connect` welds the child's head onto the parent's tail, which is
    what you want along a limb. Leave it off where a bone hangs from a
    joint without being in line with it, like a hip off a spine.
    """
    return (f'_arm = _active(_obj({_text(params.get("armature"))}))\n'
            f'bpy.ops.object.mode_set(mode="EDIT")\n'
            f'_bone = _arm.data.edit_bones.new({_text(params.get("name") or "Bone")})\n'
            f'_bone.head = {_vector(params.get("head"))}\n'
            f'_bone.tail = {_vector(params.get("tail"), (0.0, 0.0, 1.0))}\n'
            f'_parent = {_text(params.get("parent"))}\n'
            f'if _parent:\n'
            f'    _found = _arm.data.edit_bones.get(_parent)\n'
            f'    if _found is None:\n'
            f'        raise RuntimeError("no bone called %r to hang %r from -- '
            f'the rig has: %s" % (_parent, {_text(params.get("name") or "Bone")}, '
            f'", ".join(sorted(_b.name for _b in _arm.data.edit_bones))))\n'
            f'    _bone.parent = _found\n'
            f'    _bone.use_connect = {"True" if params.get("connect") else "False"}\n'
            f'bpy.ops.object.mode_set(mode="OBJECT")\n'
            f'_note("add_bone", armature=_arm.name, '
            f'bone={_text(params.get("name") or "Bone")}, parent=_parent or None)')


def parent_mesh_to_armature(params: Dict[str, Any]) -> str:
    return (f'_mesh = _obj({_text(params.get("mesh"))})\n'
            f'_arm = _obj({_text(params.get("armature"))})\n'
            f'bpy.ops.object.mode_set(mode="OBJECT")\n'
            f'for _o in bpy.context.selected_objects:\n'
            f'    _o.select_set(False)\n'
            f'_mesh.select_set(True)\n'
            f'_arm.select_set(True)\n'
            f'bpy.context.view_layer.objects.active = _arm\n'
            f'bpy.ops.object.parent_set(type="ARMATURE_NAME")\n'
            f'_note("parent_mesh_to_armature", mesh=_mesh.name, armature=_arm.name)')


def auto_weights(params: Dict[str, Any]) -> str:
    """ARMATURE_AUTO is the "with automatic weights" of the UI menu."""
    return (f'_mesh = _obj({_text(params.get("mesh"))})\n'
            f'_arm = _obj({_text(params.get("armature"))})\n'
            f'bpy.ops.object.mode_set(mode="OBJECT")\n'
            f'for _o in bpy.context.selected_objects:\n'
            f'    _o.select_set(False)\n'
            f'_mesh.select_set(True)\n'
            f'_arm.select_set(True)\n'
            f'bpy.context.view_layer.objects.active = _arm\n'
            f'bpy.ops.object.parent_set(type="ARMATURE_AUTO")\n'
            f'_note("auto_weights", mesh=_mesh.name, armature=_arm.name)')


# ======================================================
# Skinning
# ======================================================

def normalize_weights(params: Dict[str, Any]) -> str:
    return (f'_target = _active(_obj({_text(params.get("mesh") or params.get("object"))}))\n'
            f'if _target.vertex_groups:\n'
            f'    bpy.ops.object.mode_set(mode="WEIGHT_PAINT")\n'
            f'    bpy.ops.object.vertex_group_normalize_all(lock_active=False)\n'
            f'    bpy.ops.object.mode_set(mode="OBJECT")\n'
            f'_note("normalize_weights", object=_target.name)')


def assign_vertex_group(params: Dict[str, Any]) -> str:
    vertices = params.get("vertices") or []
    try:
        indices = [int(v) for v in vertices]
    except (TypeError, ValueError):
        indices = []
    return (f'_target = _obj({_text(params.get("mesh") or params.get("object"))})\n'
            f'_group = _target.vertex_groups.get({_text(params.get("bone"))}) or '
            f'_target.vertex_groups.new(name={_text(params.get("bone"))})\n'
            f'_group.add({indices!r}, {_num(params.get("weight"), 1.0)}, "REPLACE")\n'
            f'_note("assign_vertex_group", object=_target.name, group=_group.name, '
            f'count={len(indices)})')


# ======================================================
# Animation
# ======================================================

def set_frame(params: Dict[str, Any]) -> str:
    return (f'bpy.context.scene.frame_set({_int(params.get("frame"), 1, 0, 1_000_000)})\n'
            f'_note("set_frame", frame={_int(params.get("frame"), 1, 0, 1_000_000)})')


def insert_keyframe(params: Dict[str, Any]) -> str:
    """The data API, not the operator.

    bpy.ops.anim.keyframe_insert works on whatever the UI has selected
    and needs a context this script does not have; obj.keyframe_insert
    names its target and works headless.
    """
    return (f'_target = _obj({_text(params.get("object") or params.get("obj"))})\n'
            f'bpy.context.scene.frame_set({_int(params.get("frame"), 1, 0, 1_000_000)})\n'
            f'_target.keyframe_insert(data_path={_choice(params.get("property"), KEYFRAME_PATHS, "location")}, '
            f'frame={_int(params.get("frame"), 1, 0, 1_000_000)})\n'
            f'_note("insert_keyframe", object=_target.name, frame={_int(params.get("frame"), 1, 0, 1_000_000)})')


def set_pose(params: Dict[str, Any]) -> str:
    """Turn one bone, on one frame, and key it there.

    THE FRAME IS SET FIRST, AND THAT IS THE WHOLE FUNCTION
    -----------------------------------------------------
    It used to pose the bone and then call frame_set, which reads as
    the obvious order and is exactly backwards: frame_set re-evaluates
    the animation, so it OVERWRITES the rotation just assigned with
    whatever the existing curve says at that frame, and the keyframe
    records that instead.

    The first key on a bone survived, because a bone with no curve has
    nothing to be overwritten from. Every key after it recorded the
    first one's value again. So an animation came out as twenty-four
    frames of the same pose -- a rig that held still, with a full set of
    keyframes to prove it had been animated.

    Found by building the crew miner's swing and measuring where the
    pick head was on each frame: identical, to the millimetre, on all
    six sampled frames.
    """
    return (f'import math\n'
            f'_arm = _active(_obj({_text(params.get("armature"))}))\n'
            f'bpy.ops.object.mode_set(mode="POSE")\n'
            f'_pbone = _arm.pose.bones.get({_text(params.get("bone"))})\n'
            f'if _pbone is None:\n'
            f'    raise RuntimeError("no bone called %r -- the rig has: %s" % ('
            f'{_text(params.get("bone"))}, ", ".join(sorted(b.name for b in _arm.pose.bones))))\n'
            f'_frame = {_int(params.get("frame"), 1, 0, 1_000_000)}\n'
            f'bpy.context.scene.frame_set(_frame)\n'
            f'_pbone.rotation_mode = "XYZ"\n'
            f'_pbone.rotation_euler = tuple(math.radians(a) for a in '
            f'{_vector(params.get("rotation"))})\n'
            f'_pbone.keyframe_insert(data_path="rotation_euler", frame=_frame)\n'
            f'bpy.ops.object.mode_set(mode="OBJECT")\n'
            f'_note("set_pose", armature=_arm.name, bone={_text(params.get("bone"))}, '
            f'frame=_frame)')


def bake_animation(params: Dict[str, Any]) -> str:
    start = _int(params.get("start"), 1, 0, 1_000_000)
    end = _int(params.get("end"), 60, 0, 1_000_000)
    return (f'_target = _active(_obj({_text(params.get("object") or params.get("obj"))}))\n'
            f'bpy.context.scene.frame_start = {start}\n'
            f'bpy.context.scene.frame_end = {end}\n'
            f'if _target.type == "ARMATURE":\n'
            f'    bpy.ops.object.mode_set(mode="POSE")\n'
            f'    bpy.ops.pose.select_all(action="SELECT")\n'
            f'    bpy.ops.nla.bake(frame_start={start}, frame_end={end}, '
            f'only_selected=True, visual_keying=True, bake_types={{"POSE"}})\n'
            f'    bpy.ops.object.mode_set(mode="OBJECT")\n'
            f'else:\n'
            f'    bpy.ops.nla.bake(frame_start={start}, frame_end={end}, '
            f'only_selected=True, visual_keying=True, bake_types={{"OBJECT"}})\n'
            f'_note("bake_animation", object=_target.name, start={start}, end={end})')


# ======================================================
# Sculpting
# ======================================================

def enable_dyntopo(params: Dict[str, Any]) -> str:
    return (f'_target = _active(_obj({_text(params.get("object") or params.get("obj"))}))\n'
            f'bpy.ops.object.mode_set(mode="SCULPT")\n'
            f'if not bpy.context.sculpt_object.use_dynamic_topology_sculpting:\n'
            f'    bpy.ops.sculpt.dynamic_topology_toggle()\n'
            f'bpy.ops.object.mode_set(mode="OBJECT")\n'
            f'_note("enable_dyntopo", object=_target.name)')


def apply_multires(params: Dict[str, Any]) -> str:
    levels = _int(params.get("levels"), 2, 1, 6)
    return (f'_target = _active(_obj({_text(params.get("object") or params.get("obj"))}))\n'
            f'_mod = _target.modifiers.new(name="multires", type="MULTIRES")\n'
            f'for _ in range({levels}):\n'
            f'    bpy.ops.object.multires_subdivide(modifier=_mod.name, mode="CATMULL_CLARK")\n'
            f'_RESULT["modified"].append(_target.name)\n'
            f'_note("apply_multires", object=_target.name, levels={levels})')


def sculpt_brush(params: Dict[str, Any]) -> str:
    """Put an object into sculpt mode with a brush and a strength.

    WHAT THIS CANNOT DO, STATED PLAINLY
    A stroke needs screen coordinates and a 3D viewport. Blender in
    --background has neither, so nothing here paints, and a template
    that claimed to would be claiming something impossible.

    It also cannot pick a brush at all. Two separate measurements on
    this build:

      * bpy.data.brushes holds exactly one entry, "Draw" -- since 4.3
        the rest are asset-library brushes a headless session never
        loads, so a request for CLAY finds nothing to switch to; and
      * tool_settings.sculpt.brush is READ-ONLY -- assigning it raises
        `AttributeError: bpy_struct: attribute "brush" from "Sculpt"
        is read-only`.

    An earlier version of this template looked the brush up and
    assigned it. That passed its first verification only because the
    test asked for CLAY, which was never found, so the assignment line
    never ran. Asking for DRAW -- a brush that does exist -- executed
    it and the whole run died. A branch that has never executed has
    not been tested.

    So it does not try. The result reports what was asked for, what is
    actually active, and whether those are the same.

    RE-MEASURED 2026-09-20, HARDER, BECAUSE A WHOLE ARCHITECTURE RESTED ON IT
    The plan was: blockout, subdivide, then "sculpting passes" turn a
    cylinder into an arm. Four attempts in Blender 5.0.1 --background,
    each on a subdivided sphere, measuring whether any vertex moved:

      * sculpt.brush_stroke, with 3D locations in the stroke rather
        than only mouse coordinates -- poll() failed, context is
        incorrect. It refuses.
      * the same inside a temp_override with the window manager's own
        window -- refuses identically. There are no windows to borrow.
      * sculpt.mesh_filter INFLATE, a sculpt deform that needs no
        stroke at all -- CRASHED Blender. EXCEPTION_ACCESS_VIOLATION,
        exit 11. Not an exception; a segfault.
      * a displace modifier, as a control -- worked, radial spread
        0.01035 to 0.23698.

    Entering sculpt mode succeeds. Sculpting does not. So anatomy has
    to arrive already sculpted, in a base mesh someone made with a
    viewport open -- which is what aria_models/ is for, and why it is
    not a convenience but a replacement for a step that cannot run.

    What it DOES do is real: enters sculpt mode, sets the strength on
    whichever brush is active, and sets the dyntopo detail size -- the
    parameter that actually changes what later sculpting does.
    """
    return (f'_target = _active(_obj({_text(params.get("object") or params.get("obj"))}))\n'
            f'bpy.ops.object.mode_set(mode="SCULPT")\n'
            f'_wanted = {_choice(params.get("brush_type"), SCULPT_BRUSHES, "DRAW")}\n'
            f'_settings = bpy.context.tool_settings.sculpt\n'
            f'_live = getattr(_settings, "brush", None)\n'
            f'_applied = (_live is not None\n'
            f'            and _live.name.upper().replace(" ", "_") == _wanted)\n'
            f'if _live is not None:\n'
            f'    _live.strength = {_num(params.get("strength"), 0.5)}\n'
            f'if hasattr(_settings, "detail_size"):\n'
            f'    _settings.detail_size = {_num(params.get("detail_size"), 12.0)}\n'
            f'bpy.ops.object.mode_set(mode="OBJECT")\n'
            f'_note("sculpt_brush", object=_target.name, requested=_wanted, '
            f'applied=_applied, '
            f'active=(_live.name if _live else None))')


# Export
# ======================================================

def _export_preamble(params: Dict[str, Any]) -> str:
    """Select what is being exported, or everything."""
    only = params.get("object") or params.get("obj")
    if not only:
        return ('bpy.ops.object.select_all(action="SELECT")\n'
                '_use_selection = False')
    return (f'_target = _active(_obj({_text(only)}))\n'
            f'_use_selection = True')


def export_fbx(params: Dict[str, Any]) -> str:
    """Write the model out as FBX.

    MESHES AND ARMATURES ONLY, AND THAT IS NOT A DETAIL
    Baking a painted light into a texture needs a lamp in the scene,
    and an export that took everything took the lamp too: into the
    FBX, into the prefab, and into the game, where a 3.2 intensity
    Directional Light blew out an entire painted quarry. The model
    looked right and the sky went white, and nothing about the change
    announced that it had touched the lighting at all.

    An FBX from here is a model. Lamps and cameras exist to light and
    frame a bake; they do not travel with it.
    """
    return (f'{_export_preamble(params)}\n'
            f'_path = {_text(params.get("path"))}\n'
            f'bpy.ops.export_scene.fbx(filepath=_path, use_selection=_use_selection, '
            f'object_types={{"MESH", "ARMATURE"}}, '
            f'apply_unit_scale=True, bake_space_transform=False, '
            f'add_leaf_bones=False, path_mode="COPY", embed_textures=True)\n'
            f'_RESULT["exported"].append(_path)\n'
            f'_note("export_fbx", path=_path)')


def export_glb(params: Dict[str, Any]) -> str:
    return (f'{_export_preamble(params)}\n'
            f'_path = {_text(params.get("path"))}\n'
            f'bpy.ops.export_scene.gltf(filepath=_path, export_format="GLB", '
            f'use_selection=_use_selection)\n'
            f'_RESULT["exported"].append(_path)\n'
            f'_note("export_glb", path=_path)')


def export_obj(params: Dict[str, Any]) -> str:
    """wm.obj_export, the current spelling. export_scene.obj still
    exists in 5.0 but is the older one."""
    return (f'{_export_preamble(params)}\n'
            f'_path = {_text(params.get("path"))}\n'
            f'bpy.ops.wm.obj_export(filepath=_path, export_selected_objects=_use_selection)\n'
            f'_RESULT["exported"].append(_path)\n'
            f'_note("export_obj", path=_path)')


# ======================================================
# Scene
# ======================================================

def clear_scene(params: Dict[str, Any]) -> str:
    return '_clear_scene()\n_note("clear_scene")'


def save_file(params: Dict[str, Any]) -> str:
    return (f'_path = {_text(params.get("path"))}\n'
            f'bpy.ops.wm.save_as_mainfile(filepath=_path)\n'
            f'_RESULT["exported"].append(_path)\n'
            f'_note("save_file", path=_path)')


# ======================================================
# Cleaning up what a generator handed us
#
# Written against a measured example rather than a guess: one Ludo
# character, imported and inspected. What that showed, and what each
# of these exists to fix --
#
#   * it arrives 0.994 units tall. Unity wants about 1.7 for a person,
#     so every model needs the same rescale;
#   * its origin is at the model's centre, feet at Z -0.496. Unity
#     places things by their origin, so a character imported as-is
#     stands with its knees through the floor;
#   * 13,542 of its edges look non-manifold and are not. glTF splits
#     vertices at UV seams because the format stores attributes per
#     vertex, so a seam becomes two coincident vertices and every edge
#     along it reads as a boundary. ZERO edges had three or more faces,
#     which is the measure that means real damage;
#   * merging those duplicates halves the vertex count -- 18,442 to
#     9,790 -- and CREATES 727 edges with three or more faces where
#     there were none, because a blanket merge welds surfaces that only
#     happen to touch. So merging is opt-in and it is checked.
# ======================================================

_IMPORT_FORMATS = frozenset({"glb", "gltf", "fbx", "obj", "blend"})


def import_model(params: Dict[str, Any]) -> str:
    """Bring a file in. The format comes from the extension.

    Ludo returns .glb, Unity wants .fbx and a person may hand over
    either, so the caller should not have to say which it is.
    """
    return (f'_path = {_text(params.get("path"))}\n'
            f'_before = set(bpy.data.objects)\n'
            f'_kind = _path.rsplit(".", 1)[-1].lower() if "." in _path else ""\n'
            f'if _kind in ("glb", "gltf"):\n'
            f'    bpy.ops.import_scene.gltf(filepath=_path)\n'
            f'elif _kind == "fbx":\n'
            f'    bpy.ops.import_scene.fbx(filepath=_path)\n'
            f'elif _kind == "obj":\n'
            f'    bpy.ops.wm.obj_import(filepath=_path)\n'
            f'else:\n'
            f'    raise RuntimeError("I do not know how to import %r -- I read "\n'
            f'                       "glb, gltf, fbx and obj." % _path)\n'
            f'_added = [o.name for o in bpy.data.objects if o not in _before]\n'
            f'_RESULT["created"].extend(_added)\n'
            f'_note("import_model", path=_path, objects=_added)')


def measure_mesh(params: Dict[str, Any]) -> str:
    """Report what is there. Changes nothing.

    This is the gate between pipeline stages: a number here is what
    lets a later step refuse to spend minutes on something already
    broken. It is also the only honest way to say what a cleanup did,
    since the same call runs before and after.
    """
    return (
        'import bmesh\n'
        'from mathutils import Vector\n'
        '_meshes = [o for o in bpy.data.objects if o.type == "MESH"]\n'
        'if not _meshes:\n'
        '    raise RuntimeError("there is no mesh in the scene to measure")\n'
        '_verts = _faces = _tris = _ngons = 0\n'
        '_boundary = _broken = _loose = 0\n'
        '_uv_layers = 0\n'
        'for _m in _meshes:\n'
        '    _data = _m.data\n'
        '    _verts += len(_data.vertices)\n'
        '    _faces += len(_data.polygons)\n'
        '    _tris += sum(len(_p.vertices) - 2 for _p in _data.polygons)\n'
        '    _ngons += sum(1 for _p in _data.polygons if len(_p.vertices) > 4)\n'
        '    _uv_layers = max(_uv_layers, len(_data.uv_layers))\n'
        '    _bm = bmesh.new()\n'
        '    _bm.from_mesh(_data)\n'
        '    _boundary += sum(1 for _e in _bm.edges if len(_e.link_faces) == 1)\n'
        '    _broken += sum(1 for _e in _bm.edges if len(_e.link_faces) > 2)\n'
        '    _loose += sum(1 for _v in _bm.verts if not _v.link_edges)\n'
        '    _bm.free()\n'
        '_lo = Vector((1e18, 1e18, 1e18))\n'
        '_hi = Vector((-1e18, -1e18, -1e18))\n'
        'for _m in _meshes:\n'
        '    for _corner in _m.bound_box:\n'
        '        _w = _m.matrix_world @ Vector(_corner)\n'
        '        for _a in range(3):\n'
        '            _lo[_a] = min(_lo[_a], _w[_a])\n'
        '            _hi[_a] = max(_hi[_a], _w[_a])\n'
        '_size = _hi - _lo\n'
        '_measurement = {\n'
        '    "meshes": len(_meshes),\n'
        '    "armatures": len([o for o in bpy.data.objects if o.type == "ARMATURE"]),\n'
        '    "vertices": _verts, "faces": _faces, "triangles": _tris,\n'
        '    "ngons": _ngons, "uv_layers": _uv_layers,\n'
        '    "boundary_edges": _boundary, "broken_edges": _broken,\n'
        '    "loose_vertices": _loose,\n'
        '    "materials": len([m for m in bpy.data.materials]),\n'
        '    "images": len([i for i in bpy.data.images if i.name != "Render Result"]),\n'
        '    "width": round(_size.x, 4), "depth": round(_size.y, 4),\n'
        '    "height": round(_size.z, 4),\n'
        '    "floor": round(_lo.z, 4),\n'
        '}\n'
        '_RESULT.setdefault("measurements", []).append(_measurement)\n'
        '_note("measure_mesh", **_measurement)')


def remove_loose(params: Dict[str, Any]) -> str:
    """Vertices and edges attached to no face.

    They export, they take up room, and they are invisible -- so they
    are found by a person only when something downstream trips on them.
    """
    return (
        'import bmesh\n'
        '_removed = 0\n'
        'for _m in [o for o in bpy.data.objects if o.type == "MESH"]:\n'
        '    _bm = bmesh.new()\n'
        '    _bm.from_mesh(_m.data)\n'
        '    _junk = [_v for _v in _bm.verts if not _v.link_faces]\n'
        '    _removed += len(_junk)\n'
        '    for _v in _junk:\n'
        '        _bm.verts.remove(_v)\n'
        '    _bm.to_mesh(_m.data)\n'
        '    _bm.free()\n'
        '    _m.data.update()\n'
        '_note("remove_loose", removed=_removed)')


def recalculate_normals(params: Dict[str, Any]) -> str:
    """Point every face outwards.

    A generated mesh with inward faces renders as holes in Unity and
    looks like missing geometry rather than a normals problem, which
    is a bad afternoon for whoever has to work it out.
    """
    return (
        'import bmesh\n'
        'for _m in [o for o in bpy.data.objects if o.type == "MESH"]:\n'
        '    _bm = bmesh.new()\n'
        '    _bm.from_mesh(_m.data)\n'
        '    bmesh.ops.recalc_face_normals(_bm, faces=_bm.faces)\n'
        '    _bm.to_mesh(_m.data)\n'
        '    _bm.free()\n'
        '    _m.data.update()\n'
        '_note("recalculate_normals", meshes=len([o for o in bpy.data.objects '
        'if o.type == "MESH"]))')


def merge_by_distance(params: Dict[str, Any]) -> str:
    """Weld coincident vertices -- and put it back if that broke the mesh.

    MEASURED, NOT ASSUMED. On the probe model this halves the vertex
    count, 18,442 to 9,790, which is a real saving. It also creates 727
    edges with three or more faces where there were none, because a
    blanket weld joins surfaces that merely touch.

    So the merge is done on a copy of the mesh data, the damage is
    counted, and a merge that makes the mesh worse is discarded and
    reported rather than kept and hidden. A cleanup step that quietly
    breaks the thing it cleaned is worse than no cleanup step.
    """
    return (
        f'import bmesh\n'
        f'_distance = {_num(params.get("distance"), 0.0001)}\n'
        f'_allow_damage = bool({params.get("allow_damage", False)!r})\n'
        f'_merged = _kept = _reverted = 0\n'
        f'for _m in [o for o in bpy.data.objects if o.type == "MESH"]:\n'
        f'    _bm = bmesh.new()\n'
        f'    _bm.from_mesh(_m.data)\n'
        f'    _was_broken = sum(1 for _e in _bm.edges if len(_e.link_faces) > 2)\n'
        f'    _was_verts = len(_bm.verts)\n'
        f'    bmesh.ops.remove_doubles(_bm, verts=list(_bm.verts), dist=_distance)\n'
        f'    _now_broken = sum(1 for _e in _bm.edges if len(_e.link_faces) > 2)\n'
        f'    if _now_broken > _was_broken and not _allow_damage:\n'
        f'        _reverted += _now_broken - _was_broken\n'
        f'        _bm.free()\n'
        f'        continue\n'
        f'    _merged += _was_verts - len(_bm.verts)\n'
        f'    _kept += 1\n'
        f'    _bm.to_mesh(_m.data)\n'
        f'    _bm.free()\n'
        f'    _m.data.update()\n'
        f'_note("merge_by_distance", merged=_merged, meshes_changed=_kept, '
        f'reverted_edges=_reverted, distance=_distance)')


def scale_to_height(params: Dict[str, Any]) -> str:
    """Resize everything together so the tallest point reaches a height.

    Ludo normalises its models to about one unit whatever they are, so
    a character arrives half the size of a person. Unity treats one
    unit as one metre, so without this every import is hand-resized to
    the same number every time.

    Scaled about the world origin as one group, so a model made of
    several objects keeps its proportions and its parts stay together.
    """
    return (
        f'from mathutils import Vector\n'
        f'_target = {_num(params.get("height"), 1.8)}\n'
        f'if _target <= 0:\n'
        f'    raise RuntimeError("a target height of %s is not a height" % _target)\n'
        f'_movable = [o for o in bpy.data.objects if o.parent is None]\n'
        f'_meshes = [o for o in bpy.data.objects if o.type == "MESH"]\n'
        f'if not _meshes:\n'
        f'    raise RuntimeError("there is no mesh in the scene to scale")\n'
        f'_lo = Vector((1e18, 1e18, 1e18))\n'
        f'_hi = Vector((-1e18, -1e18, -1e18))\n'
        f'for _m in _meshes:\n'
        f'    for _corner in _m.bound_box:\n'
        f'        _w = _m.matrix_world @ Vector(_corner)\n'
        f'        for _a in range(3):\n'
        f'            _lo[_a] = min(_lo[_a], _w[_a])\n'
        f'            _hi[_a] = max(_hi[_a], _w[_a])\n'
        f'_was = (_hi - _lo).z\n'
        f'if _was <= 0.000001:\n'
        f'    raise RuntimeError("the model has no height to scale")\n'
        f'_factor = _target / _was\n'
        f'for _o in _movable:\n'
        f'    _o.scale = tuple(_v * _factor for _v in _o.scale)\n'
        f'    _o.location = tuple(_v * _factor for _v in _o.location)\n'
        f'bpy.context.view_layer.update()\n'
        f'_note("scale_to_height", was=round(_was, 4), now=round(_target, 4), '
        f'factor=round(_factor, 4))')


def origin_to_floor(params: Dict[str, Any]) -> str:
    """Put the origin under the model's feet, centred.

    Unity places an object by its origin. Ludo's sits at the centre of
    the model, so dropping one into a scene at ground level buries it
    to the waist -- and the fix, done by hand, is the same offset every
    single time.
    """
    return (
        'from mathutils import Vector\n'
        '_meshes = [o for o in bpy.data.objects if o.type == "MESH"]\n'
        'if not _meshes:\n'
        '    raise RuntimeError("there is no mesh in the scene to re-origin")\n'
        '_lo = Vector((1e18, 1e18, 1e18))\n'
        '_hi = Vector((-1e18, -1e18, -1e18))\n'
        'for _m in _meshes:\n'
        '    for _corner in _m.bound_box:\n'
        '        _w = _m.matrix_world @ Vector(_corner)\n'
        '        for _a in range(3):\n'
        '            _lo[_a] = min(_lo[_a], _w[_a])\n'
        '            _hi[_a] = max(_hi[_a], _w[_a])\n'
        '_shift = Vector(((_lo.x + _hi.x) / 2.0, (_lo.y + _hi.y) / 2.0, _lo.z))\n'
        'for _o in [o for o in bpy.data.objects if o.parent is None]:\n'
        '    _o.location = _o.location - _shift\n'
        'bpy.context.view_layer.update()\n'
        '_note("origin_to_floor", moved=(round(-_shift.x, 4), '
        'round(-_shift.y, 4), round(-_shift.z, 4)))')


# ======================================================
# Rigs
#
# A rigged model is not a harder static model, it is a different
# object, and two things measured on a real one say why.
#
# clean_for_unity DOES preserve a rig -- tested against a two-bone
# armature with automatic weights: bones 2 -> 2, vertex groups 2 -> 2,
# all 24 weighted vertices still weighted, ARMATURE modifier still
# bound. That was worth knowing rather than assuming.
#
# What it leaves behind is a scaled armature. scale_to_height
# multiplies object scale, so the root came out at 0.900 instead of
# 1.0, and a skinned root at a scale other than one is a well known
# source of trouble when Unity maps a humanoid avatar and retargets
# animation onto it. Hence apply_transforms.
# ======================================================

def apply_transforms(params: Dict[str, Any]) -> str:
    """Bake object scale and rotation into the data.

    For a rigged character this is not tidiness. Unity reads the
    armature root's transform when it builds an avatar, and a root left
    at 0.9 makes every retargeted animation subtly wrong in a way that
    is very hard to trace back to an export step.

    The armature and everything skinned to it are applied together --
    applying to one and not the other would separate a mesh from the
    skeleton that deforms it.
    """
    return (
        'bpy.ops.object.mode_set(mode="OBJECT")\n'
        'bpy.ops.object.select_all(action="DESELECT")\n'
        '_applied = []\n'
        'for _o in bpy.data.objects:\n'
        '    if _o.type in ("MESH", "ARMATURE"):\n'
        '        _o.select_set(True)\n'
        '        _applied.append(_o.name)\n'
        'if _applied:\n'
        '    bpy.context.view_layer.objects.active = '
        'bpy.data.objects[_applied[0]]\n'
        '    bpy.ops.object.transform_apply(location=False, rotation=True, '
        'scale=True)\n'
        '_note("apply_transforms", objects=_applied)')


def remove_stray_meshes(params: Dict[str, Any]) -> str:
    """Drop meshes that are in the file but are not the character.

    MEASURED ON A REAL LUDO RIG. Its rigged GLB comes back with a
    42-vertex Icosphere alongside the character: no vertex groups, no
    armature modifier, no parent, and 46 users -- one per bone plus
    one, because it is the custom display shape Blender gives bones.
    It is a viewport nicety inside Blender and it is real geometry the
    moment it reaches FBX, so Unity imports it as a stray sphere
    floating in the prefab.

    THE RULE IS NARROW ON PURPOSE. A mesh is removed only when the file
    has an armature, at least one OTHER mesh is properly bound to it,
    and this one has no parent, no vertex groups and no armature
    modifier. That is "this file is a rigged character and this mesh is
    not part of it" -- not "delete anything unparented", which would
    take a separate prop somebody meant to keep.

    Whatever goes is named in the result. A cleanup that silently
    deletes geometry is one nobody can trust with the geometry they
    care about.
    """
    return (
        '_rigs = [o for o in bpy.data.objects if o.type == "ARMATURE"]\n'
        '_meshes = [o for o in bpy.data.objects if o.type == "MESH"]\n'
        '_bound = [_m for _m in _meshes\n'
        '          if any(_mod.type == "ARMATURE" and _mod.object\n'
        '                 for _mod in _m.modifiers)]\n'
        '_dropped = []\n'
        'if _rigs and _bound:\n'
        '    for _m in list(_meshes):\n'
        '        if _m in _bound:\n'
        '            continue\n'
        '        if _m.parent is not None:\n'
        '            continue\n'
        '        if len(_m.vertex_groups):\n'
        '            continue\n'
        '        _dropped.append(_m.name)\n'
        '        bpy.data.objects.remove(_m, do_unlink=True)\n'
        '_note("remove_stray_meshes", removed=_dropped)')


def measure_rig(params: Dict[str, Any]) -> str:
    """Report the skeleton, and how much of the mesh it actually moves.

    Bone COUNT alone is not enough. A rig can survive an export with
    every bone intact and no weights bound to any of them, and the
    result looks correct in the outliner and does nothing when
    animated. So the weighted-vertex count and the modifier binding
    are measured too -- those are what make a rig a rig.
    """
    return (
        '_rigs = [o for o in bpy.data.objects if o.type == "ARMATURE"]\n'
        '_skins = [o for o in bpy.data.objects if o.type == "MESH"]\n'
        '_bones = sum(len(_r.data.bones) for _r in _rigs)\n'
        '_bone_names = sorted(_b.name for _r in _rigs for _b in _r.data.bones)\n'
        '_groups = sum(len(_m.vertex_groups) for _m in _skins)\n'
        '_weighted = sum(1 for _m in _skins for _v in _m.data.vertices '
        'if _v.groups)\n'
        '_vertices = sum(len(_m.data.vertices) for _m in _skins)\n'
        '_bound = [_m.name for _m in _skins for _mod in _m.modifiers\n'
        '          if _mod.type == "ARMATURE" and _mod.object is not None]\n'
        '_scales = [round(max(abs(_v) for _v in _r.scale), 4) for _r in _rigs]\n'
        '_rig = {\n'
        '    "armatures": len(_rigs), "bones": _bones,\n'
        '    "bone_names": _bone_names[:40],\n'
        '    "vertex_groups": _groups, "weighted_vertices": _weighted,\n'
        '    "vertices": _vertices, "bound_meshes": _bound,\n'
        '    "root_scales": _scales,\n'
        '}\n'
        '_RESULT.setdefault("rigs", []).append(_rig)\n'
        '_note("measure_rig", **_rig)')


def add_ik_constraint(params: Dict[str, Any]) -> str:
    """Make a limb follow a target instead of being aimed a bone at a time.

    Everything else here is FK: you turn each bone and the hand ends up
    wherever the arithmetic puts it. That is fine for a wave and wrong
    for anything a character has to HOLD. A two-handed pickaxe swing
    keyed in FK means solving both hands onto the haft at every frame,
    and they come apart the moment a shoulder angle changes -- the hands
    slide off the handle and the weight goes out of the swing.

    Rebuilt rather than added to. A second IK constraint on a bone does
    not replace the first, it stacks, and the limb then solves toward
    two targets at once; re-running a recipe would quietly do that.
    """
    target = params.get("target")
    if target is None or str(target).strip() == "":
        raise BadValue("add_ik_constraint needs a target for the limb to reach for.")

    pole = params.get("pole_target")
    pole_source = ""
    if pole is not None and str(pole).strip() != "":
        pole_source = (f'_ik.pole_target = _obj({_text(pole)})\n'
                       f'_ik.pole_subtarget = {_text(params.get("pole_subtarget"))}\n'
                       f'_ik.pole_angle = math.radians({_num(params.get("pole_angle"), -90.0)})\n')

    return (f'import math\n'
            f'_arm = _active(_obj({_text(params.get("armature"))}))\n'
            f'bpy.ops.object.mode_set(mode="POSE")\n'
            f'_pbone = _arm.pose.bones.get({_text(params.get("bone"))})\n'
            f'if _pbone is None:\n'
            f'    raise RuntimeError("no bone called %r -- the rig has: %s" % ('
            f'{_text(params.get("bone"))}, ", ".join(sorted(b.name for b in _arm.pose.bones))))\n'
            f'for _old in [_c for _c in _pbone.constraints if _c.type == "IK"]:\n'
            f'    _pbone.constraints.remove(_old)\n'
            f'_ik = _pbone.constraints.new("IK")\n'
            f'_ik.target = _obj({_text(target)})\n'
            f'_ik.subtarget = {_text(params.get("subtarget"))}\n'
            f'_ik.chain_count = {_int(params.get("chain_count"), 2, 0, IK_CHAIN_MAX)}\n'
            + pole_source +
            f'bpy.ops.object.mode_set(mode="OBJECT")\n'
            f'_note("add_ik_constraint", armature=_arm.name, '
            f'bone={_text(params.get("bone"))}, chain=_ik.chain_count)')


def set_interpolation(params: Dict[str, Any]) -> str:
    """How the keys that are already there get from one pose to the next.

    Blender keys BEZIER by default, so a posed rig already eases instead
    of ticking between frames, and for most motion that is the right
    answer and this action is not needed. It is needed where the timing
    has to be deliberate: CONSTANT to hold a wind-up dead still so the
    chop after it reads as a break, EASE_IN on the way into an impact,
    LINEAR through a pass where easing would look like hesitation.

    Narrowed by `bone` and `frame` when given, because a swing wants one
    shape on its wind-up and another on its follow-through, and setting
    a whole action at once cannot say that.

    THE FCURVES ARE NOT WHERE THE DOCS SAY
    --------------------------------------
    Blender 4.4 moved an Action's curves into slotted layers -- strips
    holding channelbags, one per slot -- and `action.fcurves` is the
    legacy view of that. It is still present, but on a slotted action it
    can be empty while the curves are perfectly there, which would make
    this action silently do nothing. So the layers are walked first and
    the flat list is the fallback, not the other way round.
    """
    frame = params.get("frame")
    frame_literal = ("None" if frame is None or str(frame).strip() == ""
                     else _int(frame, 1, 0, 1_000_000))

    return (f'_target = _obj({_text(params.get("object") or params.get("obj"))})\n'
            f'_anim = _target.animation_data\n'
            f'_action = _anim.action if _anim else None\n'
            f'if _action is None:\n'
            f'    raise RuntimeError("%r has no animation to shape yet" % _target.name)\n'
            f'_curves = []\n'
            f'for _layer in getattr(_action, "layers", []):\n'
            f'    for _strip in getattr(_layer, "strips", []):\n'
            f'        for _bag in getattr(_strip, "channelbags", []):\n'
            f'            _curves.extend(_bag.fcurves)\n'
            f'if not _curves:\n'
            f'    _curves = list(getattr(_action, "fcurves", []))\n'
            f'_bone = {_text(params.get("bone"))}\n'
            f'_only = {frame_literal}\n'
            f'_touched = 0\n'
            f'for _curve in _curves:\n'
            f'    if _bone and (\'pose.bones["%s"]\' % _bone) not in _curve.data_path:\n'
            f'        continue\n'
            f'    for _key in _curve.keyframe_points:\n'
            f'        if _only is not None and int(round(_key.co[0])) != _only:\n'
            f'            continue\n'
            f'        _key.interpolation = {_choice(params.get("interpolation"), INTERPOLATIONS, "BEZIER")}\n'
            f'        _key.easing = {_choice(params.get("easing"), EASINGS, "AUTO")}\n'
            f'        _touched += 1\n'
            f'    _curve.update()\n'
            f'if _touched == 0:\n'
            f'    raise RuntimeError("no keyframes matched -- bone=%r frame=%r" % (_bone, _only))\n'
            f'_note("set_interpolation", object=_target.name, keys=_touched)')


def add_cone(params: Dict[str, Any]) -> str:
    """A cone. One recipe in the library uses one, which is reason enough.

    `radius` is the base; a cone with `radius_top` above zero is a
    truncated one, which is what most game props actually want -- a
    lamp shade, a spoil heap, a hat.
    """
    return (f'bpy.ops.mesh.primitive_cone_add('
            f'radius1={_num(params.get("radius"), 1.0)}, '
            f'radius2={_num(params.get("radius_top"), 0.0)}, '
            f'depth={_num(params.get("depth"), 2.0)}, '
            f'location={_vector(params.get("location"))})\n'
            f'bpy.context.active_object.name = {_named(params, "Cone")}\n'
            f'_RESULT["created"].append(bpy.context.active_object.name)\n'
            f'_note("add_cone", name=bpy.context.active_object.name)')


def smooth_shade(params: Dict[str, Any]) -> str:
    """Round the shading without touching the geometry.

    Every recipe in the library asks for this on its curved parts -- 54
    times across 29 files -- and it is the difference between a cylinder
    that reads as a limb and one that reads as a barrel with facets.
    `smooth: false` turns it back off, because a sawn stone block wants
    its edges.

    `angle` is what a WHEEL needs, and every wheel in the library needed
    it. Plain shade_smooth smooths everything, including the hard rim
    where a cylinder's flat cap meets its barrel -- so the cap blends
    into the side and a tyre renders as a ball. Every cylinder in the
    library had this: wheels, hafts, grips, limbs. An angle limit keeps
    edges sharper than it and smooths the rest, so the barrel is round
    and the rim is still a rim. 30 degrees is the usual choice.

    Falls back to plain smoothing if the operator is not there, because
    shade_smooth_by_angle arrived in 4.1 and this should not be the
    thing that stops an older Blender building a recipe at all.
    """
    smooth = params.get("smooth", True)
    call = "shade_smooth" if smooth or smooth is None else "shade_flat"
    angle = params.get("angle")

    head = (f'_target = _active(_obj('
            f'{_text(params.get("object") or params.get("target"))}))\n')
    tail = (f'_note("smooth_shade", object=_target.name, '
            f'smooth={"True" if call == "shade_smooth" else "False"})')

    if angle is None or call != "shade_smooth":
        return head + f'bpy.ops.object.{call}()\n' + tail

    return (head
            + f'import math\n'
            + f'if hasattr(bpy.ops.object, "shade_smooth_by_angle"):\n'
            + f'    bpy.ops.object.shade_smooth_by_angle('
            f'angle=math.radians({_num(angle, 30.0)}))\n'
            + f'else:\n'
            + f'    bpy.ops.object.shade_smooth()\n'
            + tail)


def origin_to_geometry(params: Dict[str, Any]) -> str:
    """Put the origin at the middle of the thing, not wherever it was made.

    Distinct from origin_to_floor, which drops it to the lowest point so
    a prop can be stood on a floor. This one centres it, which is what
    you want before rotating or scaling a part about itself -- and what
    every recipe in the library asks for in its cleanup.

    With no object named, every mesh in the scene, because a recipe's
    cleanup applies to the whole build.
    """
    named = params.get("object") or params.get("target")

    if named is None or str(named).strip() == "":
        return ('bpy.ops.object.mode_set(mode="OBJECT")\n'
                'bpy.ops.object.select_all(action="DESELECT")\n'
                '_meshes = [o for o in bpy.data.objects if o.type == "MESH"]\n'
                'for _m in _meshes:\n'
                '    _m.select_set(True)\n'
                'if _meshes:\n'
                '    bpy.context.view_layer.objects.active = _meshes[0]\n'
                '    bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="MEDIAN")\n'
                '_note("origin_to_geometry", objects=len(_meshes))')

    return (f'_target = _active(_obj({_text(named)}))\n'
            f'bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="MEDIAN")\n'
            f'_note("origin_to_geometry", object=_target.name)')


def bind_to_bone(params: Dict[str, Any]) -> str:
    """Every vertex of one part to one bone, at full weight.

    The rigging move for a blockout made of separate parts, which is
    what every recipe in the library is. `auto_weights` guesses
    influence from distance, and on a figure whose upper arm and chest
    overlap it guesses wrong: the shoulder drags a corner of the chest
    with it and the seam pulls apart. A forearm is a rigid object and
    belongs entirely to the forearm bone.

    assign_vertex_group does the same job but wants the vertex indices
    named, which a recipe on disk cannot know. This reads them off the
    mesh, which is the only place they exist.

    Pair it with parent_mesh_to_armature, which binds by group NAME --
    the group made here has to be called after the bone, and is.
    """
    return (f'_target = _obj({_text(params.get("mesh") or params.get("object"))})\n'
            f'_bone = {_text(params.get("bone"))}\n'
            f'_group = _target.vertex_groups.get(_bone) or _target.vertex_groups.new(name=_bone)\n'
            f'_group.add([_v.index for _v in _target.data.vertices], '
            f'{_num(params.get("weight"), 1.0)}, "REPLACE")\n'
            f'_note("bind_to_bone", object=_target.name, bone=_bone, '
            f'vertices=len(_target.data.vertices))')


def load_image(params: Dict[str, Any]) -> str:
    """Bring a picture into the file so a material can use it.

    `check_existing` is on, so asking twice for the same file gives the
    same image rather than loading a second copy called Whatever.001 --
    which a recipe run twice would otherwise do every time, and only
    the first copy would be the one any material was pointing at.

    Packed, because the .blend or the FBX may be opened somewhere the
    original path does not exist. An export with a missing texture is a
    model that looks fine here and arrives in Unity untextured.
    """
    return (f'_path = {_text(params.get("path"))}\n'
            f'import os\n'
            f'if not os.path.isfile(_path):\n'
            f'    raise RuntimeError("no image at %r" % _path)\n'
            f'_img = bpy.data.images.load(_path, check_existing=True)\n'
            f'_name = {_text(params.get("name"))}\n'
            f'if _name:\n'
            f'    _img.name = _name\n'
            f'try:\n'
            f'    _img.pack()\n'
            f'except Exception:\n'
            f'    pass\n'
            f'_note("load_image", image=_img.name, size=list(_img.size))')


def set_texture(params: Dict[str, Any]) -> str:
    """Plug a loaded image into a material.

    Found by name and rebuilt, not added: a second image node wired to
    the same socket does not replace the first, it simply wins or loses
    depending on which link was made last, and a recipe run twice would
    leave a material nobody can reason about.

    UNLIT IS NOT A STYLE SETTING, IT IS THE WHOLE POINT
    ---------------------------------------------------
    A painted cut-out carries its own light -- the sun is in the
    painting, on the upper left, with the cool shadow down the right.
    A model lit by the scene instead gets a second, disagreeing light
    laid over the first, and reads as a 3D object among illustrations.
    Wiring the picture to Emission and taking Base Color to black makes
    the surface show exactly what was painted and nothing else, which
    is how a model stands next to a flat without contradicting it.
    """
    slot = _choice(params.get("slot"), TEXTURE_SLOTS, "Base Color")
    unlit = bool(params.get("unlit"))
    node = params.get("node") or "ARIA_Texture"

    wiring = (f'_socket = _bsdf.inputs[{slot}]\n'
              f'for _l in list(_tree.links):\n'
              f'    if _l.to_socket == _socket:\n'
              f'        _tree.links.remove(_l)\n'
              f'_tree.links.new(_tex.outputs["Color"], _socket)\n')

    if unlit:
        wiring = (f'_emit = _bsdf.inputs.get("Emission Color") or _bsdf.inputs.get("Emission")\n'
                  f'if _emit is None:\n'
                  f'    raise RuntimeError("this Principled BSDF has no emission input")\n'
                  f'for _l in list(_tree.links):\n'
                  f'    if _l.to_socket in (_emit, _bsdf.inputs["Base Color"]):\n'
                  f'        _tree.links.remove(_l)\n'
                  f'_tree.links.new(_tex.outputs["Color"], _emit)\n'
                  f'_bsdf.inputs["Base Color"].default_value = (0.0, 0.0, 0.0, 1.0)\n'
                  f'_strength = _bsdf.inputs.get("Emission Strength")\n'
                  f'if _strength is not None:\n'
                  f'    _strength.default_value = 1.0\n')

    return (f'_mat = bpy.data.materials.get({_text(params.get("material"))})\n'
            f'if _mat is None:\n'
            f'    raise RuntimeError("no material called %r -- the file has: %s" % ('
            f'{_text(params.get("material"))}, ", ".join(sorted(m.name for m in bpy.data.materials))))\n'
            f'_img = bpy.data.images.get({_text(params.get("image"))})\n'
            f'if _img is None:\n'
            f'    raise RuntimeError("no image called %r -- load_image it first. The file has: %s" % ('
            f'{_text(params.get("image"))}, ", ".join(sorted(i.name for i in bpy.data.images))))\n'
            f'_tree = _mat.node_tree\n'
            f'_bsdf = _tree.nodes.get("Principled BSDF")\n'
            f'if _bsdf is None:\n'
            f'    raise RuntimeError("%r has no Principled BSDF to plug into" % _mat.name)\n'
            f'_tex = _tree.nodes.get({_text(node)})\n'
            f'if _tex is None:\n'
            f'    _tex = _tree.nodes.new("ShaderNodeTexImage")\n'
            f'    _tex.name = {_text(node)}\n'
            f'    _tex.label = {_text(node)}\n'
            f'    _tex.location = (_bsdf.location.x - 420, _bsdf.location.y)\n'
            f'_tex.image = _img\n'
            f'_tex.interpolation = {_choice(params.get("interpolation"), INTERPOLATIONS_IMAGE, "Linear")}\n'
            + wiring +
            f'_rough = _bsdf.inputs.get("Roughness")\n'
            f'if _rough is not None:\n'
            f'    _rough.default_value = {_num(params.get("roughness"), 1.0)}\n'
            f'_note("set_texture", material=_mat.name, image=_img.name, '
            f'slot={slot}, unlit={unlit!r})')


def new_image(params: Dict[str, Any]) -> str:
    """A blank image for a bake to land in.

    A bake has to have somewhere to go: Cycles writes into whatever
    image the material's ACTIVE texture node is holding, and if there
    is no such image the operator fails with a message about no valid
    image to bake to, which reads like a UV problem and is not one.

    Non-colour for anything that is not colour. A normal map or a
    cavity mask read through the sRGB transfer curve is subtly and
    thoroughly wrong, and it is wrong in a way that looks like bad
    sculpting rather than like a colour space mistake.
    """
    data = "False" if params.get("float") in (None, "", False) else "True"
    alpha = "False" if params.get("alpha") in (None, "", False) else "True"

    return (f'_w = {_int(params.get("width"), 2048, 1, 16384)}\n'
            f'_h = {_int(params.get("height"), 2048, 1, 16384)}\n'
            f'_name = {_named(params, "Bake")}\n'
            f'_img = bpy.data.images.get(_name)\n'
            f'if _img is not None and (_img.size[0] != _w or _img.size[1] != _h):\n'
            f'    bpy.data.images.remove(_img)\n'
            f'    _img = None\n'
            f'if _img is None:\n'
            f'    _img = bpy.data.images.new(_name, width=_w, height=_h, '
            f'alpha={alpha}, float_buffer={data})\n'
            f'_colour = {_text(params.get("colorspace") or "")}\n'
            f'if _colour:\n'
            f'    _img.colorspace_settings.name = _colour\n'
            f'_note("new_image", image=_img.name, size=list(_img.size))')


def save_image(params: Dict[str, Any]) -> str:
    """Write an image to disk, so a bake can leave the .blend.

    Baked pixels live in memory until this happens. An export that
    references an unsaved bake arrives in Unity with a pink material
    and no obvious cause, because the FBX has a texture path pointing
    at a file nobody ever wrote.
    """
    return (f'import os\n'
            f'_img = bpy.data.images.get({_text(params.get("image"))})\n'
            f'if _img is None:\n'
            f'    raise RuntimeError("no image called %r -- the file has: %s" % ('
            f'{_text(params.get("image"))}, ", ".join(sorted(i.name for i in bpy.data.images))))\n'
            f'_path = {_text(params.get("path"))}\n'
            f'os.makedirs(os.path.dirname(_path) or ".", exist_ok=True)\n'
            f'_img.filepath_raw = _path\n'
            f'_img.file_format = "PNG"\n'
            f'_img.save()\n'
            f'_RESULT["exported"].append(_path)\n'
            f'_note("save_image", image=_img.name, path=_path)')


def bake_texture(params: Dict[str, Any]) -> str:
    """Bake what the material and the light are doing into an image.

    THE ACTIVE NODE IS THE WHOLE TRICK
    Cycles bakes into the image held by the material's active texture
    node -- not a node you name, not a slot you pass, the ACTIVE one.
    Nothing in the UI makes that obvious and nothing in the API hints
    at it, so a bake set up correctly in every other respect writes
    into the wrong image or refuses outright. This selects it.

    Cycles, because EEVEE cannot bake. The engine is set here rather
    than assumed, and put back afterwards so a recipe that baked does
    not quietly change how everything after it renders.

    AO and NORMAL are the two worth reaching for: they carry detail
    sculpted on a dense mesh down onto a light one, which is how a
    model ends up detailed without shipping the detail as geometry.
    """
    kind = _choice(params.get("type"), BAKE_TYPES, "DIFFUSE")
    selected = "True" if params.get("from_selected") else "False"

    return (f'_target = _active(_obj({_text(params.get("object"))}))\n'
            f'_img = bpy.data.images.get({_text(params.get("image"))})\n'
            f'if _img is None:\n'
            f'    raise RuntimeError("no image called %r -- new_image it first" % '
            f'{_text(params.get("image"))})\n'
            f'if not _target.data.uv_layers:\n'
            f'    raise RuntimeError("%r has no UVs to bake into -- unwrap it first" % _target.name)\n'
            f'if not _target.data.materials:\n'
            f'    raise RuntimeError("%r has no material to bake from" % _target.name)\n'
            f'for _slot in _target.data.materials:\n'
            f'    if _slot is None or not _slot.node_tree:\n'
            f'        continue\n'
            f'    _tree = _slot.node_tree\n'
            f'    _dest = _tree.nodes.get("ARIA_Bake")\n'
            f'    if _dest is None:\n'
            f'        _dest = _tree.nodes.new("ShaderNodeTexImage")\n'
            f'        _dest.name = "ARIA_Bake"\n'
            f'        _dest.label = "ARIA_Bake"\n'
            f'        _dest.location = (-900, -300)\n'
            f'    _dest.image = _img\n'
            f'    for _n in _tree.nodes:\n'
            f'        _n.select = False\n'
            f'    _dest.select = True\n'
            f'    _tree.nodes.active = _dest\n'
            f'_was = bpy.context.scene.render.engine\n'
            f'bpy.context.scene.render.engine = "CYCLES"\n'
            f'bpy.context.scene.cycles.samples = {_int(params.get("samples"), 32, 1, 4096)}\n'
            f'bpy.context.scene.render.bake.margin = {_int(params.get("margin"), 8, 0, 256)}\n'
            f'bpy.context.scene.render.bake.use_selected_to_active = {selected}\n'
            f'try:\n'
            f'    bpy.ops.object.bake(type={kind})\n'
            f'finally:\n'
            f'    bpy.context.scene.render.engine = _was\n'
            f'_note("bake_texture", object=_target.name, image=_img.name, kind={kind})')


def set_shader_node(params: Dict[str, Any]) -> str:
    """Set one named input on a material's Principled BSDF.

    By name and through an allowlist, because the inputs moved: what
    was "Specular" is "Specular IOR Level" and what was "Emission" is
    "Emission Color", and a caller setting the old name gets silence
    rather than an error. A name outside the list is refused while the
    script is being built, which is before Blender starts.
    """
    field = _choice(params.get("input"), SHADER_INPUTS, "Roughness")
    value = params.get("value")

    if isinstance(value, (list, tuple)):
        parts = (list(value) + [0.0, 0.0, 0.0, 1.0])[:4]
        setter = f'_socket.default_value = ({", ".join(_num(v) for v in parts)})\n'
    else:
        setter = f'_socket.default_value = {_num(value, 0.5)}\n'

    return (f'_mat = bpy.data.materials.get({_text(params.get("material"))})\n'
            f'if _mat is None:\n'
            f'    raise RuntimeError("no material called %r" % {_text(params.get("material"))})\n'
            f'_bsdf = _mat.node_tree.nodes.get("Principled BSDF")\n'
            f'if _bsdf is None:\n'
            f'    raise RuntimeError("%r has no Principled BSDF" % _mat.name)\n'
            f'_socket = _bsdf.inputs.get({field})\n'
            f'if _socket is None:\n'
            f'    raise RuntimeError("this Principled BSDF has no %r -- it has: %s" % ('
            f'{field}, ", ".join(i.name for i in _bsdf.inputs)))\n'
            + setter +
            f'_note("set_shader_node", material=_mat.name, input={field})')


def quad_remesh(params: Dict[str, Any]) -> str:
    """Retopologise to quads with QuadriFlow.

    For after sculpting. A sculpt leaves geometry that is dense, uneven
    and useless to animate; this lays an even quad mesh over the same
    shape at a face count you choose. It is Blender's own, not an
    add-on, and it is slow -- seconds to minutes on a dense mesh --
    which is worth knowing before it is put in a loop.

    UVs do not survive it. Unwrap after, never before.
    """
    return (f'_target = _active(_obj({_text(params.get("object"))}))\n'
            f'if not _target.data.polygons:\n'
            f'    raise RuntimeError("%r has no faces to remesh" % _target.name)\n'
            f'bpy.ops.object.quadriflow_remesh('
            f'target_faces={_int(params.get("faces"), 5000, 4, 1000000)}, '
            f'use_preserve_sharp=False, use_preserve_boundary=False, '
            f'use_mesh_symmetry={"True" if params.get("symmetry") else "False"})\n'
            f'_note("quad_remesh", object=_target.name, faces=len(_target.data.polygons))')


def voxel_remesh(params: Dict[str, Any]) -> str:
    """Rebuild a mesh as a uniform voxel grid.

    The other retopology, and the one to reach for mid-sculpt rather
    than at the end: it welds intersecting parts into one continuous
    surface, which is how a figure assembled from separate primitives
    becomes something sculptable at all. Quads are not guaranteed and
    the result is dense -- follow it with quad_remesh when the shape is
    settled.

    Size is in metres and it is the whole control: halving it roughly
    quadruples the face count.
    """
    return (f'_target = _active(_obj({_text(params.get("object"))}))\n'
            f'_target.data.remesh_voxel_size = {_num(params.get("size"), 0.02)}\n'
            f'_target.data.remesh_voxel_adaptivity = {_num(params.get("adaptivity"), 0.0)}\n'
            f'bpy.ops.object.voxel_remesh()\n'
            f'_note("voxel_remesh", object=_target.name, faces=len(_target.data.polygons))')


def stamp_detail(params: Dict[str, Any]) -> str:
    """Press a pattern into the surface: cracks, grain, pores, dents.

    A displacement modifier with a procedural texture behind it, NOT a
    sculpt brush. sculpt_brush explains why at length: a stroke needs
    screen coordinates and a 3D viewport, --background has neither, and
    a brush that is configured but never stroked changes nothing at
    all. This moves vertices, which is the thing that was wanted.

    IT NEEDS GEOMETRY TO MOVE. Displacement pushes existing vertices
    along their normals; it cannot invent them. On a default cube the
    result is a slightly larger cube. Subdivide first -- three or four
    levels applied -- or the recipe will look like it did nothing.

    `apply` bakes it into the mesh. Left off, the modifier stays live
    and stacks with whatever comes after, which is usually what you
    want mid-recipe and never what you want before an export.
    """
    kind = _choice(params.get("texture"), STAMP_TEXTURE_TYPES, "CLOUDS")
    coords = _choice(params.get("coords"), DISPLACE_COORDS, "LOCAL")
    apply_now = bool(params.get("apply"))

    finish = ''
    if apply_now:
        finish = (f'bpy.ops.object.modifier_apply(modifier=_mod.name)\n')

    return (f'_target = _active(_obj({_text(params.get("object"))}))\n'
            f'_tname = {_named(params, "ARIA_Stamp")}\n'
            f'_tex = bpy.data.textures.get(_tname)\n'
            f'if _tex is None or _tex.type != {kind}:\n'
            f'    if _tex is not None:\n'
            f'        bpy.data.textures.remove(_tex)\n'
            f'    _tex = bpy.data.textures.new(_tname, type={kind})\n'
            f'for _field, _value in (("noise_scale", {_num(params.get("scale"), 0.25)}),\n'
            f'                       ("noise_depth", {_int(params.get("depth"), 2, 0, 16)}),\n'
            f'                       ("contrast", {_num(params.get("contrast"), 1.0)})):\n'
            f'    if hasattr(_tex, _field):\n'
            f'        setattr(_tex, _field, _value)\n'
            f'_image = {_text(params.get("image"))}\n'
            f'if _image and _tex.type == "IMAGE":\n'
            f'    _tex.image = bpy.data.images.get(_image)\n'
            f'_mod = _target.modifiers.get(_tname)\n'
            f'if _mod is not None and _mod.type != "DISPLACE":\n'
            f'    _target.modifiers.remove(_mod)\n'
            f'    _mod = None\n'
            f'if _mod is None:\n'
            f'    _mod = _target.modifiers.new(name=_tname, type="DISPLACE")\n'
            f'_mod.texture = _tex\n'
            f'_mod.texture_coords = {coords}\n'
            f'_mod.strength = {_num(params.get("strength"), 0.05)}\n'
            f'_mod.mid_level = {_num(params.get("mid_level"), 0.5)}\n'
            f'_before = len(_target.data.vertices)\n'
            + finish +
            f'_note("stamp_detail", object=_target.name, texture=_tex.name, '
            f'kind={kind}, verts=_before)')


def edge_wear(params: Dict[str, Any]) -> str:
    """A mask that finds the edges, for paint worn off the corners.

    Built from a Bevel node, which is the standard trick and not an
    obvious one: bevel the shading normal over a small radius and
    compare it to the true normal, and the two agree everywhere flat
    and disagree on a corner. The difference IS the edge mask.

    Left as nodes rather than applied. Wire it where it is wanted, or
    bake it with bake_texture and use it as a map -- which is what it
    is for, because this only exists in Cycles.
    """
    return (f'_mat = bpy.data.materials.get({_text(params.get("material"))})\n'
            f'if _mat is None:\n'
            f'    raise RuntimeError("no material called %r" % {_text(params.get("material"))})\n'
            f'_tree = _mat.node_tree\n'
            f'_bevel = _tree.nodes.get("ARIA_EdgeBevel") or _tree.nodes.new("ShaderNodeBevel")\n'
            f'_bevel.name = "ARIA_EdgeBevel"\n'
            f'_bevel.location = (-900, 400)\n'
            f'_bevel.inputs["Radius"].default_value = {_num(params.get("radius"), 0.02)}\n'
            f'_geo = _tree.nodes.get("ARIA_EdgeGeometry") or _tree.nodes.new("ShaderNodeNewGeometry")\n'
            f'_geo.name = "ARIA_EdgeGeometry"\n'
            f'_geo.location = (-900, 200)\n'
            f'_dot = _tree.nodes.get("ARIA_EdgeDot") or _tree.nodes.new("ShaderNodeVectorMath")\n'
            f'_dot.name = "ARIA_EdgeDot"\n'
            f'_dot.operation = "DOT_PRODUCT"\n'
            f'_dot.location = (-700, 300)\n'
            f'_ramp = _tree.nodes.get("ARIA_EdgeRamp") or _tree.nodes.new("ShaderNodeValToRGB")\n'
            f'_ramp.name = "ARIA_EdgeRamp"\n'
            f'_ramp.location = (-500, 300)\n'
            f'_ramp.color_ramp.elements[0].position = {_num(params.get("softness"), 0.55)}\n'
            f'_ramp.color_ramp.elements[1].position = {_num(params.get("sharpness"), 0.75)}\n'
            f'for _l in list(_tree.links):\n'
            f'    if _l.to_node in (_dot, _ramp):\n'
            f'        _tree.links.remove(_l)\n'
            f'_tree.links.new(_bevel.outputs["Normal"], _dot.inputs[0])\n'
            f'_tree.links.new(_geo.outputs["Normal"], _dot.inputs[1])\n'
            f'_tree.links.new(_dot.outputs["Value"], _ramp.inputs["Fac"])\n'
            f'_note("edge_wear", material=_mat.name, node="ARIA_EdgeRamp")')


def cavity_mask(params: Dict[str, Any]) -> str:
    """A mask that finds the creases, for dirt that settles in them.

    The other half of edge_wear and its opposite: an Ambient Occlusion
    node with a short distance is dark where the surface folds in on
    itself, which is exactly where grime, rust and shadow collect.
    Together they are most of what makes a surface look used rather
    than new.

    Nodes, not pixels. Bake it to get a map.
    """
    inside = "True" if params.get("inside") in (None, "", True) else "False"

    return (f'_mat = bpy.data.materials.get({_text(params.get("material"))})\n'
            f'if _mat is None:\n'
            f'    raise RuntimeError("no material called %r" % {_text(params.get("material"))})\n'
            f'_tree = _mat.node_tree\n'
            f'_ao = _tree.nodes.get("ARIA_Cavity") or _tree.nodes.new("ShaderNodeAmbientOcclusion")\n'
            f'_ao.name = "ARIA_Cavity"\n'
            f'_ao.location = (-900, -600)\n'
            f'_ao.samples = {_int(params.get("samples"), 16, 1, 128)}\n'
            f'_ao.inside = {inside}\n'
            f'_ao.only_local = True\n'
            f'_ao.inputs["Distance"].default_value = {_num(params.get("distance"), 0.08)}\n'
            f'_ramp = _tree.nodes.get("ARIA_CavityRamp") or _tree.nodes.new("ShaderNodeValToRGB")\n'
            f'_ramp.name = "ARIA_CavityRamp"\n'
            f'_ramp.location = (-680, -600)\n'
            f'_ramp.color_ramp.elements[0].position = {_num(params.get("low"), 0.2)}\n'
            f'_ramp.color_ramp.elements[1].position = {_num(params.get("high"), 0.8)}\n'
            f'for _l in list(_tree.links):\n'
            f'    if _l.to_node is _ramp:\n'
            f'        _tree.links.remove(_l)\n'
            f'_tree.links.new(_ao.outputs["Color"], _ramp.inputs["Fac"])\n'
            f'_note("cavity_mask", material=_mat.name, node="ARIA_CavityRamp")')


def add_light(params: Dict[str, Any]) -> str:
    """A lamp, so there is something for a bake to bake.

    THE REASON THIS EXISTS, WHICH IS NOT OBVIOUS
    Baking AO on a figure built from convex parts gives an almost white
    map: the only dark places are the faces buried inside neighbouring
    parts, which nobody ever sees. It looks like a bake that failed and
    is a bake that worked on a shape with nothing to occlude.

    What a painted character actually carries is light -- warm from the
    upper left, cool down the other side -- and to bake that there has
    to be a lamp. A SUN is the one to reach for: it is directional, so
    every limb takes the light from the same angle, which is what an
    artist does by hand and what makes the parts look like one figure.

    Angle is in degrees: elevation above the horizon and the bearing it
    comes from, which is how a person describes a sun and not how
    Blender stores one.
    """
    kind = _choice(params.get("type"), LIGHT_TYPES, "SUN")
    elevation = _num(params.get("elevation"), 38.0)
    bearing = _num(params.get("bearing"), -38.0)

    return (f'import math\n'
            f'_name = {_named(params, "ARIA_Light")}\n'
            f'_old = bpy.data.objects.get(_name)\n'
            f'if _old is not None:\n'
            f'    bpy.data.objects.remove(_old, do_unlink=True)\n'
            f'_data = bpy.data.lights.new(name=_name, type={kind})\n'
            f'_data.energy = {_num(params.get("energy"), 3.0)}\n'
            f'_colour = {_vector(params.get("color"), (1.0, 0.96, 0.88))}\n'
            f'_data.color = _colour\n'
            f'if hasattr(_data, "angle"):\n'
            f'    _data.angle = math.radians({_num(params.get("softness"), 8.0)})\n'
            f'_lamp = bpy.data.objects.new(_name, _data)\n'
            f'bpy.context.collection.objects.link(_lamp)\n'
            f'_lamp.location = {_vector(params.get("location"), (0.0, 0.0, 6.0))}\n'
            f'_elev = math.radians({elevation})\n'
            f'_bear = math.radians({bearing})\n'
            f'_lamp.rotation_euler = (math.radians(90.0) - _elev, 0.0, _bear)\n'
            f'_note("add_light", name=_lamp.name, kind={kind}, '
            f'elevation={elevation}, bearing={bearing})')


def set_world(params: Dict[str, Any]) -> str:
    """The sky the scene sits under -- the fill light, effectively.

    A bake with a black world gives a figure lit from one side and
    dead on the other, which reads as a cut-out in a cave. Painted art
    has bounce: the shadow side is cooler and darker, never black. This
    is that, and it is half of why a baked figure sits beside a painted
    one at all.
    """
    return (f'_world = bpy.context.scene.world or bpy.data.worlds.new("World")\n'
            f'bpy.context.scene.world = _world\n'
            f'_world.use_nodes = True\n'
            f'_bg = _world.node_tree.nodes.get("Background")\n'
            f'if _bg is None:\n'
            f'    raise RuntimeError("the world has no Background node")\n'
            f'_c = {_vector(params.get("color"), (0.42, 0.46, 0.55))}\n'
            f'_bg.inputs[0].default_value = (_c[0], _c[1], _c[2], 1.0)\n'
            f'_bg.inputs[1].default_value = {_num(params.get("strength"), 0.6)}\n'
            f'_note("set_world", strength={_num(params.get("strength"), 0.6)})')


def append_from_blend(params: Dict[str, Any]) -> str:
    """Append one object out of another .blend file.

    The data API rather than bpy.ops.wm.append, which wants a filepath
    stitched together out of the blend, the word "Object" and the name,
    and fails quietly when that path is a hair wrong. Loading through
    bpy.data.libraries.load names what it wants and says so when it is
    not there.

    Appended, not linked: a linked object is read-only and cannot be
    modified, scaled, remeshed or bound to an armature, which is the
    entire reason for fetching it.

    A miss lists what the file actually holds. A 49MB library with 382
    meshes in it is not something anybody is going to guess their way
    around, and "object not found" with no list is the least helpful
    thing this could say.
    """
    rename = params.get("name")

    return (f'import os\n'
            f'_blend = {_text(params.get("blend") or params.get("path"))}\n'
            f'if not os.path.isfile(_blend):\n'
            f'    raise RuntimeError("no .blend at %r" % _blend)\n'
            f'_want = {_text(params.get("object"))}\n'
            f'_before = set(bpy.data.objects)\n'
            f'with bpy.data.libraries.load(_blend, link=False) as (_src, _dst):\n'
            f'    _have = list(_src.objects)\n'
            f'    if _want not in _have:\n'
            f'        raise RuntimeError("%r is not in %s. It holds %d objects; "\n'
            f'                           "the ones with \'body\' in the name are: %s"\n'
            f'                           % (_want, os.path.basename(_blend), len(_have),\n'
            f'                              ", ".join(n for n in sorted(_have) if "body" in n.lower())\n'
            f'                              or "none"))\n'
            f'    _dst.objects = [_want]\n'
            f'_added = [o for o in _dst.objects if o is not None]\n'
            f'for _o in _added:\n'
            f'    bpy.context.collection.objects.link(_o)\n'
            f'if not _added:\n'
            f'    raise RuntimeError("%r loaded as nothing" % _want)\n'
            f'_new = _added[0]\n'
            f'_rename = {_text(rename)}\n'
            f'if _rename:\n'
            f'    _new.name = _rename\n'
            f'    if _new.data is not None:\n'
            f'        _new.data.name = _rename\n'
            f'_new.location = {_vector(params.get("location"))}\n'
            f'bpy.context.view_layer.objects.active = _new\n'
            f'_new.select_set(True)\n'
            f'_RESULT["created"].append(_new.name)\n'
            f'_note("append_from_blend", object=_new.name, '
            f'verts=len(_new.data.vertices) if _new.type == "MESH" else 0, '
            f'uvs=[u.name for u in _new.data.uv_layers] if _new.type == "MESH" else [])')


def apply_shrinkwrap(params: Dict[str, Any]) -> str:
    """Wrap one mesh onto another -- which is how clothing happens here.

    THE ANSWER TO "HOW DO YOU MODEL A VEST WITHOUT SCULPTING"
    You do not model it. You make a rough shape roughly where the vest
    goes, wrap it onto the body so every vertex lands on the body's own
    surface, and then thicken it with apply_solidify. What comes out
    fits the character, because it was taken FROM the character.

    `offset` is the gap between garment and skin. Zero puts the cloth
    exactly on the body, which reads as paint; a centimetre or two is
    cloth. On a figure scaled to 1.8m, 0.01 to 0.02 is about right.

    Applied by default, because a live shrinkwrap re-evaluates when the
    body moves and a garment bound to an armature should already have
    the shape baked in.
    """
    return (f'_target = _active(_obj({_text(params.get("object"))}))\n'
            f'_onto = _obj({_text(params.get("target") or params.get("onto"))})\n'
            f'_mod = _target.modifiers.new(name="ARIA_Shrinkwrap", type="SHRINKWRAP")\n'
            f'_mod.target = _onto\n'
            f'_mod.wrap_method = {_choice(params.get("method"), WRAP_METHODS, "NEAREST_SURFACEPOINT")}\n'
            f'_mod.offset = {_num(params.get("offset"), 0.012)}\n'
            f'_mod.use_negative_direction = True\n'
            f'_mod.use_positive_direction = True\n'
            f'{"bpy.ops.object.modifier_apply(modifier=_mod.name)" if params.get("apply", True) else "pass"}\n'
            f'_note("apply_shrinkwrap", object=_target.name, onto=_onto.name)')


def apply_simple_deform(params: Dict[str, Any]) -> str:
    """Bend, taper, twist or stretch a whole shape at once.

    Silhouette work. A taper turns a straight limb into one that
    narrows toward the wrist; a bend puts a curve through a shape that
    was modelled straight. Cheap, total, and nothing like sculpting --
    it moves everything by a rule rather than anything by hand, which
    is the trade the reframing makes.
    """
    return (f'import math\n'
            f'_target = _active(_obj({_text(params.get("object"))}))\n'
            f'_mod = _target.modifiers.new(name="ARIA_Deform", type="SIMPLE_DEFORM")\n'
            f'_mod.deform_method = {_choice(params.get("method"), DEFORM_METHODS, "TAPER")}\n'
            f'_mod.deform_axis = {_choice(params.get("axis"), AXES, "Z")}\n'
            f'_mod.factor = {_num(params.get("factor"), 0.3)}\n'
            f'_mod.angle = math.radians({_num(params.get("angle"), 45.0)})\n'
            f'{"bpy.ops.object.modifier_apply(modifier=_mod.name)" if params.get("apply", True) else "pass"}\n'
            f'_note("apply_simple_deform", object=_target.name)')


def apply_cast(params: Dict[str, Any]) -> str:
    """Push a shape toward a sphere, cylinder or box.

    How a blocky thing becomes a soft one. Subdivision rounds the
    corners off geometry; a cast pulls the whole silhouette toward a
    primitive, which is what "plush" means -- an arm that is a bit more
    sausage than it was. Factor 1 is all the way there, which is almost
    never wanted; a third is usually plenty.
    """
    return (f'_target = _active(_obj({_text(params.get("object"))}))\n'
            f'_mod = _target.modifiers.new(name="ARIA_Cast", type="CAST")\n'
            f'_mod.cast_type = {_choice(params.get("shape"), CAST_TYPES, "SPHERE")}\n'
            f'_mod.factor = {_num(params.get("factor"), 0.35)}\n'
            f'_mod.radius = {_num(params.get("radius"), 0.0)}\n'
            f'_mod.size = {_num(params.get("size"), 0.0)}\n'
            f'_mod.use_x = True\n'
            f'_mod.use_y = True\n'
            f'_mod.use_z = True\n'
            f'{"bpy.ops.object.modifier_apply(modifier=_mod.name)" if params.get("apply", True) else "pass"}\n'
            f'_note("apply_cast", object=_target.name)')


def apply_lattice(params: Dict[str, Any]) -> str:
    """A cage around a mesh, for changing proportions.

    The procedural answer to "chibi proportions, exaggerated features".
    A lattice is a grid of control points enclosing the mesh; moving one
    drags everything near it, smoothly. Pull the top points out and the
    head grows without the face distorting, which is the thing a scale
    on the head object cannot do because it would take the neck with it.

    Built to fit the mesh's own bounding box, so the caller does not
    have to know where the character is standing. Left unapplied --
    move_lattice_point is what does the work, and applying before the
    points move would bake in nothing at all.
    """
    return (f'_target = _obj({_text(params.get("object"))})\n'
            f'_name = {_named(params, "ARIA_Lattice")}\n'
            f'_old = bpy.data.objects.get(_name)\n'
            f'if _old is not None:\n'
            f'    bpy.data.objects.remove(_old, do_unlink=True)\n'
            f'_data = bpy.data.lattices.new(_name)\n'
            f'_data.points_u = {_int(params.get("u"), 2, 2, 20)}\n'
            f'_data.points_v = {_int(params.get("v"), 2, 2, 20)}\n'
            f'_data.points_w = {_int(params.get("w"), 4, 2, 20)}\n'
            f'_lat = bpy.data.objects.new(_name, _data)\n'
            f'bpy.context.collection.objects.link(_lat)\n'
            f'_corners = [_target.matrix_world.__matmul__(mathutils.Vector(_c)) '
            f'for _c in _target.bound_box]\n'
            f'_lo = mathutils.Vector((min(c.x for c in _corners), min(c.y for c in _corners), '
            f'min(c.z for c in _corners)))\n'
            f'_hi = mathutils.Vector((max(c.x for c in _corners), max(c.y for c in _corners), '
            f'max(c.z for c in _corners)))\n'
            f'_lat.location = (_lo + _hi) / 2.0\n'
            f'_pad = {_num(params.get("padding"), 1.02)}\n'
            f'_lat.scale = ((_hi.x - _lo.x) * _pad or 1.0, (_hi.y - _lo.y) * _pad or 1.0, '
            f'(_hi.z - _lo.z) * _pad or 1.0)\n'
            f'_mod = _target.modifiers.new(name=_name, type="LATTICE")\n'
            f'_mod.object = _lat\n'
            f'_note("apply_lattice", object=_target.name, lattice=_lat.name, '
            f'points=[_data.points_u, _data.points_v, _data.points_w])')


def move_lattice_point(params: Dict[str, Any]) -> str:
    """Move one control point of a lattice, and everything near it.

    Where the proportion change actually happens. The point is named by
    its place in the grid -- u across, v through, w up -- which is how
    a caller thinks about it ("the top layer", "the front") rather than
    by an index into a flat list nobody can picture.

    `layer` moves every point at one w, which is the common case: the
    whole top of the cage out by a tenth makes a bigger head.
    """
    layer = params.get("layer")
    one = "" if layer is not None and str(layer).strip() != "" else "not-layer"

    if one == "":
        pick = (f'_want = [_i for _i in range(len(_pts)) '
                f'if (_i // (_u * _v)) == {_int(layer, 0, 0, 19)}]\n')
    else:
        pick = (f'_iu = {_int(params.get("u"), 0, 0, 19)}\n'
                f'_iv = {_int(params.get("v"), 0, 0, 19)}\n'
                f'_iw = {_int(params.get("w"), 0, 0, 19)}\n'
                f'_want = [_iw * _u * _v + _iv * _u + _iu]\n')

    return (f'_lat = _obj({_text(params.get("lattice"))})\n'
            f'if _lat.type != "LATTICE":\n'
            f'    raise RuntimeError("%r is not a lattice" % _lat.name)\n'
            f'_pts = _lat.data.points\n'
            f'_u, _v = _lat.data.points_u, _lat.data.points_v\n'
            + pick +
            f'_off = {_vector(params.get("offset"))}\n'
            f'for _i in _want:\n'
            f'    if 0 <= _i < len(_pts):\n'
            f'        _p = _pts[_i]\n'
            f'        _p.co_deform = (_p.co_deform[0] + _off[0], _p.co_deform[1] + _off[1], '
            f'_p.co_deform[2] + _off[2])\n'
            f'_note("move_lattice_point", lattice=_lat.name, moved=len(_want))')


def join_objects(params: Dict[str, Any]) -> str:
    """Make several meshes into one.

    A character assembled from parts is a dozen objects, and a dozen
    objects is a dozen draw calls, a dozen materials and a dozen things
    to bind separately. One mesh takes one armature, one UV layout and
    one bake.

    The first name in the list is the one that survives; everything
    else is merged into it and disappears.
    """
    return (f'_names = {list(params.get("objects") or [])!r}\n'
            f'if len(_names) < 2:\n'
            f'    raise RuntimeError("joining needs at least two objects")\n'
            f'_keep = _active(_obj(_names[0]))\n'
            f'for _n in _names[1:]:\n'
            f'    _other = _obj(_n)\n'
            f'    if _other.type != "MESH":\n'
            f'        raise RuntimeError("%r is a %s, not a mesh" % (_n, _other.type))\n'
            f'    _other.select_set(True)\n'
            f'bpy.context.view_layer.objects.active = _keep\n'
            f'bpy.ops.object.join()\n'
            f'_note("join_objects", into=_keep.name, count=len(_names), '
            f'verts=len(_keep.data.vertices))')


def add_geometry_nodes(params: Dict[str, Any]) -> str:
    """An empty geometry node group on an object, wired input to output.

    The foundation the other two stand on, and on its own it does
    nothing -- geometry goes in and the same geometry comes out. That
    is deliberate: a group that passes through is a group you can add
    to without having first broken the object.

    Blender 4.0 moved a group's sockets from `group.inputs` and
    `group.outputs` to `group.interface`, and the old names simply are
    not there in 5.0 -- not deprecated, absent. A template written
    against a tutorial from before that change fails on its first line.
    """
    return (f'_target = _active(_obj({_text(params.get("object"))}))\n'
            f'_name = {_named(params, "ARIA_Nodes")}\n'
            f'_group = bpy.data.node_groups.get(_name)\n'
            f'if _group is not None:\n'
            f'    bpy.data.node_groups.remove(_group)\n'
            f'_group = bpy.data.node_groups.new(_name, "GeometryNodeTree")\n'
            f'_group.interface.new_socket("Geometry", in_out="INPUT", '
            f'socket_type="NodeSocketGeometry")\n'
            f'_group.interface.new_socket("Geometry", in_out="OUTPUT", '
            f'socket_type="NodeSocketGeometry")\n'
            f'_in = _group.nodes.new("NodeGroupInput")\n'
            f'_in.location = (-600, 0)\n'
            f'_out = _group.nodes.new("NodeGroupOutput")\n'
            f'_out.location = (600, 0)\n'
            f'_group.links.new(_in.outputs[0], _out.inputs[0])\n'
            f'_mod = _target.modifiers.get(_name)\n'
            f'if _mod is not None and _mod.type != "NODES":\n'
            f'    _target.modifiers.remove(_mod)\n'
            f'    _mod = None\n'
            f'if _mod is None:\n'
            f'    _mod = _target.modifiers.new(name=_name, type="NODES")\n'
            f'_mod.node_group = _group\n'
            f'_note("add_geometry_nodes", object=_target.name, group=_group.name)')


def scatter_on_surface(params: Dict[str, Any]) -> str:
    """Scatter instances of one object across another's faces.

    What geometry nodes are for in a pipeline like this: rivets along a
    strap, studs on a boot, stones across a quarry floor, clumps on a
    head. The scattered object is instanced, not copied, so ten
    thousand of them cost about what one does.

    SAME SEED, SAME SCATTER. That is the whole difference between
    procedural and random: a recipe run twice puts every stone back
    where it was, and a recipe run with the seed changed by one gives a
    different quarry that is just as repeatable.

    REALIZE INSTANCES, WHICH IS THE TRAP
    Instances are not geometry. They draw, they render, and they export
    to FBX as nothing at all -- the model arrives in Unity with a bare
    surface and no rivets, and nothing anywhere says why. A Realize
    Instances node before the output turns them into real mesh so they
    survive the trip.

    The original surface is joined back in, because a scatter that
    replaced the thing it scattered over would be a strange thing to
    want.
    """
    align = "True" if params.get("align", True) else "False"

    return (f'_target = _active(_obj({_text(params.get("object"))}))\n'
            f'_source = _obj({_text(params.get("scatter") or params.get("source"))})\n'
            f'_name = {_named(params, "ARIA_Scatter")}\n'
            f'_group = bpy.data.node_groups.get(_name)\n'
            f'if _group is not None:\n'
            f'    bpy.data.node_groups.remove(_group)\n'
            f'_group = bpy.data.node_groups.new(_name, "GeometryNodeTree")\n'
            f'_group.interface.new_socket("Geometry", in_out="INPUT", '
            f'socket_type="NodeSocketGeometry")\n'
            f'_group.interface.new_socket("Geometry", in_out="OUTPUT", '
            f'socket_type="NodeSocketGeometry")\n'
            f'_in = _group.nodes.new("NodeGroupInput"); _in.location = (-900, 0)\n'
            f'_out = _group.nodes.new("NodeGroupOutput"); _out.location = (700, 0)\n'
            f'_dist = _group.nodes.new("GeometryNodeDistributePointsOnFaces")\n'
            f'_dist.location = (-600, 100)\n'
            f'_dist.distribute_method = '
            f'{_choice(params.get("method"), DISTRIBUTE_METHODS, "POISSON")}\n'
            f'_info = _group.nodes.new("GeometryNodeObjectInfo")\n'
            f'_info.location = (-600, -260)\n'
            f'_info.inputs["Object"].default_value = _source\n'
            f'_info.transform_space = "ORIGINAL"\n'
            f'_rand = _group.nodes.new("FunctionNodeRandomValue")\n'
            f'_rand.location = (-350, -120)\n'
            f'_rand.data_type = "FLOAT"\n'
            f'_rand.inputs[2].default_value = {_num(params.get("scale_min"), 0.8)}\n'
            f'_rand.inputs[3].default_value = {_num(params.get("scale_max"), 1.2)}\n'
            f'_rand.inputs["Seed"].default_value = {_int(params.get("seed"), 0, 0, 1000000)}\n'
            f'_inst = _group.nodes.new("GeometryNodeInstanceOnPoints")\n'
            f'_inst.location = (-80, 0)\n'
            f'_real = _group.nodes.new("GeometryNodeRealizeInstances")\n'
            f'_real.location = (200, 0)\n'
            f'_join = _group.nodes.new("GeometryNodeJoinGeometry")\n'
            f'_join.location = (450, 0)\n'
            f'_L = _group.links\n'
            f'_L.new(_in.outputs[0], _dist.inputs["Mesh"])\n'
            f'_L.new(_dist.outputs["Points"], _inst.inputs["Points"])\n'
            f'_L.new(_info.outputs["Geometry"], _inst.inputs["Instance"])\n'
            f'_L.new(_rand.outputs[1], _inst.inputs["Scale"])\n'
            f'if {align}:\n'
            f'    _L.new(_dist.outputs["Rotation"], _inst.inputs["Rotation"])\n'
            f'_L.new(_inst.outputs["Instances"], _real.inputs["Geometry"])\n'
            f'_L.new(_real.outputs["Geometry"], _join.inputs["Geometry"])\n'
            f'_L.new(_in.outputs[0], _join.inputs["Geometry"])\n'
            f'_L.new(_join.outputs["Geometry"], _out.inputs[0])\n'
            f'_seed = _dist.inputs.get("Seed")\n'
            f'if _seed is not None:\n'
            f'    _seed.default_value = {_int(params.get("seed"), 0, 0, 1000000)}\n'
            f'_den = _dist.inputs.get("Density Max") or _dist.inputs.get("Density")\n'
            f'if _den is not None:\n'
            f'    _den.default_value = {_num(params.get("density"), 40.0)}\n'
            f'_mod = _target.modifiers.get(_name)\n'
            f'if _mod is not None and _mod.type != "NODES":\n'
            f'    _target.modifiers.remove(_mod); _mod = None\n'
            f'if _mod is None:\n'
            f'    _mod = _target.modifiers.new(name=_name, type="NODES")\n'
            f'_mod.node_group = _group\n'
            f'{"bpy.ops.object.modifier_apply(modifier=_mod.name)" if params.get("apply") else "pass"}\n'
            f'_note("scatter_on_surface", object=_target.name, scattered=_source.name, '
            f'group=_group.name)')


def set_geometry_input(params: Dict[str, Any]) -> str:
    """Turn one number on a geometry node group that already exists.

    So a recipe can try three densities without rebuilding the graph
    three times, and so a variation layer can change a seed and get a
    different-but-repeatable result.

    Named sockets on a modifier are addressed by identifier ("Socket_2")
    rather than by label, which is not guessable -- so this looks the
    label up in the group's interface and translates. A miss lists the
    labels that exist.
    """
    value = params.get("value")
    if isinstance(value, (list, tuple)):
        parts = (list(value) + [0.0, 0.0, 0.0])[:3]
        literal = "(" + ", ".join(_num(v) for v in parts) + ")"
    elif isinstance(value, bool):
        literal = "True" if value else "False"
    else:
        literal = _num(value, 0.0)

    return (f'_target = _obj({_text(params.get("object"))})\n'
            f'_mod = _target.modifiers.get({_text(params.get("group") or params.get("modifier"))})\n'
            f'if _mod is None or _mod.type != "NODES":\n'
            f'    raise RuntimeError("%r has no geometry nodes modifier called %r -- it has: %s" % ('
            f'_target.name, {_text(params.get("group") or params.get("modifier"))}, '
            f'", ".join(m.name for m in _target.modifiers)))\n'
            f'_label = {_text(params.get("input"))}\n'
            f'_found = None\n'
            f'for _item in _mod.node_group.interface.items_tree:\n'
            f'    if getattr(_item, "in_out", "") == "INPUT" and _item.name == _label:\n'
            f'        _found = _item.identifier\n'
            f'        break\n'
            f'if _found is None:\n'
            f'    raise RuntimeError("no input called %r -- the group has: %s" % (_label, '
            f'", ".join(i.name for i in _mod.node_group.interface.items_tree '
            f'if getattr(i, "in_out", "") == "INPUT")))\n'
            f'_mod[_found] = {literal}\n'
            f'_note("set_geometry_input", object=_target.name, input=_label)')


def relax_surface(params: Dict[str, Any]) -> str:
    """Average each vertex toward its neighbours -- take the anatomy out.

    THIS IS WHAT MAKES A CUT GARMENT READ AS CLOTH. Cutting trousers
    out of a body gives a shape that fits perfectly and looks like
    bare legs, because it IS bare legs: it has kneecaps, a calf muscle
    and an ankle bone, and cloth has none of those. Rendered, the first
    pair came out as skin with a seam.

    Smoothing is the fix and it is the cheap one. A few iterations
    round the kneecap out of the knee and the tendon out of the ankle
    while leaving the silhouette, which is the difference between a
    leg and a trouser leg.

    It SHRINKS as it smooths -- averaging toward neighbours pulls a
    convex surface inward -- so this belongs before the inflate that
    pushes the garment back off the body, never after it.

    `axis` narrows it: smoothing only across x and y keeps a hem's
    height while still rounding the shape it sits on.

    `group` is what keeps the hem straight, and a cut garment always
    has the right one to hand. Smoothing averages every vertex toward
    its neighbours INCLUDING the ones on an open boundary, which have
    neighbours on one side only -- so an unweighted pass drags the
    waistband into a wavy frill. The region group a garment was cut
    with already fades to nothing at its own edges, so handing it back
    here smooths the middle hard and leaves the hem where it was cut.
    """
    axis = str(params.get("axis") or "xyz").lower()
    rounds = _int(params.get("iterations"), 6, 1, 200)
    group = params.get("group")
    return (f'_target = _active(_obj({_text(params.get("object"))}))\n'
            f'_mod = _target.modifiers.new(name="ARIA_Relax", type="SMOOTH")\n'
            f'_mod.factor = {_num(params.get("factor"), 0.5)}\n'
            f'_mod.iterations = {rounds}\n'
            + (f'if _target.vertex_groups.get({_text(group)}) is not None:\n'
               f'    _mod.vertex_group = {_text(group)}\n' if group else '')
            + f'_mod.use_x = {"x" in axis}\n'
            f'_mod.use_y = {"y" in axis}\n'
            f'_mod.use_z = {"z" in axis}\n'
            f'{"bpy.ops.object.modifier_apply(modifier=_mod.name)" if params.get("apply", True) else "pass"}\n'
            f'_note("relax_surface", object=_target.name, '
            f'iterations={rounds})')


def delete_object(params: Dict[str, Any]) -> str:
    """Remove one named object from the scene, and say that it went.

    THIS IS THE OTHER HALF OF CUTTING A GARMENT OUT OF A BODY. A pair
    of trousers is made by copying the body, keeping the part the
    trousers cover and throwing the rest away -- which means the body
    is standing in the scene when the trousers are finished. Exporting
    then gives a pair of trousers with a naked man inside them, because
    `export_fbx` writes every mesh in the file rather than a selection.

    Narrow on purpose: one object, by name, and a miss is an error
    rather than a shrug. `remove_stray_meshes` is the one that decides
    for itself what to drop, and it only does it inside a rigged file.

    Children are not orphaned -- they are re-parented to whatever the
    object itself hung from, so deleting a base does not send the hat
    that was parented to its head back to the world origin.
    """
    return (f'_doomed = _obj({_text(params.get("object"))})\n'
            f'_up = _doomed.parent\n'
            f'for _child in list(_doomed.children):\n'
            f'    _keep = _child.matrix_world.copy()\n'
            f'    _child.parent = _up\n'
            f'    _child.matrix_world = _keep\n'
            f'_gone = _doomed.name\n'
            f'bpy.data.objects.remove(_doomed, do_unlink=True)\n'
            f'_RESULT["modified"].append(_gone)\n'
            f'_note("delete_object", object=_gone, '
            f'left=len([_o for _o in bpy.data.objects if _o.type == "MESH"]))')


def duplicate_object(params: Dict[str, Any]) -> str:
    """A copy of an object, with its own mesh data.

    Its own data, not a link: a linked duplicate shares vertices with
    the original, so masking the copy would cut holes in the body the
    garment is being made from.

    Modifiers are not carried over. A copy of a rigged body that still
    had its armature modifier would deform twice.
    """
    return (f'_src = _obj({_text(params.get("object"))})\n'
            f'_name = {_named(params, "Copy")}\n'
            f'_old = bpy.data.objects.get(_name)\n'
            f'if _old is not None:\n'
            f'    bpy.data.objects.remove(_old, do_unlink=True)\n'
            f'_copy = _src.copy()\n'
            f'_copy.data = _src.data.copy()\n'
            f'_copy.name = _name\n'
            f'_copy.data.name = _name\n'
            f'_copy.modifiers.clear()\n'
            f'_copy.parent = None\n'
            f'bpy.context.collection.objects.link(_copy)\n'
            f'bpy.context.view_layer.objects.active = _copy\n'
            f'_copy.select_set(True)\n'
            f'_RESULT["created"].append(_copy.name)\n'
            f'_note("duplicate_object", source=_src.name, copy=_copy.name, '
            f'verts=len(_copy.data.vertices))')


def vertex_group_by_region(params: Dict[str, Any]) -> str:
    """Put every vertex inside a box into a named vertex group.

    How a garment says which part of a body it covers. The box is in
    WORLD metres, which is how the body was measured -- waist at 1.02,
    chest 1.26 to 1.44 -- so a recipe can name the region it means
    rather than working in some local space it cannot see.

    `soft` fades the membership over that many metres at the boundary,
    which is what stops a masked garment ending in a hard ring. Zero
    for a hem that should be a clean edge, a couple of centimetres for
    one that should not announce itself.

    Leaving a bound out leaves that side open: a vest needs a top and a
    bottom and does not care about x at all.
    """
    def bound(key, default):
        value = params.get(key)
        return "None" if value is None or str(value).strip() == "" else _num(value, default)

    return (f'_target = _obj({_text(params.get("object"))})\n'
            f'_gname = {_named(params, "ARIA_Region")}\n'
            f'_grp = _target.vertex_groups.get(_gname) or _target.vertex_groups.new(name=_gname)\n'
            f'_lo = [{bound("x_min", 0)}, {bound("y_min", 0)}, {bound("z_min", 0)}]\n'
            f'_hi = [{bound("x_max", 0)}, {bound("y_max", 0)}, {bound("z_max", 0)}]\n'
            f'_soft = {_num(params.get("soft"), 0.0)}\n'
            f'_M = _target.matrix_world\n'
            f'_in = 0\n'
            f'for _v in _target.data.vertices:\n'
            f'    _w = _M.__matmul__(_v.co)\n'
            f'    _weight = 1.0\n'
            f'    for _axis, _value in enumerate((_w.x, _w.y, _w.z)):\n'
            f'        _a, _b = _lo[_axis], _hi[_axis]\n'
            f'        if _a is not None:\n'
            f'            if _value < _a - _soft:\n'
            f'                _weight = 0.0\n'
            f'            elif _soft > 0 and _value < _a:\n'
            f'                _weight = min(_weight, (_value - (_a - _soft)) / _soft)\n'
            f'        if _b is not None:\n'
            f'            if _value > _b + _soft:\n'
            f'                _weight = 0.0\n'
            f'            elif _soft > 0 and _value > _b:\n'
            f'                _weight = min(_weight, ((_b + _soft) - _value) / _soft)\n'
            f'    if _weight > 0.0:\n'
            f'        _grp.add([_v.index], _weight, "REPLACE")\n'
            f'        _in += 1\n'
            f'_note("vertex_group_by_region", object=_target.name, group=_gname, '
            f'vertices=_in, of=len(_target.data.vertices))')


def apply_mask(params: Dict[str, Any]) -> str:
    """Throw away everything outside a vertex group.

    The cut. What is left is the part of the body the garment covers,
    in exactly the body's shape -- so a trouser leg is already a leg,
    two of them, without anybody having to model or wrap one.

    Applied by default: a live mask hides geometry rather than removing
    it, and hidden geometry still exports.
    """
    invert = "True" if params.get("invert") else "False"

    return (f'_target = _active(_obj({_text(params.get("object"))}))\n'
            f'_gname = {_text(params.get("group"))}\n'
            f'if _target.vertex_groups.get(_gname) is None:\n'
            f'    raise RuntimeError("%r has no vertex group %r -- it has: %s" % ('
            f'_target.name, _gname, ", ".join(g.name for g in _target.vertex_groups) or "none"))\n'
            f'_mod = _target.modifiers.new(name="ARIA_Mask", type="MASK")\n'
            f'_mod.vertex_group = _gname\n'
            f'_mod.invert_vertex_group = {invert}\n'
            f'_mod.threshold = {_num(params.get("threshold"), 0.05)}\n'
            f'{"bpy.ops.object.modifier_apply(modifier=_mod.name)" if params.get("apply", True) else "pass"}\n'
            f'_note("apply_mask", object=_target.name, group=_gname, '
            f'verts=len(_target.data.vertices))')


def inflate(params: Dict[str, Any]) -> str:
    """Move every vertex out along its own normal by a fixed distance.

    For getting a garment clear of the skin it was cut from. Negative
    shrinks, which is how you get a lining.

    A displace modifier with NO texture: displacement is
    (texture - mid_level) * strength, and an absent texture reads as
    1.0, so mid_level 0 and strength d moves everything exactly d.

    Not shrinkwrap, which cannot do this at all: a mesh wrapped onto
    the thing it was copied from finds every vertex already on the
    target at distance zero, where there is no direction to offset
    along. The surfaces stay coincident and z-fight into speckle.
    """
    return (f'_target = _active(_obj({_text(params.get("object"))}))\n'
            f'_mod = _target.modifiers.new(name="ARIA_Inflate", type="DISPLACE")\n'
            f'_mod.texture = None\n'
            f'_mod.mid_level = 0.0\n'
            f'_mod.strength = {_num(params.get("distance"), 0.012)}\n'
            f'_mod.direction = "NORMAL"\n'
            f'{"bpy.ops.object.modifier_apply(modifier=_mod.name)" if params.get("apply", True) else "pass"}\n'
            f'_note("inflate", object=_target.name, '
            f'distance={_num(params.get("distance"), 0.012)})')


# ======================================================
# Seeing the work
#
# THE REASON THIS SECTION EXISTS
# Every number a run reports can be right while the model is wrong.
# The library's own history is the proof: a tree standing on a spike,
# a roof floating over no walls, a pebble field scattered ten metres
# underground -- each one a run that succeeded, measured correctly and
# looked wrong the moment anybody rendered it. A layer that cannot
# render can only say that it ran. This one can show what it made.
# ======================================================

# What a preview looks like.
#   clay     -- Workbench, one matte colour, cavity shading. Form only:
#               what a sculptor turns on to judge a shape, because a
#               texture hides the bumps a shape is made of.
#   material -- EEVEE with the model's own materials, lit by a three-
#               lamp studio if the scene has no lamps of its own.
#   final    -- the scene as it stands: its engine, its lamps, its sky.
PREVIEW_LOOKS = frozenset({"clay", "material", "final"})

# Where a preview camera stands, as a direction from the model's centre.
# Models here face -Y (the base meshes and every recipe do), so "front"
# looks at a face and "right" is the MODEL's right, seen from -X.
PREVIEW_VIEWS = {
    "front": (0.0, -1.0, 0.0),
    "back": (0.0, 1.0, 0.0),
    "right": (-1.0, 0.0, 0.0),
    "left": (1.0, 0.0, 0.0),
    "top": (0.0, 0.0, 1.0),
    "bottom": (0.0, 0.0, -1.0),
    "three_quarter": (-0.62, -0.62, 0.48),
    "three_quarter_back": (0.62, 0.62, 0.48),
}
PREVIEW_VIEW_NAMES = frozenset(PREVIEW_VIEWS)
DEFAULT_PREVIEW_VIEWS = ("front", "right", "three_quarter", "back")

# Engines by the name a person uses. Blender's own identifiers moved in
# 4.2 (BLENDER_EEVEE_NEXT) and back again later, so the script picks
# whichever this build actually lists instead of trusting either.
RENDER_ENGINES = frozenset({"EEVEE", "CYCLES", "WORKBENCH"})

# Common to every template that renders. Functions, so a script that
# renders twice defines them twice and nothing worse.
_RENDER_KIT = '''
import math as _math
import os as _os


def _aria_engine(kind):
    # Asked of the setting itself: the enum's static item list names
    # only EEVEE, because the others register at startup.
    render = bpy.context.scene.render
    wanted = {"EEVEE": ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"),
              "CYCLES": ("CYCLES",),
              "WORKBENCH": ("BLENDER_WORKBENCH",)}[kind]
    current = render.engine
    for name in wanted:
        try:
            render.engine = name
        except TypeError:
            continue
        render.engine = current
        return name
    raise RuntimeError("this Blender has no %s engine" % kind)


def _aria_folder(path):
    folder = _os.path.dirname(_os.path.abspath(path))
    if folder:
        _os.makedirs(folder, exist_ok=True)
    return _os.path.abspath(path)


class _AriaNothingToRender(Exception):
    pass


class _AriaRestore:
    """Remembers settings and puts them back, whatever happened.

    A preview is a look at the work, not a change to it. A script that
    rendered and then saved must save the scene it was given -- not one
    with a new camera, a studio of lamps and a different engine.
    """

    def __init__(self):
        self._saved = []
        self._made = []

    def set(self, owner, attribute, value):
        self._saved.append((owner, attribute, getattr(owner, attribute)))
        setattr(owner, attribute, value)

    def made(self, datablock):
        self._made.append(datablock)
        return datablock

    def undo(self):
        for owner, attribute, value in reversed(self._saved):
            try:
                setattr(owner, attribute, value)
            except Exception:
                pass
        for block in reversed(self._made):
            try:
                if isinstance(block, bpy.types.Object):
                    data = block.data
                    bpy.data.objects.remove(block, do_unlink=True)
                    if data is not None and data.users == 0:
                        if isinstance(data, bpy.types.Camera):
                            bpy.data.cameras.remove(data)
                        elif isinstance(data, bpy.types.Light):
                            bpy.data.lights.remove(data)
                elif isinstance(block, bpy.types.World):
                    bpy.data.worlds.remove(block)
                elif isinstance(block, bpy.types.Image):
                    bpy.data.images.remove(block)
            except Exception:
                pass


def _aria_targets(names):
    if names:
        return [_obj(n) for n in names]
    return [o for o in bpy.context.scene.objects
            if o.type in {"MESH", "CURVE", "SURFACE", "META", "FONT"}
            and not o.hide_render and not o.name.startswith("ARIA_Preview")]


def _aria_bounds(objects):
    """Every corner of every evaluated bounding box, the centre, the radius.

    EVALUATED, because a subdivided, arrayed or mirrored object is
    bigger than its base mesh, and framing the base crops the model.
    """
    graph = bpy.context.evaluated_depsgraph_get()
    points = []
    for obj in objects:
        seen = obj.evaluated_get(graph)
        try:
            mesh = seen.to_mesh()
        except RuntimeError:
            mesh = None
        if mesh is not None and len(mesh.vertices):
            # The surface, not its box: a box's corners stand well
            # outside anything round, and framing them leaves a head
            # small in the middle of the picture. Thinned so a
            # million-vertex sculpt does not take a minute to frame.
            step = max(1, len(mesh.vertices) // 20000)
            matrix = seen.matrix_world
            points += [matrix @ mesh.vertices[i].co for i in range(0, len(mesh.vertices), step)]
        else:
            points += [seen.matrix_world @ mathutils.Vector(c) for c in seen.bound_box]
        seen.to_mesh_clear()
    if not points:
        raise RuntimeError("there is nothing to render -- no visible geometry "
                           "in the scene")
    low = mathutils.Vector([min(p[i] for p in points) for i in range(3)])
    high = mathutils.Vector([max(p[i] for p in points) for i in range(3)])
    return points, (low + high) / 2.0, max((high - low).length / 2.0, 0.001)


def _aria_aim(camera, direction, points, centre, radius, perspective):
    """Stand the camera along a direction and fit the model in frame.

    Fitted to the model's own corners, not to a sphere around it: a
    sphere around a person is mostly empty air, and a preview that is
    mostly empty air shows the face at a size nobody can judge.
    """
    direction = mathutils.Vector(direction).normalized()
    if abs(direction.z) > 0.999:
        rotation = mathutils.Euler((0.0 if direction.z > 0 else _math.pi, 0.0, 0.0))
        rotation = rotation.to_quaternion()
    else:
        rotation = (-direction).to_track_quat("-Z", "Y")
    camera.rotation_mode = "QUATERNION"
    camera.rotation_quaternion = rotation
    basis = rotation.to_matrix()
    right, up = basis.col[0], basis.col[1]

    across = [max(abs((p - centre).dot(right)), abs((p - centre).dot(up)))
              for p in points]
    if perspective:
        camera.data.type = "PERSP"
        camera.data.lens = 50.0
        camera.data.sensor_fit = "AUTO"
        slope = _math.tan(camera.data.angle / 2.0)
        distance = max((p - centre).dot(direction) + a / slope
                       for p, a in zip(points, across)) * 1.08
    else:
        camera.data.type = "ORTHO"
        camera.data.ortho_scale = max(across) * 2.0 * 1.12
        distance = radius * 3.0
    camera.location = centre + direction * distance
    camera.data.clip_start = max(0.001, distance / 5000.0)
    camera.data.clip_end = distance + radius * 6.0


def _aria_studio(restore, scene):
    """Three lamps when the scene has none, so a material has light to show.

    Only when it has none. A scene that brought its own lighting is
    telling you how it wants to be seen.
    """
    lit = any(o.type == "LIGHT" and not o.hide_render for o in scene.objects)
    if not lit:
        for name, elevation, bearing, energy, colour in (
                ("Key", 45.0, -40.0, 3.5, (1.0, 0.95, 0.88)),
                ("Fill", 20.0, 60.0, 1.0, (0.80, 0.87, 1.0)),
                ("Rim", 35.0, 160.0, 2.5, (1.0, 1.0, 1.0))):
            data = bpy.data.lights.new("ARIA_Preview_" + name, "SUN")
            data.energy = energy
            data.color = colour
            lamp = restore.made(bpy.data.objects.new("ARIA_Preview_" + name, data))
            scene.collection.objects.link(lamp)
            lamp.rotation_euler = (_math.radians(90.0 - elevation), 0.0,
                                   _math.radians(bearing))
    if scene.world is None:
        world = restore.made(bpy.data.worlds.new("ARIA_Preview_World"))
        world.use_nodes = True
        background = world.node_tree.nodes.get("Background")
        if background is not None:
            background.inputs[0].default_value = (0.35, 0.37, 0.40, 1.0)
            background.inputs[1].default_value = 0.5
        restore.set(scene, "world", world)


def _aria_clay(restore, scene):
    """Workbench as a sculptor has it: studio light, one colour, cavity."""
    shading = scene.display.shading
    restore.set(shading, "light", "STUDIO")
    restore.set(shading, "color_type", "SINGLE")
    restore.set(shading, "single_color", (0.74, 0.71, 0.67))
    restore.set(shading, "show_cavity", True)
    restore.set(shading, "cavity_type", "BOTH")
    restore.set(shading, "show_specular_highlight", True)
    restore.set(scene.view_settings, "view_transform", "Standard")
    if scene.world is None:
        restore.set(scene, "world", restore.made(bpy.data.worlds.new("ARIA_Preview_World")))
    restore.set(scene.world, "color", (0.22, 0.23, 0.25))


def _aria_sheet(paths, size, target):
    """The views side by side in one picture, left to right, top to bottom.

    One picture because one look should take in the whole model: a
    front that is right and a side that is wrong is a model that is
    wrong, and four separate files invite checking the first.
    """
    import numpy as _np
    columns = int(_math.ceil(_math.sqrt(len(paths))))
    rows = int(_math.ceil(len(paths) / float(columns)))
    gap = 4
    width, height = columns * size + (columns - 1) * gap, rows * size + (rows - 1) * gap
    canvas = _np.empty((height, width, 4), dtype=_np.float32)
    canvas[:] = (0.08, 0.08, 0.09, 1.0)
    for index, path in enumerate(paths):
        picture = bpy.data.images.load(path, check_existing=False)
        try:
            w, h = picture.size
            pixels = _np.empty(w * h * 4, dtype=_np.float32)
            picture.pixels.foreach_get(pixels)
            pixels = pixels.reshape((h, w, 4))
            pixels[..., 3] = 1.0
            row, column = divmod(index, columns)
            top = height - (row * (size + gap)) - h
            left = column * (size + gap)
            canvas[top:top + h, left:left + w] = pixels
        finally:
            bpy.data.images.remove(picture)
    sheet = bpy.data.images.new("ARIA_Preview_Sheet", width, height, alpha=False)
    try:
        sheet.pixels.foreach_set(canvas.ravel())
        sheet.filepath_raw = target
        sheet.file_format = "PNG"
        sheet.save()
    finally:
        bpy.data.images.remove(sheet)
'''


def _names(value: Any) -> str:
    """A list of object names, as a Python literal. One name is a list of one."""
    if value is None or value == "":
        return "[]"
    if isinstance(value, str):
        value = [value]
    try:
        return "[" + ", ".join(_text(item) for item in value) + "]"
    except TypeError:
        raise BadValue(f"{value!r} is not an object name or a list of them.") from None


def _views(value: Any) -> List[str]:
    if value is None or value == "" or value == []:
        return list(DEFAULT_PREVIEW_VIEWS)
    if isinstance(value, str):
        value = [part for part in value.replace(",", " ").split() if part]
    chosen = []
    for item in value:
        key = str(item).strip().lower().replace("-", "_").replace(" ", "_")
        if key not in PREVIEW_VIEWS:
            raise BadValue(f"{item!r} is not a view. Views: "
                           f"{', '.join(sorted(PREVIEW_VIEWS))}.")
        if key not in chosen:
            chosen.append(key)
    return chosen


def _engine_line(params: Dict[str, Any], restore: str = "_restore") -> str:
    """Switch engine for this render only, if one was asked for."""
    if not params.get("engine"):
        return ""
    return (f'{restore}.set(_scene.render, "engine", '
            f'_aria_engine({_choice(params.get("engine"), RENDER_ENGINES, "EEVEE")}))\n')


def render_preview(params: Dict[str, Any]) -> str:
    """Look at the model from several sides and save what it looks like.

    One PNG per view (<name>_front.png and so on) plus a sheet of all of
    them at the path given. Cameras, lamps and settings made for the
    preview are removed afterwards, so previewing between steps never
    changes what gets exported or saved.

    look: clay (form only -- for judging shape and sculpting), material
    (the model's own colours; the default) or final (the scene's own
    engine and lights). views: any of front, back, left, right, top,
    bottom, three_quarter, three_quarter_back. object/objects: frame
    only these. skip_empty: an empty scene is noted, not an error.
    """
    views = _views(params.get("views"))
    look = _choice(params.get("look"), PREVIEW_LOOKS, "material")
    size = _int(params.get("size"), 640, 64, 4096)
    sheet = "False" if params.get("sheet") is False else "True"
    transparent = "True" if params.get("transparent") else "False"
    directions = "{" + ", ".join(
        f"{view!r}: {PREVIEW_VIEWS[view]!r}" for view in views) + "}"

    return (_RENDER_KIT +
            f'_scene = bpy.context.scene\n'
            f'_target_path = _aria_folder({_text(params.get("path"))})\n'
            f'_stem = _os.path.splitext(_target_path)[0]\n'
            f'_look = {look}\n'
            f'_restore = _AriaRestore()\n'
            f'_written = []\n'
            f'try:\n'
            f'    _targets = _aria_targets({_names(params.get("objects") or params.get("object"))})\n'
            f'    if not _targets and {"True" if params.get("skip_empty") else "False"}:\n'
            f'        raise _AriaNothingToRender()\n'
            f'    _points, _centre, _radius = _aria_bounds(_targets)\n'
            f'    _chosen = {_names(params.get("objects") or params.get("object"))}\n'
            f'    if _chosen:\n'
            f'        for _o in _scene.objects:\n'
            f'            if _o.type in {{"MESH", "CURVE", "SURFACE", "META", "FONT"}} and _o.name not in _chosen:\n'
            f'                _restore.set(_o, "hide_render", True)\n'
            f'    if _look == "clay":\n'
            f'        _restore.set(_scene.render, "engine", _aria_engine("WORKBENCH"))\n'
            f'        _aria_clay(_restore, _scene)\n'
            f'    elif _look == "material":\n'
            f'        _restore.set(_scene.render, "engine", _aria_engine("EEVEE"))\n'
            f'        _aria_studio(_restore, _scene)\n'
            f'        if hasattr(_scene, "eevee"):\n'
            f'            _restore.set(_scene.eevee, "taa_render_samples", {_int(params.get("samples"), 16, 1, 4096)})\n'
            f'    ' + (_engine_line(params) or 'pass\n') +
            f'    if _scene.render.engine == "CYCLES":\n'
            f'        _restore.set(_scene.cycles, "samples", {_int(params.get("samples"), 32, 1, 4096)})\n'
            f'    _restore.set(_scene.render, "resolution_x", {size})\n'
            f'    _restore.set(_scene.render, "resolution_y", {size})\n'
            f'    _restore.set(_scene.render, "resolution_percentage", 100)\n'
            f'    _restore.set(_scene.render, "film_transparent", {transparent})\n'
            f'    _restore.set(_scene.render.image_settings, "file_format", "PNG")\n'
            f'    _restore.set(_scene.render.image_settings, "color_mode", "RGBA" if {transparent} else "RGB")\n'
            f'    _cam_data = bpy.data.cameras.new("ARIA_Preview_Camera")\n'
            f'    _cam = _restore.made(bpy.data.objects.new("ARIA_Preview_Camera", _cam_data))\n'
            f'    _scene.collection.objects.link(_cam)\n'
            f'    _restore.set(_scene, "camera", _cam)\n'
            f'    _restore.set(_scene.render, "filepath", _scene.render.filepath)\n'
            f'    for _view, _direction in {directions}.items():\n'
            f'        _aria_aim(_cam, _direction, _points, _centre, _radius, _view.startswith("three_quarter"))\n'
            f'        _file = _stem + "_" + _view + ".png"\n'
            f'        _scene.render.filepath = _file\n'
            f'        bpy.ops.render.render(write_still=True)\n'
            f'        _written.append(_file)\n'
            f'    if {sheet} and len(_written) > 1:\n'
            f'        _aria_sheet(_written, {size}, _target_path)\n'
            f'        _written.insert(0, _target_path)\n'
            f'    elif _written and _written[0] != _target_path:\n'
            f'        import shutil as _shutil\n'
            f'        _shutil.copyfile(_written[0], _target_path)\n'
            f'        _written.insert(0, _target_path)\n'
            f'except _AriaNothingToRender:\n'
            f'    _note("render_preview", skipped="the scene has nothing to render")\n'
            f'finally:\n'
            f'    _restore.undo()\n'
            f'_RESULT.setdefault("renders", []).extend(_written)\n'
            f'_note("render_preview", look=_look, views={views!r}, sheet_order="left to right, top to bottom", '
            f'path=_target_path, files=len(_written))')


def render_image(params: Dict[str, Any]) -> str:
    """Render the scene through a camera that is already there.

    The shot a person set up, as they set it up -- unlike a preview,
    which frames the model itself and throws its camera away. Size,
    engine, samples and a transparent background can be given for this
    one render; the scene's own settings are put back afterwards.
    """
    camera = params.get("camera")
    return (_RENDER_KIT +
            f'_scene = bpy.context.scene\n'
            f'_restore = _AriaRestore()\n'
            f'_path = _aria_folder({_text(params.get("path"))})\n'
            f'try:\n'
            + (f'    _restore.set(_scene, "camera", _obj({_text(camera)}))\n' if camera else '') +
            f'    if _scene.camera is None:\n'
            f'        raise RuntimeError("there is no camera to render through -- add_camera '
            f'first, or use render_preview, which brings its own")\n'
            f'    ' + (_engine_line(params) or 'pass\n') +
            (f'    _restore.set(_scene.render, "resolution_x", {_int(params.get("width"), 1920, 16, 16384)})\n'
             if params.get("width") else '') +
            (f'    _restore.set(_scene.render, "resolution_y", {_int(params.get("height"), 1080, 16, 16384)})\n'
             if params.get("height") else '') +
            (f'    _restore.set(_scene.render, "film_transparent", {"True" if params.get("transparent") else "False"})\n'
             if params.get("transparent") is not None else '') +
            (f'    if _scene.render.engine == "CYCLES":\n'
             f'        _restore.set(_scene.cycles, "samples", {_int(params.get("samples"), 64, 1, 65536)})\n'
             f'    elif hasattr(_scene, "eevee"):\n'
             f'        _restore.set(_scene.eevee, "taa_render_samples", {_int(params.get("samples"), 64, 1, 65536)})\n'
             if params.get("samples") else '') +
            f'    _restore.set(_scene.render.image_settings, "file_format", "PNG")\n'
            f'    _restore.set(_scene.render, "filepath", _path)\n'
            f'    bpy.ops.render.render(write_still=True)\n'
            f'finally:\n'
            f'    _restore.undo()\n'
            f'_RESULT.setdefault("renders", []).append(_path)\n'
            f'_note("render_image", path=_path, camera=_scene.camera.name if _scene.camera else None)')


def set_render(params: Dict[str, Any]) -> str:
    """Change how the scene renders, and keep it that way.

    Unlike the render actions, this is meant to last: it is saved with
    the file, so a scene set up for Cycles at 4K stays set up.
    """
    lines = [_RENDER_KIT, '_scene = bpy.context.scene\n']
    if params.get("engine"):
        lines.append(f'_scene.render.engine = _aria_engine('
                     f'{_choice(params.get("engine"), RENDER_ENGINES, "EEVEE")})\n')
    if params.get("width"):
        lines.append(f'_scene.render.resolution_x = {_int(params.get("width"), 1920, 16, 16384)}\n')
    if params.get("height"):
        lines.append(f'_scene.render.resolution_y = {_int(params.get("height"), 1080, 16, 16384)}\n')
    if params.get("percentage"):
        lines.append(f'_scene.render.resolution_percentage = {_int(params.get("percentage"), 100, 1, 1000)}\n')
    if params.get("transparent") is not None:
        lines.append(f'_scene.render.film_transparent = {"True" if params.get("transparent") else "False"}\n')
    if params.get("samples"):
        samples = _int(params.get("samples"), 64, 1, 65536)
        lines.append(f'_scene.cycles.samples = {samples}\n'
                     f'if hasattr(_scene, "eevee"):\n'
                     f'    _scene.eevee.taa_render_samples = {samples}\n')
    if params.get("frame_start") is not None:
        lines.append(f'_scene.frame_start = {_int(params.get("frame_start"), 1, 0, 1_000_000)}\n')
    if params.get("frame_end") is not None:
        lines.append(f'_scene.frame_end = {_int(params.get("frame_end"), 250, 0, 1_000_000)}\n')
    if params.get("fps"):
        lines.append(f'_scene.render.fps = {_int(params.get("fps"), 24, 1, 240)}\n')
    lines.append('_note("set_render", engine=_scene.render.engine, '
                 'width=_scene.render.resolution_x, height=_scene.render.resolution_y)')
    return "".join(lines)


def add_camera(params: Dict[str, Any]) -> str:
    """A camera that stays in the scene, aimed at an object or a point.

    `target` names an object to look at, `look_at` gives a point. With
    neither it faces straight down -Y's opposite way: toward the front
    of a model standing at the origin. It becomes the scene's camera
    unless `active` is false.
    """
    target = params.get("target")
    aim = (f'_point = _obj({_text(target)}).matrix_world.translation.copy()\n' if target else
           f'_point = mathutils.Vector({_vector(params.get("look_at"), (0.0, 0.0, 0.9))})\n')
    ortho = bool(params.get("ortho"))
    return (f'_name = {_named(params, "Camera")}\n'
            f'_data = bpy.data.cameras.new(_name)\n'
            f'_data.lens = {_num(params.get("lens"), 50.0)}\n'
            + (f'_data.type = "ORTHO"\n_data.ortho_scale = {_num(params.get("ortho_scale"), 4.0)}\n'
               if ortho else '') +
            f'_cam = bpy.data.objects.new(_name, _data)\n'
            f'bpy.context.scene.collection.objects.link(_cam)\n'
            f'_cam.location = {_vector(params.get("location"), (0.0, -6.0, 1.4))}\n'
            + aim +
            f'_cam.rotation_mode = "QUATERNION"\n'
            f'_cam.rotation_quaternion = (_point - _cam.location).to_track_quat("-Z", "Y")\n'
            + ('' if params.get("active") is False else 'bpy.context.scene.camera = _cam\n') +
            f'_RESULT["created"].append(_cam.name)\n'
            f'_note("add_camera", name=_cam.name, lens=_data.lens)')


def describe_scene(params: Dict[str, Any]) -> str:
    """Everything in the file, as data. Changes nothing.

    What a person gets by glancing at the outliner and the properties
    panel: every object with where it is, how big, what it is made of
    and what is stacked on it. Without this, working on a file ARIA did
    not build in this very run means guessing its names.
    """
    return ('import math as _math\n'
            '_graph = bpy.context.evaluated_depsgraph_get()\n'
            '_objects = []\n'
            'for _o in sorted(bpy.context.scene.objects, key=lambda o: o.name):\n'
            '    _entry = {"name": _o.name, "type": _o.type,\n'
            '              "location": [round(v, 4) for v in _o.matrix_world.translation],\n'
            '              "rotation": [round(_math.degrees(v), 2) for v in _o.matrix_world.to_euler()],\n'
            '              "scale": [round(v, 4) for v in _o.scale],\n'
            '              "dimensions": [round(v, 4) for v in _o.dimensions],\n'
            '              "parent": _o.parent.name if _o.parent else None,\n'
            '              "hidden": bool(_o.hide_render)}\n'
            '    if _o.modifiers:\n'
            '        _entry["modifiers"] = [m.type + ":" + m.name for m in _o.modifiers]\n'
            '    if _o.type == "MESH":\n'
            '        _seen = _o.evaluated_get(_graph).to_mesh()\n'
            '        _entry.update(vertices=len(_o.data.vertices), faces=len(_o.data.polygons),\n'
            '                      evaluated_faces=len(_seen.polygons),\n'
            '                      materials=[m.name for m in _o.data.materials if m],\n'
            '                      uv_layers=[u.name for u in _o.data.uv_layers],\n'
            '                      vertex_groups=[g.name for g in _o.vertex_groups][:40],\n'
            '                      shape_keys=[k.name for k in _o.data.shape_keys.key_blocks] if _o.data.shape_keys else [])\n'
            '        _o.evaluated_get(_graph).to_mesh_clear()\n'
            '    elif _o.type == "ARMATURE":\n'
            '        _entry["bones"] = [b.name for b in _o.data.bones][:200]\n'
            '        _entry["action"] = _o.animation_data.action.name if _o.animation_data and _o.animation_data.action else None\n'
            '    elif _o.type == "CAMERA":\n'
            '        _entry.update(lens=_o.data.lens, camera_type=_o.data.type)\n'
            '    elif _o.type == "LIGHT":\n'
            '        _entry.update(light_type=_o.data.type, energy=_o.data.energy)\n'
            '    _objects.append(_entry)\n'
            '_s = bpy.context.scene\n'
            '_RESULT["scene"] = {\n'
            '    "objects": _objects,\n'
            '    "camera": _s.camera.name if _s.camera else None,\n'
            '    "engine": _s.render.engine,\n'
            '    "resolution": [_s.render.resolution_x, _s.render.resolution_y],\n'
            '    "frames": [_s.frame_start, _s.frame_end, _s.frame_current],\n'
            '    "materials": sorted(m.name for m in bpy.data.materials),\n'
            '    "images": sorted(i.name for i in bpy.data.images),\n'
            '    "actions": sorted(a.name for a in bpy.data.actions),\n'
            '    "node_groups": sorted(g.name for g in bpy.data.node_groups),\n'
            '}\n'
            '_note("describe_scene", objects=len(_objects))')


# ======================================================
# Sculpting, by hand
#
# WHY NOT BLENDER'S OWN BRUSHES
# Measured on 5.0.1 in --background (see sculpt_brush): a brush stroke
# refuses without a window, the mesh filters crash Blender outright,
# and the brush cannot even be chosen. So the brushes are here, as
# arithmetic on the vertices -- which is all a sculpt brush ever was:
# find the vertices inside a sphere, weight them by a falloff, move
# them. Done on the mesh data with numpy it runs headless, it is exact,
# and the same stroke gives the same result every time.
#
# WHAT IT NEEDS FROM THE MESH
# Vertices to move. A brush on a 500-face sphere moves a handful of
# points and makes facets, not form -- the same as in Blender, where a
# sculptor subdivides or remeshes first. voxel_remesh (even density,
# any shape) or apply_subdivision with apply=True before sculpting.
# The brush works on the mesh itself, under any modifiers, so a live
# Subdivision on top smooths what was sculpted rather than adding to it.
# ======================================================

SCULPT_STROKE_BRUSHES = frozenset({
    "draw", "clay", "inflate", "crease", "pinch", "flatten", "fill",
    "scrape", "smooth", "grab",
})

# How strongly a vertex is moved against how far it is from the brush
# centre, 0 at the centre to 1 at the rim.
SCULPT_FALLOFFS = frozenset({"smooth", "sphere", "linear", "sharp", "constant"})

# Views a stroke can be placed on. Orthographic ones only: on those a
# point in the picture is a straight line into the scene, so "40%
# across, 30% down the front view" names one place on the surface.
SCULPT_VIEWS = frozenset({"front", "back", "left", "right", "top", "bottom"})

_SCULPT_KIT = '''
import numpy as _np
from mathutils.bvhtree import BVHTree as _BVHTree


def _aria_falloff(t, kind):
    t = _np.clip(t, 0.0, 1.0)
    if kind == "sphere":
        return _np.sqrt(1.0 - t * t)
    if kind == "linear":
        return 1.0 - t
    if kind == "sharp":
        return (1.0 - t) ** 2
    if kind == "constant":
        return _np.ones_like(t)
    s = 1.0 - t                                  # smooth: ease in and out
    return s * s * (3.0 - 2.0 * s)


class _AriaSculpt:
    """One mesh's vertices, held as arrays while a stroke works on them."""

    def __init__(self, obj):
        if obj.type != "MESH":
            raise RuntimeError("%r is a %s -- only meshes can be sculpted" % (obj.name, obj.type))
        if obj.data.shape_keys:
            raise RuntimeError("%r has shape keys; sculpting the base under them would "
                               "tear the keys away from it" % obj.name)
        self.obj = obj
        self.mesh = obj.data
        count = len(self.mesh.vertices)
        if count == 0:
            raise RuntimeError("%r has no vertices" % obj.name)
        self.co = _np.empty(count * 3, dtype=_np.float64)
        self.mesh.vertices.foreach_get("co", self.co)
        self.co = self.co.reshape(-1, 3)
        self.start = self.co.copy()
        edges = _np.empty(len(self.mesh.edges) * 2, dtype=_np.int64)
        self.mesh.edges.foreach_get("vertices", edges)
        self.edges = edges.reshape(-1, 2)
        self.faces = [tuple(p.vertices) for p in self.mesh.polygons]
        self.refresh_normals()

    def refresh_normals(self):
        self.write()
        self.mesh.update()
        normals = _np.empty(len(self.mesh.vertices) * 3, dtype=_np.float64)
        self.mesh.vertex_normals.foreach_get("vector", normals)
        self.normals = normals.reshape(-1, 3)

    def write(self):
        self.mesh.vertices.foreach_set("co", self.co.ravel())

    def tree(self):
        return _BVHTree.FromPolygons([tuple(v) for v in self.co], self.faces)

    def neighbour_average(self):
        total = _np.zeros_like(self.co)
        count = _np.zeros(len(self.co))
        a, b = self.edges[:, 0], self.edges[:, 1]
        _np.add.at(total, a, self.co[b])
        _np.add.at(total, b, self.co[a])
        _np.add.at(count, a, 1.0)
        _np.add.at(count, b, 1.0)
        count[count == 0] = 1.0
        return total / count[:, None]

    def dab(self, centre, radius, strength, brush, falloff, front_only, offset=None):
        """One touch of the brush. Returns how many vertices it moved."""
        delta = self.co - centre
        distance = _np.sqrt((delta * delta).sum(axis=1))
        inside = _np.nonzero(distance < radius)[0]
        if not len(inside):
            return 0
        weight = _aria_falloff(distance[inside] / radius, falloff)
        normals = self.normals[inside]

        # The area normal: which way "out" is here, averaged over the
        # brush, so a bump grows off the surface and not off one vertex.
        area = (normals * weight[:, None]).sum(axis=0)
        length = _np.linalg.norm(area)
        if length < 1e-12:
            return 0
        area = area / length
        if front_only:
            # The other side of a thin part -- the back of a lip, the
            # far wall of a nostril -- faces away from the brush. A
            # sculptor's brush never reaches it; neither does this one.
            weight = weight * (normals @ area > 0.0)

        points = self.co[inside]
        depth = radius * 0.1 * strength      # one dab's full push
        plane_point = (points * weight[:, None]).sum(axis=0) / max(weight.sum(), 1e-12)
        height = (points - plane_point) @ area   # above (+) or below (-) the area plane

        if brush == "draw":
            move = area[None, :] * (weight * depth)[:, None]
        elif brush == "inflate":
            move = normals * (weight * depth)[:, None]
        elif brush == "clay":
            # Lay a layer on: push out like draw, but ease off where the
            # surface already stands proud of the area plane, so dabs
            # fill the low ground and build a soft, even layer. An
            # earlier version lifted everything to a fixed plane, which
            # measured as a flat plateau with a hard beaded rim -- a
            # brow like a plank laid on the face.
            reach = max(abs(depth) * 2.0, 1e-12)
            ease = _np.clip(1.0 - (height * _np.sign(depth)) / reach, 0.0, 1.0)
            move = area[None, :] * (weight * depth * ease)[:, None]
        elif brush in ("flatten", "fill", "scrape"):
            pull = -height
            if brush == "fill":
                pull = _np.clip(pull, 0.0, None)
            elif brush == "scrape":
                pull = _np.clip(pull, None, 0.0)
            move = area[None, :] * (weight * pull * min(abs(strength), 1.0))[:, None]
        elif brush == "pinch":
            toward = centre - points
            toward = toward - (toward @ area)[:, None] * area[None, :]
            move = toward * (weight * 0.3 * strength)[:, None]
        elif brush == "crease":
            toward = centre - points
            toward = toward - (toward @ area)[:, None] * area[None, :]
            move = (toward * (weight * 0.3 * abs(strength))[:, None]
                    - area[None, :] * (weight * depth)[:, None])
        elif brush == "smooth":
            average = self.neighbour_average()[inside]
            move = (average - points) * (weight * min(abs(strength), 1.0))[:, None]
        elif brush == "grab":
            move = _np.asarray(offset)[None, :] * weight[:, None]
        else:
            raise RuntimeError("no brush called %r" % brush)

        self.co[inside] = points + move
        return int((_np.abs(move).sum(axis=1) > 1e-12).sum())


def _aria_resample(points, spacing):
    """Dabs along a path, `spacing` apart, the way a stroke lays them."""
    if len(points) < 2 or spacing <= 0:
        return [mathutils.Vector(p) for p in points]
    dabs = [mathutils.Vector(points[0])]
    carry = 0.0
    for a, b in zip(points[:-1], points[1:]):
        a, b = mathutils.Vector(a), mathutils.Vector(b)
        length = (b - a).length
        travelled = spacing - carry
        while travelled <= length:
            dabs.append(a.lerp(b, travelled / length))
            travelled += spacing
        carry = length - (travelled - spacing)
    if (dabs[-1] - mathutils.Vector(points[-1])).length > spacing * 0.25:
        dabs.append(mathutils.Vector(points[-1]))
    return dabs


def _aria_view_point(obj, framing, view, u, v):
    """A point in an orthographic preview picture, as a place on the surface.

    The camera is rebuilt exactly as render_preview built it -- same
    framing, same fit -- so (u, v) read off that picture, 0-1 across
    and 0-1 down, lands where the picture showed it.
    """
    direction = mathutils.Vector(__ARIA_VIEWS__[view]).normalized()
    points, centre, radius = _aria_bounds(framing)
    if abs(direction.z) > 0.999:
        rotation = mathutils.Euler((0.0 if direction.z > 0 else _math.pi, 0.0, 0.0)).to_quaternion()
    else:
        rotation = (-direction).to_track_quat("-Z", "Y")
    basis = rotation.to_matrix()
    right, up = basis.col[0], basis.col[1]
    across = max(max(abs((p - centre).dot(right)), abs((p - centre).dot(up))) for p in points)
    scale = across * 2.0 * 1.12
    origin = (centre + right * ((u - 0.5) * scale) + up * ((0.5 - v) * scale)
              + direction * (radius * 3.0))
    inverse = obj.matrix_world.inverted()
    local_origin = inverse @ origin
    local_direction = (inverse.to_3x3() @ -direction).normalized()
    hit, location, normal, index = obj.ray_cast(local_origin, local_direction)
    if not hit:
        raise RuntimeError("the %s view at (%.3f, %.3f) is empty space -- there is no %r "
                           "there to sculpt" % (view, u, v, obj.name))
    return obj.matrix_world @ location
'''.replace("__ARIA_VIEWS__", repr(PREVIEW_VIEWS))


def sculpt_ready(params: Dict[str, Any]) -> str:
    """Get a mesh ready to sculpt: rounded, then remeshed to an even density.

    What a sculptor does before the first stroke, in one step. A
    primitive is the wrong starting point as it stands -- a UV sphere's
    512 flat faces survive a voxel remesh as 512 flat patches, which is
    measured: the first sculpted head here was a pleated ball. So it is
    rounded first (two applied Catmull-Clark levels), then rebuilt as an
    even grid the brushes can move.

    detail: voxels across the object's longest side -- 150 is a head
    with room for a brow and a mouth; 300 takes wrinkles and costs four
    times the faces. smooth=false keeps hard edges (a cube stays a cube,
    at the price of any faceting it had).
    """
    detail = _num(params.get("detail"), 150.0)
    smooth = "False" if params.get("smooth") is False else "True"
    return (f'_target = _active(_obj({_text(params.get("object"))}))\n'
            f'if _target.type != "MESH":\n'
            f'    raise RuntimeError("%r is not a mesh" % _target.name)\n'
            f'_before = len(_target.data.polygons)\n'
            f'if {smooth} and _before < 20000:\n'
            f'    _round = _target.modifiers.new(name="ARIA_Round", type="SUBSURF")\n'
            f'    _round.levels = 2\n'
            f'    _round.render_levels = 2\n'
            f'    bpy.ops.object.modifier_move_to_index(modifier=_round.name, index=0)\n'
            f'    bpy.ops.object.modifier_apply(modifier=_round.name)\n'
            f'_corners = [mathutils.Vector(c) for c in _target.bound_box]\n'
            f'_longest = max(max(c[i] for c in _corners) - min(c[i] for c in _corners) for i in range(3))\n'
            f'_target.data.remesh_voxel_size = max(_longest / max({detail}, 1.0), 1e-5)\n'
            f'_target.data.remesh_voxel_adaptivity = 0.0\n'
            f'bpy.ops.object.voxel_remesh()\n'
            f'bpy.ops.object.shade_smooth()\n'
            f'_RESULT["modified"].append(_target.name)\n'
            f'_note("sculpt_ready", object=_target.name, faces_before=_before, '
            f'faces=len(_target.data.polygons), voxel=round(_target.data.remesh_voxel_size, 5))')


def _stroke_points(value: Any, view: Any) -> str:
    """The stroke's path as a literal: 3D points, or 2D points on a view."""
    try:
        points = [list(point) for point in (value or [])]
    except TypeError:
        raise BadValue(f"{value!r} is not a list of points.") from None
    if not points:
        raise BadValue("a stroke needs at least one point.")
    sizes = {len(point) for point in points}
    if sizes == {3}:
        return "[" + ", ".join(_vector(point) for point in points) + "]"
    if sizes == {2}:
        if not view:
            raise BadValue("two-number points are places on a picture -- say which view "
                           f"they are on ({', '.join(sorted(SCULPT_VIEWS))}).")
        return "[" + ", ".join(f"({_num(p[0])}, {_num(p[1])})" for p in points) + "]"
    raise BadValue("points are all [x, y, z] in the scene, or all [u, v] on a view.")


def sculpt_stroke(params: Dict[str, Any]) -> str:
    """Sculpt one stroke: a brush dragged along a path on a mesh's surface.

    brush: draw (push out; negative strength pushes in), clay (lay on a
    flat layer), inflate, crease (a sharp valley), pinch (pull toward
    the line), flatten, fill (only raise the low), scrape (only cut the
    high), smooth, grab (carry a region by `offset`).

    points: the path. Either [x, y, z] in the scene -- snapped onto the
    surface -- or [u, v] on an orthographic preview picture (0-1 across,
    0-1 down) with `view` naming which: front, back, left, right, top,
    bottom. `frame` must match what that preview framed: "scene" (its
    default) or "object".

    radius in metres, strength 0-1 (negative inverts), falloff smooth /
    sphere / linear / sharp / constant -- keep grab on smooth: sphere's
    hard rim leaves a visible shell line around what was pulled, spacing between dabs as a
    fraction of the radius, mirror "X" to sculpt both sides of a figure
    at once, front_only (default true) to leave the far side of thin
    parts alone. Needs a dense mesh -- sculpt_ready first.
    """
    brush = _choice(params.get("brush"), SCULPT_STROKE_BRUSHES, "draw")
    falloff = _choice(params.get("falloff"), SCULPT_FALLOFFS, "smooth")
    view = params.get("view")
    view_literal = _choice(view, SCULPT_VIEWS, "front") if view else "None"
    points = _stroke_points(params.get("points"), view)
    mirror = str(params.get("mirror") or "").strip().upper()
    if mirror not in ("", "X", "Y", "Z"):
        raise BadValue(f"{params.get('mirror')!r} is not a mirror axis. Use X, Y or Z.")
    mirror_index = {"X": 0, "Y": 1, "Z": 2}.get(mirror, -1)
    frame = str(params.get("frame") or "scene").strip().lower()
    if frame not in ("scene", "object"):
        raise BadValue(f"{params.get('frame')!r} is not a framing. Use scene or object.")
    front_only = "False" if params.get("front_only") is False else "True"
    strength = _num(params.get("strength"), 0.5)
    if params.get("invert"):
        strength = f"-({strength})"

    return (_RENDER_KIT + _SCULPT_KIT +
            f'_target = _obj({_text(params.get("object"))})\n'
            f'_sculpt = _AriaSculpt(_target)\n'
            f'_radius_world = {_num(params.get("radius"), 0.05)}\n'
            f'if _radius_world <= 0:\n'
            f'    raise RuntimeError("a brush needs a radius above zero")\n'
            f'_scale = sum(abs(s) for s in _target.matrix_world.to_scale()) / 3.0\n'
            f'_radius = _radius_world / max(_scale, 1e-9)\n'
            f'_inverse = _target.matrix_world.inverted()\n'
            f'_view = {view_literal}\n'
            f'_raw = {points}\n'
            f'if _view is not None:\n'
            f'    _framing = [_target] if {frame!r} == "object" else _aria_targets([])\n'
            f'    _world = [_aria_view_point(_target, _framing, _view, p[0], p[1]) for p in _raw]\n'
            f'else:\n'
            f'    _world = [mathutils.Vector(p) for p in _raw]\n'
            f'_tree = _sculpt.tree()\n'
            f'_path = []\n'
            f'for _p in _world:\n'
            f'    _near = _tree.find_nearest(_inverse @ _p)\n'
            f'    _path.append(_near[0] if _near[0] is not None else _inverse @ _p)\n'
            f'_dabs = _aria_resample([tuple(p) for p in _path], _radius * {_num(params.get("spacing"), 0.25)})\n'
            f'_offset = _inverse.to_3x3() @ mathutils.Vector({_vector(params.get("offset"))})\n'
            f'_moved = 0\n'
            f'_mirror = {mirror_index}\n'
            f'_brush = {brush}\n'
            f'_centres = [(_d, _offset) for _d in _dabs]\n'
            f'if _mirror >= 0:\n'
            f'    _flip = lambda v: mathutils.Vector([-c if i == _mirror else c for i, c in enumerate(v)])\n'
            f'    _centres += [(_flip(_d), _flip(_offset)) for _d in _dabs]\n'
            f'if _brush == "grab":\n'
            f'    # A grab carries the region once, from where it was taken;\n'
            f'    # repeated along a path it would drag the same vertices twice.\n'
            f'    _centres = [_centres[0]] + ([_centres[len(_dabs)]] if _mirror >= 0 else [])\n'
            f'for _index, (_centre, _off) in enumerate(_centres):\n'
            f'    _moved += _sculpt.dab(_np.array(_centre), _radius, {strength}, _brush, '
            f'{falloff}, {front_only}, _np.array(_off))\n'
            f'    if _index % 4 == 3:\n'
            f'        _sculpt.refresh_normals()\n'
            f'_sculpt.write()\n'
            f'_sculpt.mesh.update()\n'
            f'_shift = _np.sqrt(((_sculpt.co - _sculpt.start) ** 2).sum(axis=1))\n'
            f'_RESULT["modified"].append(_target.name)\n'
            f'_note("sculpt_stroke", object=_target.name, brush=_brush, dabs=len(_centres), '
            f'vertices_moved=int((_shift > 1e-9).sum()), '
            f'largest_move=round(float(_shift.max()) * _scale, 5), '
            f'modifiers=[m.type for m in _target.modifiers])')


TEMPLATES = {
    "apply_transforms": apply_transforms,
    "remove_stray_meshes": remove_stray_meshes,
    "measure_rig": measure_rig,
    "add_cone": add_cone,
    "load_image": load_image,
    "add_light": add_light,
    "set_world": set_world,
    "new_image": new_image,
    "save_image": save_image,
    "bake_texture": bake_texture,
    "set_shader_node": set_shader_node,
    "quad_remesh": quad_remesh,
    "voxel_remesh": voxel_remesh,
    "stamp_detail": stamp_detail,
    "edge_wear": edge_wear,
    "cavity_mask": cavity_mask,
    "set_texture": set_texture,
    "bind_to_bone": bind_to_bone,
    "smooth_shade": smooth_shade,
    "origin_to_geometry": origin_to_geometry,
    "add_ik_constraint": add_ik_constraint,
    "set_interpolation": set_interpolation,
    "import_model": import_model,
    "append_from_blend": append_from_blend,
    "measure_mesh": measure_mesh,
    "remove_loose": remove_loose,
    "recalculate_normals": recalculate_normals,
    "merge_by_distance": merge_by_distance,
    "scale_to_height": scale_to_height,
    "origin_to_floor": origin_to_floor,
    # modelling
    "add_cube": add_cube,
    "add_sphere": add_sphere,
    "add_cylinder": add_cylinder,
    "add_plane": add_plane,
    "add_torus": add_torus,
    # modifiers
    "apply_subdivision": apply_subdivision,
    "apply_bevel": apply_bevel,
    "apply_mirror": apply_mirror,
    "apply_array": apply_array,
    "apply_boolean": apply_boolean,
    "apply_solidify": apply_solidify,
    "apply_shrinkwrap": apply_shrinkwrap,
    "apply_simple_deform": apply_simple_deform,
    "apply_cast": apply_cast,
    "apply_lattice": apply_lattice,
    "move_lattice_point": move_lattice_point,
    "join_objects": join_objects,
    "duplicate_object": duplicate_object,
    "delete_object": delete_object,
    "relax_surface": relax_surface,
    "inflate": inflate,
    "vertex_group_by_region": vertex_group_by_region,
    "apply_mask": apply_mask,
    "add_geometry_nodes": add_geometry_nodes,
    "scatter_on_surface": scatter_on_surface,
    "set_geometry_input": set_geometry_input,
    "apply_decimate": apply_decimate,
    # transforms
    "move": move,
    "rotate": rotate,
    "scale": scale,
    "parent": parent,
    # uv
    "smart_uv_project": smart_uv_project,
    "mark_seams": mark_seams,
    "unwrap": unwrap,
    # materials
    "create_material": create_material,
    "assign_material": assign_material,
    # rigging
    "create_armature": create_armature,
    "add_bone": add_bone,
    "parent_mesh_to_armature": parent_mesh_to_armature,
    "auto_weights": auto_weights,
    # skinning
    "normalize_weights": normalize_weights,
    "assign_vertex_group": assign_vertex_group,
    # animation
    "insert_keyframe": insert_keyframe,
    "set_pose": set_pose,
    "set_frame": set_frame,
    "bake_animation": bake_animation,
    # sculpting
    "sculpt_brush": sculpt_brush,
    "sculpt_stroke": sculpt_stroke,
    "sculpt_ready": sculpt_ready,
    "enable_dyntopo": enable_dyntopo,
    "apply_multires": apply_multires,
    # export
    "export_fbx": export_fbx,
    "export_glb": export_glb,
    "export_obj": export_obj,
    # seeing the work
    "render_preview": render_preview,
    "render_image": render_image,
    "set_render": set_render,
    "add_camera": add_camera,
    "describe_scene": describe_scene,
    # scene
    "clear_scene": clear_scene,
    "save_file": save_file,
}


def known_actions() -> List[str]:
    return sorted(TEMPLATES)


def _read_parameters(function, names: List[str], seen: set) -> None:
    """Every params.get() in a function, and in the helpers it hands params to."""
    import inspect
    import re

    if function in seen:
        return
    seen.add(function)
    source = inspect.getsource(function)
    for found in re.findall(r'params\.get\("([a-z_]+)"', source):
        if found not in names:
            names.append(found)
    for helper in re.findall(r'\b(_[a-z_]+)\(params\b', source):
        target = globals().get(helper)
        if callable(target):
            _read_parameters(target, names, seen)


def parameters(action: str) -> List[str]:
    """The parameter names an action reads, in the order it reads them.

    Read from the template's own source -- and the helpers it passes
    params to -- so it cannot drift from what the template does. A
    caller spelling one wrong can then be told, rather than having the
    value dropped: every template reads with params.get(), which is
    silent about names it never asks for.
    """
    template = TEMPLATES.get(action)
    if template is None:
        raise UnknownAction(f"{action!r} is not an action.")
    names: List[str] = []
    _read_parameters(template, names, set())
    return names


def describe_action(action: str) -> str:
    """What an action does and what it takes, for a person or a model."""
    import inspect

    template = TEMPLATES.get(action)
    if template is None:
        raise UnknownAction(f"{action!r} is not an action.")
    doc = inspect.getdoc(template) or "(no description)"
    return f"{action}({', '.join(parameters(action))})\n\n{doc}"


class UnknownAction(ValueError):
    """An action name no template implements."""


def build_script(actions: Sequence[Dict[str, Any]]) -> str:
    """One runnable script from a list of actions.

    Each action is {"action": name, "params": {...}}. An unknown name
    raises here rather than producing a script that fails halfway --
    a partial run leaves a scene nobody asked for.
    """
    if not actions:
        raise UnknownAction("no actions to perform")

    body: List[str] = []
    for index, entry in enumerate(actions):
        name = str((entry or {}).get("action") or "")
        template = TEMPLATES.get(name)
        if template is None:
            raise UnknownAction(
                f"step {index + 1} asks for {name!r}, which is not an action. "
                f"Known: {', '.join(known_actions())}")
        params = (entry or {}).get("params") or {}
        body.append(f"# --- step {index + 1}: {name}\n{template(params)}")

    return (PREAMBLE + "\n\n" + "\n\n".join(body)
            + EPILOGUE.format(open=RESULT_OPEN, close=RESULT_CLOSE))
