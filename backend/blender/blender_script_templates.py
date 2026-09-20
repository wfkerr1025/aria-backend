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
    "known_actions",
]

# The generated script prints its findings between these, so the runner
# can pick a result out of Blender's very chatty stdout. Same technique
# unity_ops uses against the Editor, for the same reason.
RESULT_OPEN = "###ARIA_BLENDER_RESULT_OPEN###"
RESULT_CLOSE = "###ARIA_BLENDER_RESULT_CLOSE###"

# What a modifier may be. Checked against the enum this Blender build
# actually accepts, rather than trusted from the caller.
MODIFIER_TYPES = frozenset({
    "SUBSURF", "BEVEL", "MIRROR", "ARRAY", "BOOLEAN", "SOLIDIFY",
    "DECIMATE", "MULTIRES", "ARMATURE",
})

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
    """Make one object the active selection, which most operators need."""
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
    return (f'{_export_preamble(params)}\n'
            f'_path = {_text(params.get("path"))}\n'
            f'bpy.ops.export_scene.fbx(filepath=_path, use_selection=_use_selection, '
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
    """
    smooth = params.get("smooth", True)
    call = "shade_smooth" if smooth or smooth is None else "shade_flat"

    return (f'_target = _active(_obj({_text(params.get("object") or params.get("target"))}))\n'
            f'bpy.ops.object.{call}()\n'
            f'_note("smooth_shade", object=_target.name, smooth={"True" if call == "shade_smooth" else "False"})')


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


TEMPLATES = {
    "apply_transforms": apply_transforms,
    "remove_stray_meshes": remove_stray_meshes,
    "measure_rig": measure_rig,
    "add_cone": add_cone,
    "load_image": load_image,
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
    "enable_dyntopo": enable_dyntopo,
    "apply_multires": apply_multires,
    # export
    "export_fbx": export_fbx,
    "export_glb": export_glb,
    "export_obj": export_obj,
    # scene
    "clear_scene": clear_scene,
    "save_file": save_file,
}


def known_actions() -> List[str]:
    return sorted(TEMPLATES)


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
