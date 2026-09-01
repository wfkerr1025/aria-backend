"""ARIA Lite - turning what somebody said into one Ludo.ai action.

The mirror of blender_nl_mapping, and it works the same way: a
vocabulary this module owns, mapped to primitives that already exist.
A model never gets to decide what runs.

LUDO HAS TO BE NAMED
--------------------
"in Ludo", "with Ludo", "using Ludo", or "Ludo, ..." to open. "Make a
car" on its own returns None.

The gate matters more here than it does for Blender, and for a reason
Blender does not have: EVERY GENERATION COSTS REAL MONEY. Blender is
free to run and can be run again; a Ludo call spends credits the user
cannot get back. A sentence read too eagerly is not an inconvenience,
it is a charge.

Naming Blender instead returns None, exactly as naming Ludo returns
None over in the Blender mapper. Two tools that both make 3D assets
must not both answer one sentence.

ONE ACTION, NOT A RECIPE
------------------------
Blender's mapper composes twenty-seven steps because Blender is a
program being driven. Ludo is an API being asked, so a sentence maps
to ONE call -- which may itself be two HTTP requests, but that is
ludo_actions' business and it says so.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

__all__ = [
    "ACTIONS",
    "STYLE_WORDS",
    "describe",
    "map_text",
    "names_another_tool",
    "names_ludo",
    "wants_something_built",
]


# ======================================================
# Naming the tool
# ======================================================

_LUDO_NAMED = re.compile(
    r"\b(?:in|with|inside|via|from|on)\s+ludo(?:\.ai)?\b"
    r"|\bus(?:e|es|ing)\s+ludo(?:\.ai)?\b"
    r"|^\s*ludo(?:\.ai)?\s*[,:]",
    re.I)

# Blender is the competing tool here, matched as a bare word for the
# same reason Ludo is over there: "make a car in Ludo or Blender"
# names two tools with one preposition, and that is precisely the
# sentence nobody should guess at.
_OTHER_TOOLS = ("blender",)

_OTHER_TOOL_NAMED = re.compile(
    r"\b(?:%s)\b" % "|".join(_OTHER_TOOLS), re.I)


def names_ludo(text: str) -> bool:
    """Whether a sentence explicitly asks for Ludo.ai.

    Public because routing and running should ask the same question.
    """
    return bool(_LUDO_NAMED.search(str(text or "")))


def names_another_tool(text: str) -> bool:
    """Whether a sentence names some other asset tool."""
    return bool(_OTHER_TOOL_NAMED.search(str(text or "")))


def _without_tool(text: str) -> str:
    """The sentence with the tool phrase taken out."""
    return re.sub(r"\s{2,}", " ", _LUDO_NAMED.sub(" ", str(text))).strip()


# ======================================================
# Asking versus instructing
# ======================================================

_ASKING_ABOUT = re.compile(
    r"^\s*(?:how|what|whats|why|where|when|who|which|whose|"
    r"is|are|was|were|do|does|did|should|tell me|explain)\b",
    re.I)

_WANTS_EXPLANATION = re.compile(
    r"\b(?:tell me|explain|teach me|walk me through|show me how|"
    r"how (?:do|would|can|could|to|does)|"
    r"what(?:'s| is|s) the (?:best|right|easiest) way|"
    r"steps? (?:to|for)|tutorial|guide|"
    r"how much (?:does|do|is)|what does it cost|credits? cost)\b",
    re.I)

_MAKE = re.compile(r"\b(?:create|make|generate|build|model|draw|render|"
                   r"design|compose|write me|give me|i want|i need|get me)\b",
                   re.I)


# ======================================================
# What was asked for
# ======================================================

# Art styles a person actually says, mapped to the spec's own enum
# values. "low poly" is a real art_style; "lowpoly" is not, and would
# be refused by the API after a credit had been spent finding out.
STYLE_WORDS = {
    "low poly": "Low Poly", "low-poly": "Low Poly", "lowpoly": "Low Poly",
    "pixel": "8-Bit", "pixel art": "8-Bit", "8 bit": "8-Bit",
    "8-bit": "8-Bit", "16 bit": "16-Bit", "16-bit": "16-Bit",
    "32-bit": "32-Bit", "voxel": "Voxel Art",
    "anime": "Anime/Manga", "manga": "Anime/Manga", "chibi": "Chibi",
    "cartoon": "Western Cartoon", "cel shaded": "Cel-Shaded",
    "cel-shaded": "Cel-Shaded", "comic": "Comic Book",
    "watercolor": "Watercolor", "watercolour": "Watercolor",
    "sketch": "Sketch", "noir": "Noir", "neon": "Neon",
    "minimalist": "Minimalist", "flat": "Flat Design",
    "photorealistic": "Photorealistic 3D", "realistic": "Photorealistic 3D",
    "hand painted": "Hand-Painted", "hand-painted": "Hand-Painted",
    "claymation": "Claymation", "clay": "Claymation",
    "paper craft": "Paper Craft", "papercraft": "Paper Craft",
    "pixar": "Pixar Style", "stylized 3d": "Stylized 3D",
    # "stylized" alone is NOT here. Measured: "a stylized cartoon
    # girl" chose Stylized 3D over Western Cartoon, because the
    # longest phrase wins and "stylized" is longer than "cartoon" --
    # so a 2D drawing request picked a 3D style. A word that generic
    # should not own a style; the phrase that names one can.
    "retro": "Retro 2D", "silhouette": "Silhouette",
}

_NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")

# "saying 'hello there'", "that says \"run!\""
_QUOTED = re.compile(r"[\"'“‘]([^\"'”’]{1,300})[\"'”’]")


def _find_style(text: str) -> Optional[str]:
    for phrase in sorted(STYLE_WORDS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(phrase)}\b", text):
            return STYLE_WORDS[phrase]
    return None


def _find_number(text: str) -> Optional[float]:
    found = _NUMBER.findall(text)
    return float(found[0]) if found else None


def _find_quoted(text: str) -> Optional[str]:
    match = _QUOTED.search(text)
    return match.group(1).strip() if match else None


def _subject(text: str) -> str:
    """What to actually ask Ludo for, with the instruction words gone.

    "make me a red sports car" -> "red sports car". Ludo's own prompt
    augmentation does the rest, and leaving "make me a" in the prompt
    only competes with it.
    """
    stripped = _MAKE.sub(" ", text)
    stripped = re.sub(r"\b(?:me|a|an|the|some|please|for me)\b", " ", stripped,
                      flags=re.I)
    stripped = re.sub(r"\b(?:in|with|using)\s+\w+\s+style\b", " ", stripped,
                      flags=re.I)
    for phrase in STYLE_WORDS:
        stripped = re.sub(rf"\b{re.escape(phrase)}\b", " ", stripped, flags=re.I)
    stripped = re.sub(r"\s{2,}", " ", stripped).strip(" ,.!?-")
    return stripped


# WHY THERE IS NO "I DO NOT KNOW HOW"
# -----------------------------------
# Measured. Typed into chat:
#
#     Create a stylized cartoon girl with bright red hair, a large
#     pink bow, a pink dress, big expressive eyes, and a confident
#     heroic pose in Ludo
#
# and this module returned None, because "girl" was not in its list of
# character words -- and nothing else in the sentence matched either.
# The best a short-circuit could have said was "I do not know how to
# make that", about a plain image request, to an API whose entire
# purpose is turning a sentence into a picture.
#
# That was the wrong shape. Blender needs a vocabulary because a noun
# it has never heard of cannot be built out of cubes and cylinders.
# Ludo has no such limit. So the table below recognises only the
# SPECIAL cases -- a sprite sheet, a sound, music, a voice, a video --
# and everything else that asks for something becomes an image.
#
# It is also the cheapest reading. An image is one generation; the 3D
# pipeline is two, because Ludo has no text-to-3D. Guessing "model"
# from an ambiguous sentence would spend twice what guessing "image"
# does, so the ambiguous case takes the cheap road and the expensive
# one has to be asked for by name.


def _params_style(text: str, subject: str) -> dict:
    return {"prompt": subject, "style": _find_style(text)}


def _params_sprite(text: str, subject: str) -> dict:
    frames = _find_number(text)
    params = {"prompt": subject, "style": _find_style(text)}
    if frames is not None:
        params["frames"] = int(frames)
    return params


def _params_audio(text: str, subject: str) -> dict:
    params = {"prompt": subject}
    duration = _find_number(text)
    if duration is not None:
        params["duration"] = duration
    return params


def _params_music(text: str, subject: str) -> dict:
    params = {"prompt": subject}
    duration = _find_number(text)
    if duration is not None:
        params["duration"] = int(duration)
    return params


def _params_voice(text: str, subject: str) -> dict:
    """A voice needs a line, and the line is whatever was quoted.

    Left as None when nothing was quoted, so ludo_actions asks for it
    rather than this module inventing something to say.
    """
    return {"prompt": subject, "text": _find_quoted(text)}


def _params_animation(text: str, subject: str) -> dict:
    params = {"prompt": subject, "style": _find_style(text)}
    duration = _find_number(text)
    if duration is not None:
        params["duration"] = duration
    return params


def _params_model(text: str, subject: str) -> dict:
    params = {"prompt": subject, "style": _find_style(text)}
    faces = _find_number(text)
    # Only a number big enough to be a face budget. "a 4 wheeled car"
    # must not become a four-face model.
    if faces is not None and faces >= 500:
        params["polycount"] = int(faces)
    return params


# What kind of image, from the words people actually use. Every value
# on the right is in ludo_client.IMAGE_TYPES, pinned by a test --
# an invented type costs a credit to be refused.
#
# image_type is not decoration: Ludo composes a sprite differently
# from a background, and a background differently from an icon.
_IMAGE_TYPE_HINTS = (
    (r"\b(?:portraits?|headshots?)\b", "portrait"),
    (r"\b(?:icons?)\b", "icon"),
    (r"\b(?:logos?)\b", "logo"),
    (r"\b(?:tiles?|tileable|seamless)\b", "tile"),
    (r"\b(?:textures?|materials?)\b", "texture"),
    (r"\b(?:sprites?)\b", "sprite"),
    (r"\b(?:ui|buttons?|hud)\b", "ui_asset"),
    (r"\b(?:card art)\b", "card-art"),
    (r"\b(?:splash|title screen)\b", "splash"),
    (r"\b(?:parallax)\b", "parallax_layer"),
    (r"\b(?:backgrounds?|environments?|landscapes?|forests?|cities|city|"
     r"dungeons?|caves?|scenes?|terrains?)\b", "fixed_background"),
)

_WIDE = ("fixed_background", "side_scrolling_background", "splash",
         "parallax_layer")
_SQUARE = ("texture", "tile", "icon", "item-icon")


def _params_image(text: str, subject: str) -> dict:
    """An image, with the image_type inferred from what was asked for."""
    params = {"prompt": subject, "style": _find_style(text)}

    params["image_type"] = "art"
    for pattern, kind in _IMAGE_TYPE_HINTS:
        if re.search(pattern, text):
            params["image_type"] = kind
            break

    if params["image_type"] in _WIDE:
        params["aspect_ratio"] = "ar_16_9"
    elif params["image_type"] in _SQUARE:
        params["aspect_ratio"] = "ar_1_1"
    return params


# A sentence reaches the 3D pipeline -- two generations -- only when it
# says so. "Make a car in Ludo" is a picture of a car; "make a 3D car
# in Ludo" is a model.
_WANTS_3D = re.compile(
    r"\b(?:3d|3-d|three[- ]dimensional|models?|meshe?s?|sculpts?|"
    r"printable|game[- ]?ready)\b", re.I)

_VEHICLE = re.compile(
    r"\b(?:cars?|trucks?|vehicles?|tanks?|ships?|planes?|aircraft|"
    r"motorbikes?|bikes?|spaceships?)\b", re.I)

_CHARACTER = re.compile(
    r"\b(?:characters?|persons?|people|heroe?s?|humanoids?|npcs?|"
    r"creatures?|monsters?|enemies|enemys?|avatars?|girls?|boys?|"
    r"wo?m[ae]n|knights?|wizards?|mages?|warriors?|princess(?:es)?|"
    r"soldiers?|robots?|figures?)\b", re.I)

FORMAT, SUBJECT = "format", "subject"

# Only the special cases. The first match wins, so the more specific
# phrases come first, and anything falling through becomes an image.
ACTIONS = (
    (r"\b(?:sprite\s?sheets?|spritesheets?)\b", "generate_sprite_sheet",
     _params_sprite, FORMAT),
    (r"\b(?:sound\s?effects?|sfx)\b", "generate_audio", _params_audio, FORMAT),
    (r"\b(?:music|soundtracks?|theme\s?tunes?|songs?|scores?)\b",
     "generate_music", _params_music, FORMAT),
    (r"\b(?:voices?|speech|dialogue|voiceovers?|voice-overs?)\b",
     "generate_voice", _params_voice, FORMAT),
    (r"\b(?:animations?|animate|videos?|clips?|cutscenes?)\b",
     "generate_animation", _params_animation, FORMAT),
    (r"\b(?:audio|sounds?)\b", "generate_audio", _params_audio, FORMAT),
)


# Collecting a generation that outran its turn. Not a generation, so
# it costs nothing and is deliberately checked before everything else
# -- "collect abc123 in Ludo" contains no make-verb and would
# otherwise fall through to None.
_COLLECT = re.compile(
    r"\b(?:collect|fetch|retrieve|get|check|pick up)\b[^\w]{0,4}"
    r"(?:job|generation|asset)?[^\w]{0,4}"
    r"([A-Za-z0-9][A-Za-z0-9_-]{5,63})\b", re.I)


def collect_request(text: str) -> Optional[str]:
    """The job id a sentence asks to collect, or None.

    Free: the work was charged for when it was queued.
    """
    if not names_ludo(text) or names_another_tool(text):
        return None
    match = _COLLECT.search(_without_tool(text))
    return match.group(1) if match else None


def wants_something_built(text: str) -> bool:
    """Whether a sentence asks Ludo to make something, recognised or not.

    The difference between "in Ludo, what does a credit cost?" -- which
    a model should answer -- and "make me a spaceship in Ludo", which
    it must not, because it would describe one and sound like it had
    spent the credits making it.
    """
    if not names_ludo(text) or names_another_tool(text):
        return False

    lowered = _without_tool(text).lower()
    if _ASKING_ABOUT.match(lowered) or _WANTS_EXPLANATION.search(lowered):
        return False
    return bool(_MAKE.search(lowered))


def map_text(text: str) -> Optional[Dict[str, Any]]:
    """Turn a sentence into one Ludo action, or return None.

    None means "not a Ludo request I recognise", and the caller must
    let the turn go to a model rather than guessing -- a wrong guess
    here spends money.

    Returns {"action": str, "params": {...}, "summary": str}.
    """
    if not text or not str(text).strip():
        return None

    if not names_ludo(text) or names_another_tool(text):
        return None

    stripped = _without_tool(text)
    lowered = stripped.lower()

    if _ASKING_ABOUT.match(lowered) or _WANTS_EXPLANATION.search(lowered):
        return None

    if not _MAKE.search(lowered):
        # No verb asking for anything to be made. "the car in Ludo is
        # red" is a remark, not an order.
        return None

    subject = _subject(stripped)

    for pattern, action, build, role in ACTIONS:
        if not re.search(pattern, lowered):
            continue
        return _plan(action, build, lowered, subject, pattern, role)

    # Nothing special was named. Ludo turns a sentence into a picture,
    # so that is the answer -- unless the sentence asked for a model,
    # which is the one reading worth two generations.
    if _WANTS_3D.search(lowered):
        if _VEHICLE.search(lowered):
            return _plan("generate_vehicle", _params_model, lowered, subject)
        if _CHARACTER.search(lowered):
            return _plan("generate_character", _params_model, lowered, subject)
        return _plan("generate_model", _params_model, lowered, subject)

    return _plan("generate_image", _params_image, lowered, subject)


def _plan(action, build, lowered: str, subject: str,
          pattern: str = "", role: str = SUBJECT) -> Optional[Dict[str, Any]]:
    """One action, its parameters, and what it will cost."""
    wanted = subject
    if role == FORMAT and pattern:
        # The matched word named the output, not the thing. Take it
        # out, or Ludo is asked for a picture of the word.
        wanted = re.sub(pattern, " ", wanted, flags=re.I)
        wanted = re.sub(r"\b(?:of|for|with)\b", " ", wanted, flags=re.I)
        wanted = re.sub(r"\s{2,}", " ", wanted).strip(" ,.!?-")

    params = {name: value for name, value in build(lowered, wanted).items()
              if value is not None}

    if not str(params.get("prompt") or "").strip():
        # "Make a sprite sheet in Ludo" -- of WHAT? Answered by asking,
        # not by guessing and not by handing the turn to a model that
        # would offer to help. Nothing is spent.
        params.pop("prompt", None)
        return {"action": action, "params": params, "needs": ["prompt"],
                "summary": describe(action, params)}

    return {"action": action, "params": params, "needs": [],
            "summary": describe(action, params)}


def describe(action: str, params: Dict[str, Any]) -> str:
    """A sentence saying what will be asked for, and what it will cost.

    Shown before anything runs. Ludo charges per generation, and the
    two-call actions charge twice -- a person should see that before
    it happens, not on their invoice.
    """
    two_calls = {"generate_model", "generate_character", "generate_vehicle",
                 "generate_sprite_sheet", "generate_animation"}

    nouns = {
        "generate_model": "a 3D model", "generate_character": "a character model",
        "generate_vehicle": "a vehicle model",
        "generate_environment": "a background image",
        "generate_sprite_sheet": "a sprite sheet",
        "generate_animation": "a short video", "generate_texture": "a texture",
        "generate_material_texture": "a material texture",
        "generate_audio": "a sound effect", "generate_music": "music",
        "generate_voice": "a spoken line", "generate_image": "an image",
    }

    what = nouns.get(action, action)
    prompt = str(params.get("prompt") or "").strip()
    parts = [f"ask Ludo.ai for {what}" + (f" of {prompt}" if prompt else "")]

    if params.get("style"):
        parts.append(f"in the {params['style']} style")
    if params.get("frames"):
        parts.append(f"over {params['frames']} frames")
    if params.get("polycount"):
        parts.append(f"at about {params['polycount']} faces")
    if params.get("duration"):
        parts.append(f"about {params['duration']} seconds long")

    cost = ("two generations, because Ludo has no text-to-3D and no "
            "one-call sprite sheet -- an image first, then the conversion"
            if action in two_calls else "one generation")
    return ", ".join(parts) + f" ({cost})"
