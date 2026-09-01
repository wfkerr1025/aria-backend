"""ARIA Lite - turning what somebody said into Blender actions.

"Create a car" has to become a list of actions. There are two ways to
do that and only one of them is safe.

The unsafe one is to ask a language model to write the bpy. That was
measured on this machine: asked to drive Blender, phi-3-mini produced
`def main:` -- not valid Python -- and invented an operator that does
not exist. A model that cannot run something does not decline; it
writes what such a request usually looks like.

So this module recognises phrases from a vocabulary it owns, and maps
them to actions the templates already know how to generate. Every
number it emits it either read from the sentence or chose itself.

BLENDER HAS TO BE NAMED
-----------------------
Nothing here fires unless the sentence says Blender: "in Blender",
"with Blender", "using Blender", or "Blender, ..." to open. "Make a
car" on its own returns None.

This is the difference between a feature and a hazard. "Make a car"
could mean a 3D model, a game object, a drawing, a story, or nothing
in particular, and acting on it means starting a subprocess and
writing files because somebody used a common verb. Naming the tool
costs the person two words and removes the guess entirely.

Naming a DIFFERENT asset tool -- Ludo.ai -- returns None even if
Blender is also named, because an ambiguous sentence is a reason to
ask rather than to pick. Unity is not treated as a competing tool:
"export it from Blender to Unity" is a Blender request with a
destination.

ASKING VERSUS INSTRUCTING
-------------------------
"Can you make a car in Blender?" builds a car. It is a request in a
polite shape, and once the tool gate has done its job there is no
ambiguity left for a question guard to protect against -- the person
named Blender. Refusing them for being polite would be friction with
no safety behind it.

What still does not build is a request for an EXPLANATION, and those
are separated by what they ask for rather than by how they open:

    "can you make a car in Blender?"          -> builds
    "can you tell me how to make a car in
     Blender?"                                -> None
    "how do I make a car in Blender?"         -> None

So there are two guards, not one. A sentence that OPENS with an
interrogative about the world ("how", "what", "is") is answered; and
a sentence that asks to be taught ("tell me", "explain", "how to",
"the best way") is answered wherever that phrase sits.

WHAT THIS IS NOT
----------------
It is not understanding. It knows the nouns in RECIPES and the verbs
in OPERATIONS and nothing else, and when a sentence is outside that it
returns None so the turn goes to a model that can at least talk about
it. A wrong match is worse than no match: no match is a conversation,
a wrong match is twenty minutes of somebody wondering why their car is
a torus.

THE OTHER HALF OF THE SAME RULE
-------------------------------
`cli_programs` reads a typed command line and refuses sentences. This
reads a sentence and refuses to invent. Between them, a model never
gets to be the thing that decides what runs.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

__all__ = [
    "COLORS",
    "OPERATIONS",
    "RECIPES",
    "describe",
    "map_text",
    "names_another_tool",
    "names_blender",
    "recipe_names",
    "wants_something_built",
]


# ======================================================
# Leaf values read out of the sentence
# ======================================================

# Named colours, because "make it red" is how people say it. Linear
# values -- Blender's base_color is not sRGB, and a naive 1.0 red
# renders as a glowing traffic cone.
COLORS = {
    "red": (0.60, 0.05, 0.05), "green": (0.08, 0.40, 0.10),
    "blue": (0.05, 0.15, 0.55), "yellow": (0.75, 0.60, 0.05),
    "orange": (0.75, 0.25, 0.03), "purple": (0.30, 0.06, 0.45),
    "pink": (0.80, 0.35, 0.45), "brown": (0.18, 0.09, 0.04),
    "black": (0.02, 0.02, 0.02), "white": (0.85, 0.85, 0.85),
    "grey": (0.30, 0.30, 0.30), "gray": (0.30, 0.30, 0.30),
    "silver": (0.55, 0.55, 0.58), "gold": (0.65, 0.48, 0.10),
}

# Materials that imply their own shading, not just a colour.
_METALS = frozenset({"silver", "gold"})

_NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")

# "call it Wheel", "called Wheel", "name it Wheel", "named Wheel".
# NOT "as Wheel" -- "export it as glb" would name something "glb".
_NAMED = re.compile(r"\b(?:call|called|name|named)(?:\s+it)?\s+"
                    r"['\"]?([A-Za-z][\w -]{0,40}?)['\"]?\s*$", re.I)


def _find_color(text: str) -> Optional[tuple]:
    for word, value in COLORS.items():
        if re.search(rf"\b{word}\b", text):
            return word, value
    return None


def _find_numbers(text: str) -> List[float]:
    return [float(match) for match in _NUMBER.findall(text)]


def _find_name(text: str, fallback: str) -> str:
    match = _NAMED.search(text.strip())
    if not match:
        return fallback
    # Blender object names are free-form, but a name with a quote or a
    # newline in it makes every later reference miserable.
    cleaned = re.sub(r"[^\w -]", "", match.group(1)).strip()
    return cleaned or fallback


def _find_target(text: str) -> Optional[str]:
    """The object a sentence is about, when it says so.

    "rig the Character", "unwrap Body". Falls back to None, and the
    caller then uses whatever the run just created -- which is right
    for "make a tree and unwrap it".
    """
    match = re.search(r"\b(?:the|my|this)\s+([A-Z][\w]{1,40})\b", text)
    if match:
        return match.group(1)
    match = re.search(r"\b(?:rig|unwrap|sculpt|animate|export|bevel|smooth|"
                      r"subdivide|mirror)\s+(?:the\s+|my\s+|this\s+)?"
                      r"([A-Z][\w]{1,40})\b", text)
    return match.group(1) if match else None


# ======================================================
# Recipes -- nouns that become several objects
# ======================================================

def _material(name: str, text: str, default_color, default_metallic=0.0,
              default_rough=0.5) -> List[dict]:
    """A material step honouring a colour word if the sentence has one."""
    found = _find_color(text)
    if found:
        word, color = found
        metallic = 0.9 if word in _METALS else default_metallic
        rough = 0.25 if word in _METALS else default_rough
    else:
        color, metallic, rough = default_color, default_metallic, default_rough
    return [{"action": "create_material",
             "params": {"name": name, "color": list(color),
                        "metallic": metallic, "roughness": rough}}]


def _low_poly(text: str) -> bool:
    return bool(re.search(r"\blow[- ]?poly\b", text))


def _car(text: str) -> List[dict]:
    name = _find_name(text, "Car")
    steps: List[dict] = [
        {"action": "add_cube",
         "params": {"name": f"{name}_Body", "size": 2,
                    "location": [0, 0, 0.8]}},
        {"action": "scale",
         "params": {"object": f"{name}_Body", "x": 1.8, "y": 0.9, "z": 0.4}},
        {"action": "add_cube",
         "params": {"name": f"{name}_Cabin", "size": 2,
                    "location": [-0.2, 0, 1.5]}},
        {"action": "scale",
         "params": {"object": f"{name}_Cabin", "x": 0.9, "y": 0.8, "z": 0.35}},
    ]
    if not _low_poly(text):
        steps.append({"action": "apply_bevel",
                      "params": {"object": f"{name}_Body", "amount": 0.06,
                                 "segments": 2}})

    # Four wheels: cylinders on their sides at the corners.
    for index, (x, y) in enumerate(((1.2, 1.0), (1.2, -1.0),
                                    (-1.2, 1.0), (-1.2, -1.0))):
        wheel = f"{name}_Wheel_{index + 1}"
        steps += [
            {"action": "add_cylinder",
             "params": {"name": wheel, "radius": 0.42, "depth": 0.3,
                        "location": [x, y, 0.42]}},
            {"action": "rotate", "params": {"object": wheel, "x": 90}},
        ]

    steps += _material(f"{name}_Paint", text, (0.55, 0.06, 0.06),
                       default_metallic=0.7, default_rough=0.3)
    steps += [
        {"action": "assign_material",
         "params": {"object": f"{name}_Body", "material": f"{name}_Paint"}},
        {"action": "assign_material",
         "params": {"object": f"{name}_Cabin", "material": f"{name}_Paint"}},
        {"action": "create_material",
         "params": {"name": f"{name}_Rubber", "color": [0.02, 0.02, 0.02],
                    "metallic": 0.0, "roughness": 0.9}},
    ]
    for index in range(4):
        steps.append({"action": "assign_material",
                      "params": {"object": f"{name}_Wheel_{index + 1}",
                                 "material": f"{name}_Rubber"}})
    for index in range(4):
        steps.append({"action": "parent",
                      "params": {"child": f"{name}_Wheel_{index + 1}",
                                 "parent": f"{name}_Body"}})
    steps.append({"action": "parent",
                  "params": {"child": f"{name}_Cabin",
                             "parent": f"{name}_Body"}})
    return steps


def _character(text: str) -> List[dict]:
    """A blocked-out humanoid, proportioned so a rig lands on it.

    Not a sculpt. It is the shape somebody would box out before
    detailing, and it is what "make a character" can honestly mean
    without a model inventing geometry.
    """
    name = _find_name(text, "Character")
    parts = [
        (f"{name}_Torso", "add_cube", {"size": 2, "location": [0, 0, 1.35]},
         {"x": 0.35, "y": 0.22, "z": 0.45}),
        (f"{name}_Hips", "add_cube", {"size": 2, "location": [0, 0, 0.95]},
         {"x": 0.32, "y": 0.22, "z": 0.18}),
    ]
    steps: List[dict] = []
    for part, action, add, scale in parts:
        steps.append({"action": action, "params": {"name": part, **add}})
        steps.append({"action": "scale", "params": {"object": part, **scale}})

    steps += [
        {"action": "add_sphere",
         "params": {"name": f"{name}_Head", "radius": 0.28,
                    "location": [0, 0, 2.05]}},
        {"action": "scale",
         "params": {"object": f"{name}_Head", "x": 0.9, "y": 0.85, "z": 1.05}},
    ]

    limbs = (
        (f"{name}_Arm_L", 0.55, 1.45, 0.09, 0.85),
        (f"{name}_Arm_R", -0.55, 1.45, 0.09, 0.85),
        (f"{name}_Leg_L", 0.18, 0.45, 0.12, 0.95),
        (f"{name}_Leg_R", -0.18, 0.45, 0.12, 0.95),
    )
    for limb, x, z, radius, depth in limbs:
        steps.append({"action": "add_cylinder",
                      "params": {"name": limb, "radius": radius,
                                 "depth": depth, "location": [x, 0, z]}})

    steps += _material(f"{name}_Skin", text, (0.55, 0.36, 0.26))
    for part in (f"{name}_Torso", f"{name}_Hips", f"{name}_Head",
                 *(limb[0] for limb in limbs)):
        steps.append({"action": "assign_material",
                      "params": {"object": part, "material": f"{name}_Skin"}})
    for part in (f"{name}_Hips", f"{name}_Head",
                 *(limb[0] for limb in limbs)):
        steps.append({"action": "parent",
                      "params": {"child": part, "parent": f"{name}_Torso"}})
    return steps


def _tree(text: str) -> List[dict]:
    name = _find_name(text, "Tree")
    low = _low_poly(text)
    steps: List[dict] = [
        {"action": "add_cylinder",
         "params": {"name": f"{name}_Trunk", "radius": 0.18, "depth": 2.4,
                    "location": [0, 0, 1.2]}},
        {"action": "add_sphere",
         "params": {"name": f"{name}_Canopy", "radius": 1.1,
                    "location": [0, 0, 2.9]}},
    ]
    if low:
        # Decimate to a facetted canopy: this is the whole point of
        # somebody asking for low-poly.
        steps.append({"action": "apply_decimate",
                      "params": {"object": f"{name}_Canopy", "ratio": 0.12,
                                 "apply": True}})
    else:
        steps.append({"action": "apply_subdivision",
                      "params": {"object": f"{name}_Canopy", "levels": 1}})

    steps += [
        {"action": "create_material",
         "params": {"name": f"{name}_Bark", "color": [0.13, 0.07, 0.03],
                    "metallic": 0.0, "roughness": 0.9}},
        {"action": "assign_material",
         "params": {"object": f"{name}_Trunk", "material": f"{name}_Bark"}},
    ]
    steps += _material(f"{name}_Leaves", text, (0.06, 0.30, 0.07),
                       default_rough=0.8)
    steps += [
        {"action": "assign_material",
         "params": {"object": f"{name}_Canopy", "material": f"{name}_Leaves"}},
        {"action": "parent",
         "params": {"child": f"{name}_Canopy", "parent": f"{name}_Trunk"}},
    ]
    return steps


def _house(text: str) -> List[dict]:
    name = _find_name(text, "House")
    return [
        {"action": "add_cube",
         "params": {"name": f"{name}_Walls", "size": 2, "location": [0, 0, 1.2]}},
        {"action": "scale",
         "params": {"object": f"{name}_Walls", "x": 2.0, "y": 1.5, "z": 0.6}},
        {"action": "apply_solidify",
         "params": {"object": f"{name}_Walls", "thickness": 0.08}},
        {"action": "add_cube",
         "params": {"name": f"{name}_Roof", "size": 2, "location": [0, 0, 2.5]}},
        {"action": "scale",
         "params": {"object": f"{name}_Roof", "x": 2.2, "y": 1.7, "z": 0.12}},
        {"action": "add_cube",
         "params": {"name": f"{name}_Door", "size": 2,
                    "location": [0, -1.5, 0.85]}},
        {"action": "scale",
         "params": {"object": f"{name}_Door", "x": 0.3, "y": 0.1, "z": 0.45}},
        *_material(f"{name}_Wall_Mat", text, (0.55, 0.50, 0.42),
                   default_rough=0.85),
        {"action": "assign_material",
         "params": {"object": f"{name}_Walls", "material": f"{name}_Wall_Mat"}},
        {"action": "create_material",
         "params": {"name": f"{name}_Roof_Mat", "color": [0.20, 0.06, 0.04],
                    "metallic": 0.0, "roughness": 0.8}},
        {"action": "assign_material",
         "params": {"object": f"{name}_Roof", "material": f"{name}_Roof_Mat"}},
        {"action": "parent",
         "params": {"child": f"{name}_Roof", "parent": f"{name}_Walls"}},
        {"action": "parent",
         "params": {"child": f"{name}_Door", "parent": f"{name}_Walls"}},
    ]


def _table(text: str) -> List[dict]:
    name = _find_name(text, "Table")
    steps: List[dict] = [
        {"action": "add_cube",
         "params": {"name": f"{name}_Top", "size": 2, "location": [0, 0, 0.75]}},
        {"action": "scale",
         "params": {"object": f"{name}_Top", "x": 1.0, "y": 0.6, "z": 0.04}},
        {"action": "apply_bevel",
         "params": {"object": f"{name}_Top", "amount": 0.02, "segments": 2}},
    ]
    for index, (x, y) in enumerate(((0.9, 0.5), (0.9, -0.5),
                                    (-0.9, 0.5), (-0.9, -0.5))):
        leg = f"{name}_Leg_{index + 1}"
        steps += [
            {"action": "add_cube",
             "params": {"name": leg, "size": 2, "location": [x, y, 0.36]}},
            {"action": "scale",
             "params": {"object": leg, "x": 0.05, "y": 0.05, "z": 0.36}},
            {"action": "parent", "params": {"child": leg,
                                            "parent": f"{name}_Top"}},
        ]
    steps += _material(f"{name}_Wood", text, (0.22, 0.11, 0.05),
                       default_rough=0.7)
    steps.append({"action": "assign_material",
                  "params": {"object": f"{name}_Top",
                             "material": f"{name}_Wood"}})
    for index in range(4):
        steps.append({"action": "assign_material",
                      "params": {"object": f"{name}_Leg_{index + 1}",
                                 "material": f"{name}_Wood"}})
    return steps


def _chair(text: str) -> List[dict]:
    name = _find_name(text, "Chair")
    steps: List[dict] = [
        {"action": "add_cube",
         "params": {"name": f"{name}_Seat", "size": 2, "location": [0, 0, 0.45]}},
        {"action": "scale",
         "params": {"object": f"{name}_Seat", "x": 0.25, "y": 0.25, "z": 0.03}},
        {"action": "add_cube",
         "params": {"name": f"{name}_Back", "size": 2,
                    "location": [-0.22, 0, 0.75]}},
        {"action": "scale",
         "params": {"object": f"{name}_Back", "x": 0.03, "y": 0.25, "z": 0.3}},
        {"action": "parent",
         "params": {"child": f"{name}_Back", "parent": f"{name}_Seat"}},
    ]
    for index, (x, y) in enumerate(((0.2, 0.2), (0.2, -0.2),
                                    (-0.2, 0.2), (-0.2, -0.2))):
        leg = f"{name}_Leg_{index + 1}"
        steps += [
            {"action": "add_cylinder",
             "params": {"name": leg, "radius": 0.025, "depth": 0.42,
                        "location": [x, y, 0.21]}},
            {"action": "parent", "params": {"child": leg,
                                            "parent": f"{name}_Seat"}},
        ]
    steps += _material(f"{name}_Wood", text, (0.24, 0.13, 0.06),
                       default_rough=0.7)
    steps.append({"action": "assign_material",
                  "params": {"object": f"{name}_Seat",
                             "material": f"{name}_Wood"}})
    return steps


def _sword(text: str) -> List[dict]:
    name = _find_name(text, "Sword")
    return [
        {"action": "add_cube",
         "params": {"name": f"{name}_Blade", "size": 2, "location": [0, 0, 1.4]}},
        {"action": "scale",
         "params": {"object": f"{name}_Blade", "x": 0.06, "y": 0.015, "z": 1.0}},
        {"action": "apply_bevel",
         "params": {"object": f"{name}_Blade", "amount": 0.012, "segments": 1}},
        {"action": "add_cube",
         "params": {"name": f"{name}_Guard", "size": 2, "location": [0, 0, 0.4]}},
        {"action": "scale",
         "params": {"object": f"{name}_Guard", "x": 0.22, "y": 0.04, "z": 0.03}},
        {"action": "add_cylinder",
         "params": {"name": f"{name}_Grip", "radius": 0.035, "depth": 0.38,
                    "location": [0, 0, 0.2]}},
        {"action": "add_sphere",
         "params": {"name": f"{name}_Pommel", "radius": 0.06,
                    "location": [0, 0, 0.0]}},
        {"action": "create_material",
         "params": {"name": f"{name}_Steel", "color": [0.55, 0.56, 0.60],
                    "metallic": 1.0, "roughness": 0.18}},
        {"action": "assign_material",
         "params": {"object": f"{name}_Blade", "material": f"{name}_Steel"}},
        {"action": "assign_material",
         "params": {"object": f"{name}_Guard", "material": f"{name}_Steel"}},
        {"action": "create_material",
         "params": {"name": f"{name}_Leather", "color": [0.10, 0.05, 0.02],
                    "metallic": 0.0, "roughness": 0.9}},
        {"action": "assign_material",
         "params": {"object": f"{name}_Grip", "material": f"{name}_Leather"}},
        {"action": "parent",
         "params": {"child": f"{name}_Guard", "parent": f"{name}_Blade"}},
        {"action": "parent",
         "params": {"child": f"{name}_Grip", "parent": f"{name}_Blade"}},
        {"action": "parent",
         "params": {"child": f"{name}_Pommel", "parent": f"{name}_Blade"}},
    ]


def _barrel(text: str) -> List[dict]:
    name = _find_name(text, "Barrel")
    steps = [
        {"action": "add_cylinder",
         "params": {"name": f"{name}_Body", "radius": 0.45, "depth": 1.1,
                    "location": [0, 0, 0.55]}},
        {"action": "apply_bevel",
         "params": {"object": f"{name}_Body", "amount": 0.04, "segments": 2}},
    ]
    for index, height in enumerate((0.25, 0.85)):
        band = f"{name}_Band_{index + 1}"
        steps += [
            {"action": "add_torus",
             "params": {"name": band, "major_radius": 0.46,
                        "minor_radius": 0.03, "location": [0, 0, height]}},
            {"action": "parent", "params": {"child": band,
                                            "parent": f"{name}_Body"}},
        ]
    steps += _material(f"{name}_Wood", text, (0.20, 0.10, 0.04),
                       default_rough=0.85)
    steps.append({"action": "assign_material",
                  "params": {"object": f"{name}_Body",
                             "material": f"{name}_Wood"}})
    return steps


def _rock(text: str) -> List[dict]:
    name = _find_name(text, "Rock")
    steps = [
        {"action": "add_sphere",
         "params": {"name": name, "radius": 1.0, "location": [0, 0, 1.0]}},
        {"action": "scale",
         "params": {"object": name, "x": 1.0, "y": 0.8, "z": 0.65}},
        {"action": "apply_decimate",
         "params": {"object": name, "ratio": 0.18, "apply": True}},
    ]
    steps += _material(f"{name}_Stone", text, (0.16, 0.16, 0.17),
                       default_rough=0.95)
    steps.append({"action": "assign_material",
                  "params": {"object": name, "material": f"{name}_Stone"}})
    return steps


# Word -> builder. The keys are what a sentence has to contain, and
# nothing outside this table gets built.
RECIPES = {
    "car": _car, "vehicle": _car, "truck": _car,
    "character": _character, "person": _character, "humanoid": _character,
    "tree": _tree,
    "house": _house, "building": _house,
    "table": _table, "desk": _table,
    "chair": _chair, "stool": _chair,
    "sword": _sword, "blade": _sword,
    "barrel": _barrel, "crate": _barrel,
    "rock": _rock, "boulder": _rock, "stone": _rock,
}


def recipe_names() -> List[str]:
    return sorted(set(RECIPES))


# ======================================================
# Operations -- verbs that act on what is already there
# ======================================================

def _rig(text: str, target: Optional[str]) -> List[dict]:
    """A five-bone humanoid rig, parented with automatic weights.

    Five bones is the least that is honestly a rig: spine, head, one
    arm, one leg -- enough to pose, and enough for `auto_weights` to
    have something to bind to.
    """
    mesh = target or "Character_Torso"
    armature = "Armature"
    return [
        {"action": "create_armature",
         "params": {"name": armature, "location": [0, 0, 0.95]}},
        {"action": "add_bone",
         "params": {"armature": armature, "name": "Spine",
                    "head": [0, 0, 0.95], "tail": [0, 0, 1.75]}},
        {"action": "add_bone",
         "params": {"armature": armature, "name": "Head",
                    "head": [0, 0, 1.75], "tail": [0, 0, 2.25]}},
        {"action": "add_bone",
         "params": {"armature": armature, "name": "Arm_L",
                    "head": [0.2, 0, 1.7], "tail": [0.9, 0, 1.35]}},
        {"action": "add_bone",
         "params": {"armature": armature, "name": "Arm_R",
                    "head": [-0.2, 0, 1.7], "tail": [-0.9, 0, 1.35]}},
        {"action": "add_bone",
         "params": {"armature": armature, "name": "Leg_L",
                    "head": [0.18, 0, 0.95], "tail": [0.18, 0, 0.05]}},
        {"action": "add_bone",
         "params": {"armature": armature, "name": "Leg_R",
                    "head": [-0.18, 0, 0.95], "tail": [-0.18, 0, 0.05]}},
        {"action": "auto_weights", "params": {"mesh": mesh,
                                              "armature": armature}},
        {"action": "normalize_weights", "params": {"mesh": mesh}},
    ]


# A walk is four poses on a 24-frame loop: contact, passing, contact
# again mirrored, passing again. Degrees, applied to the leg and arm
# bones the rig above creates.
_WALK = (
    (1, (("Leg_L", (25, 0, 0)), ("Leg_R", (-25, 0, 0)),
         ("Arm_L", (-20, 0, 0)), ("Arm_R", (20, 0, 0)))),
    (7, (("Leg_L", (0, 0, 0)), ("Leg_R", (0, 0, 0)),
         ("Arm_L", (0, 0, 0)), ("Arm_R", (0, 0, 0)))),
    (13, (("Leg_L", (-25, 0, 0)), ("Leg_R", (25, 0, 0)),
          ("Arm_L", (20, 0, 0)), ("Arm_R", (-20, 0, 0)))),
    (19, (("Leg_L", (0, 0, 0)), ("Leg_R", (0, 0, 0)),
          ("Arm_L", (0, 0, 0)), ("Arm_R", (0, 0, 0)))),
    (25, (("Leg_L", (25, 0, 0)), ("Leg_R", (-25, 0, 0)),
          ("Arm_L", (-20, 0, 0)), ("Arm_R", (20, 0, 0)))),
)

_IDLE = (
    (1, (("Spine", (0, 0, 0)), ("Head", (0, 0, 0)))),
    (24, (("Spine", (2.5, 0, 0)), ("Head", (-2, 0, 1.5)))),
    (48, (("Spine", (0, 0, 0)), ("Head", (0, 0, 0)))),
)


def _animate(text: str, target: Optional[str]) -> List[dict]:
    armature = target or "Armature"
    cycle = _IDLE if re.search(r"\bidle\b", text) else _WALK
    steps: List[dict] = []
    for frame, poses in cycle:
        for bone, rotation in poses:
            steps.append({"action": "set_pose",
                          "params": {"armature": armature, "bone": bone,
                                     "rotation": list(rotation),
                                     "frame": frame}})
    return steps


def _unwrap(text: str, target: Optional[str]) -> List[dict]:
    obj = target or "Cube"
    if re.search(r"\bseams?\b", text):
        return [
            {"action": "mark_seams", "params": {"object": obj,
                                                "sharpness": 0.9}},
            {"action": "unwrap", "params": {"object": obj, "margin": 0.02}},
        ]
    return [{"action": "smart_uv_project",
             "params": {"object": obj, "angle_limit": 1.15, "margin": 0.02}}]


def _sculpt(text: str, target: Optional[str]) -> List[dict]:
    """Sculpt setup on a subdivided base.

    Headless Blender has one brush and no viewport to drag it in, so
    this prepares a mesh for sculpting rather than claiming to sculpt
    it. The template says so in its own result.
    """
    obj = target or "Sculpt_Base"
    steps: List[dict] = []
    if target is None:
        steps += [
            {"action": "add_sphere",
             "params": {"name": obj, "radius": 1.0, "location": [0, 0, 1]}},
        ]
    numbers = _find_numbers(text)
    levels = int(numbers[0]) if numbers and 1 <= numbers[0] <= 4 else 2
    steps += [
        {"action": "apply_multires", "params": {"object": obj, "levels": levels}},
        {"action": "enable_dyntopo", "params": {"object": obj}},
        {"action": "sculpt_brush",
         "params": {"object": obj, "brush_type": "DRAW", "strength": 0.5,
                    "detail_size": 12.0}},
    ]
    return steps


_EXPORT_FORMATS = (
    (r"\bfbx\b", "export_fbx", "fbx"),
    (r"\b(?:glb|gltf)\b", "export_glb", "glb"),
    (r"\bobj\b", "export_obj", "obj"),
)


def _export(text: str, target: Optional[str]) -> List[dict]:
    action, suffix = "export_fbx", "fbx"
    for pattern, name, extension in _EXPORT_FORMATS:
        if re.search(pattern, text):
            action, suffix = name, extension
            break
    # No path in the sentence: the caller fills one in. A path invented
    # here would be a hallucinated path, which is the one thing ARIA is
    # never allowed to do.
    return [{"action": action,
             "params": {"path": f"<export>.{suffix}", "object": target}}]


def _smooth(text: str, target: Optional[str]) -> List[dict]:
    obj = target or "Cube"
    numbers = _find_numbers(text)
    levels = int(numbers[0]) if numbers and 1 <= numbers[0] <= 4 else 2
    return [{"action": "apply_subdivision",
             "params": {"object": obj, "levels": levels}}]


def _mirror(text: str, target: Optional[str]) -> List[dict]:
    obj = target or "Cube"
    axis = "X"
    for candidate in ("x", "y", "z"):
        if re.search(rf"\b{candidate}[- ]?axis\b", text):
            axis = candidate.upper()
            break
    return [{"action": "apply_mirror", "params": {"object": obj, "axis": axis}}]


# Verb pattern -> builder. Order matters: the first match wins, so the
# more specific patterns come first.
OPERATIONS = (
    (r"\b(?:rig|add a rig|create a rig|add an armature|skeleton)\b", _rig, "rig"),
    (r"\b(?:animate|animation|walk cycle|idle)\b", _animate, "animate"),
    (r"\b(?:uv[- ]?unwrap|unwrap|uv map)\b", _unwrap, "unwrap"),
    (r"\bsculpt\b", _sculpt, "sculpt"),
    (r"\bexport\b", _export, "export"),
    (r"\b(?:subdivide|smooth)\b", _smooth, "smooth"),
    (r"\bmirror\b", _mirror, "mirror"),
)


# ======================================================
# Reading the whole sentence
# ======================================================

_MAKE = re.compile(r"\b(?:create|make|generate|build|model|add|give me|"
                   r"i want|i need)\b", re.I)

# Asking ABOUT modelling is not asking FOR a model. Measured: "how do
# I model a car in Blender?" built a twenty-seven step car, because
# _MAKE matches "model" and the question mark meant nothing. Somebody
# asking how a thing is done wants an answer, not four wheels.
#
# can/could/would/will are NOT here. "Can you make a car in Blender?"
# is a request in a polite shape, and once the tool gate has done its
# job there is no ambiguity left for this guard to protect against --
# the person named Blender. Refusing them for being polite would be
# friction with no safety behind it.
_ASKING_ABOUT = re.compile(
    r"^\s*(?:how|what|whats|why|where|when|who|which|whose|"
    r"is|are|was|were|do|does|did|should|tell me|explain)\b",
    re.I)

# ...but an interrogative opening can still be wrapped around a
# request for an EXPLANATION, and those are the ones that must not
# build. "Can you tell me how to make a car in Blender?" opens with
# "can", so dropping can/could/would/will above would let it through
# on its way to twenty-seven steps nobody asked for.
#
# Matched anywhere in the sentence, not just at the front, because
# that is where these phrases actually sit.
_WANTS_EXPLANATION = re.compile(
    r"\b(?:tell me|explain|teach me|walk me through|show me how|"
    r"how (?:do|would|can|could|to|does)|"
    r"what(?:'s| is|s) the (?:best|right|easiest) way|"
    r"steps? (?:to|for)|tutorial|guide)\b",
    re.I)


# ======================================================
# Naming the tool
# ======================================================

# Blender acts only when Blender is named. Three prepositions ("in",
# "with", "using"), the verb form ("use Blender to..."), and the
# vocative opening ("Blender, make a car").
#
# WHY A GATE AND NOT A GUESS
# "make a car" is not a Blender request. It is a sentence that could
# mean a 3D model, a game object, a drawing, a story, or nothing at
# all. Acting on it means starting a subprocess and writing files
# because somebody used a common verb, and a person who wanted a
# conversation gets a scene instead.
#
# Naming the tool is cheap for the person and unambiguous for ARIA.
#
# "from" earns its place on the export sentence: "export it from
# Blender to Unity" is the ordinary way to say that, and without it
# the one request the asset pipeline exists for did not reach here.
_BLENDER_NAMED = re.compile(
    r"\b(?:in|with|inside|via|from)\s+blender\b"
    r"|\bus(?:e|es|ing)\s+blender\b"
    r"|^\s*blender\s*[,:]",
    re.I)

# Other tools that model or generate assets. Naming one of these means
# the request is not for this layer, even if Blender is named too --
# an ambiguous sentence is a reason to ask, not to pick.
#
# Unity is deliberately NOT here. "export it from Blender to Unity" is
# a Blender request with a destination, and blender_asset_pipeline
# exists to serve exactly that sentence.
_OTHER_TOOLS = ("ludo",)

# Matched as a bare word rather than behind a preposition, because
# "make a car in Blender or Ludo" names two tools with only one
# preposition and is exactly the sentence that should not be guessed
# at. Mentioning the tool at all is enough.
#
# The cost is a sentence like "make a ludo board in Blender", which is
# refused. That is the safe direction: a refusal is a conversation.
_OTHER_TOOL_NAMED = re.compile(
    r"\b(?:%s)(?:\.ai)?\b" % "|".join(_OTHER_TOOLS), re.I)


def names_blender(text: str) -> bool:
    """Whether a sentence explicitly asks for Blender.

    Public because routing and running should ask the same question.
    A short-circuit that decides differently from the mapper is a bug
    waiting for a sentence that lands between them.
    """
    return bool(_BLENDER_NAMED.search(str(text or "")))


def names_another_tool(text: str) -> bool:
    """Whether a sentence names some other asset tool."""
    return bool(_OTHER_TOOL_NAMED.search(str(text or "")))


def wants_something_built(text: str) -> bool:
    """Whether a sentence asks Blender to DO something, recognised or not.

    The difference that matters when map_text returns None. Two very
    different sentences both come back empty:

        "in Blender, what makes good topology?"  -- a question
        "make me a spaceship in Blender"         -- a request I cannot fill

    The first should go to a model, which can answer it. The second
    must not: a model asked to build a spaceship will describe one and
    sound like it did the work, which is the failure this whole layer
    exists to prevent. It gets an honest "I do not know how".
    """
    if not names_blender(text) or names_another_tool(text):
        return False

    stripped = _without_tool(text)
    lowered = stripped.lower()
    if _ASKING_ABOUT.match(lowered) or _WANTS_EXPLANATION.search(lowered):
        return False

    if _MAKE.search(lowered):
        return True
    return any(re.search(pattern, lowered) for pattern, _, _ in OPERATIONS)


def _without_tool(text: str) -> str:
    """The sentence with the tool phrase taken out.

    Everything downstream reads position: _find_name looks for a name
    at the END of the sentence, so "a sword called Excalibur in
    Blender" would otherwise be a sword called nothing. Removing the
    phrase that got us here leaves the request the person actually
    made.
    """
    return re.sub(r"\s{2,}", " ", _BLENDER_NAMED.sub(" ", str(text))).strip()


def map_text(text: str) -> Optional[Dict[str, Any]]:
    """Turn a sentence into actions, or return None.

    None means "not a Blender request I recognise", and the caller
    must then let the turn go to a model to be talked about rather
    than guessing.

    Returns {"actions": [...], "summary": str, "matched": [...]}.
    """
    if not text or not str(text).strip():
        return None

    # The gate, before anything else is read. Blender is named, and no
    # competing tool is.
    if not names_blender(text) or names_another_tool(text):
        return None

    stripped = _without_tool(text)
    lowered = stripped.lower()
    original = stripped

    if _ASKING_ABOUT.match(lowered) or _WANTS_EXPLANATION.search(lowered):
        return None

    target = _find_target(original)

    actions: List[dict] = []
    matched: List[str] = []

    # A recipe: a noun that becomes objects. Only when the sentence
    # also asks for something to be made -- "the car is red" is not a
    # request to model a car.
    recipe_hit = None
    if _MAKE.search(lowered):
        for word in sorted(RECIPES, key=len, reverse=True):
            if re.search(rf"\b{word}s?\b", lowered):
                recipe_hit = word
                break

    if recipe_hit:
        # Always start clean for a recipe. Blender's default scene has
        # a cube, a camera and a light in it, and a car built around a
        # stray cube is the sort of thing that is only noticed after
        # the export.
        actions.append({"action": "clear_scene"})
        actions += RECIPES[recipe_hit](original)
        matched.append(f"recipe:{recipe_hit}")

    # Operations: verbs acting on what exists. A sentence can have both
    # ("make a character and rig it"), and then the rig runs against
    # what the recipe just built.
    for pattern, builder, label in OPERATIONS:
        if re.search(pattern, lowered):
            local_target = target
            if local_target is None and recipe_hit:
                local_target = _default_target(recipe_hit, original)
            actions += builder(lowered, local_target)
            matched.append(label)

    if not actions:
        return None

    return {"actions": actions, "matched": matched,
            "summary": describe(actions)}


def _default_target(recipe: str, text: str) -> Optional[str]:
    """What an operation in the same sentence should act on.

    "make a character and rig it" -- "it" is the torso, because that is
    the object the rest of the character is parented to.
    """
    builder = RECIPES.get(recipe)
    if builder is None:  # pragma: no cover - keys and table agree
        return None
    steps = builder(text)
    for step in steps:
        name = (step.get("params") or {}).get("name")
        if name and step["action"].startswith("add_"):
            return name
    return None


def describe(actions: List[dict]) -> str:
    """A sentence saying what a list of actions will do.

    Shown before anything runs, because a person should be able to see
    that "create a car" became four wheels and not four spheres.
    """
    if not actions:
        return "nothing"

    created = [a["params"]["name"] for a in actions
               if a["action"].startswith("add_") and (a.get("params") or {}).get("name")]
    parts: List[str] = []
    if created:
        shown = ", ".join(created[:6])
        if len(created) > 6:
            shown += f" and {len(created) - 6} more"
        parts.append(f"create {shown}")

    verbs = {
        "create_material": "add materials",
        "apply_subdivision": "smooth it",
        "apply_bevel": "bevel the edges",
        "apply_decimate": "cut the polygon count",
        "apply_mirror": "mirror it",
        "smart_uv_project": "UV unwrap it",
        "unwrap": "UV unwrap it",
        "auto_weights": "bind it to a rig",
        "set_pose": "key an animation",
        "sculpt_brush": "set up sculpting",
        "export_fbx": "export FBX",
        "export_glb": "export GLB",
        "export_obj": "export OBJ",
    }
    seen = []
    for action in actions:
        phrase = verbs.get(action["action"])
        if phrase and phrase not in seen:
            seen.append(phrase)
    parts += seen

    return ", then ".join(parts) if parts else f"run {len(actions)} steps"
