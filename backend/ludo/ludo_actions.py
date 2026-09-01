"""ARIA Lite - asking Ludo.ai to make something.

The high-level verbs. Each one is a small, fixed recipe over
ludo_client, and each returns the same shape whether it worked or not:

    {"success": bool, "ran": bool, "error": str|None,
     "kind": str, "url": str|None, "result": dict|None, "steps": [...]}

`ran` is the one that matters, and it means "a generation was
attempted and credits may have been spent". A refusal before any call
has ran=False, and never claims otherwise.

THREE THINGS THE SPEC ASKED FOR THAT THE API DOES NOT DO
--------------------------------------------------------
Written down because the alternative is a function that looks like it
works. Read from the Ludo.ai OpenAPI spec (0.9.9), not assumed:

1. THERE IS NO TEXT-TO-3D ENDPOINT. `/assets/3d-model` requires an
   `image`. So generate_model() is two calls -- an image first, then
   the conversion -- and both are reported in `steps`, because two
   calls is two lots of credits and the user should see that.

2. AN IMAGE HAS NO RESOLUTION SETTING. `/assets/image` takes
   `aspect_ratio` and nothing else about size. generate_texture()
   therefore refuses a `resolution` rather than accepting it and
   quietly producing whatever Ludo felt like. `texture_size`
   (1024/2048) is real, but it belongs to the 3D conversion.

3. A VOICE NEEDS WORDS. `/audio/voice` requires `voice_description`
   AND `text`. "Generate a voice" on its own has nothing to say, so
   generate_voice() asks for the line rather than inventing one.

SPRITE SHEETS AND VIDEO ARE ALSO TWO CALLS, for the same reason as
the 3D models: `/assets/sprite/animate` needs an `initial_image` and
`/assets/video` needs an `image`.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.ludo import ludo_client as client
from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "download_asset",
    "generate_animation",
    "generate_audio",
    "generate_character",
    "generate_environment",
    "generate_material_texture",
    "generate_model",
    "generate_music",
    "generate_sprite_sheet",
    "generate_texture",
    "generate_vehicle",
    "generate_voice",
    "output_dir",
    "rig_model",
    "save_asset",
    "validate_asset",
]

# The default face budget for a generated model. The API's own default
# is 50000, which is a film asset rather than a game one; 20000 is a
# hero prop a real-time engine will not choke on. Overridable per call.
DEFAULT_FACES = 20_000

# What each verb produces, and therefore what extension its file gets.
KIND_SUFFIXES = {
    "model": ".glb",
    "rig": ".glb",
    "animation": ".mp4",
    "image": ".png",
    "texture": ".png",
    "sprite_sheet": ".png",
    "audio": ".mp3",
    "music": ".mp3",
    "voice": ".mp3",
}


def output_dir() -> Path:
    """Where Ludo's assets are written.

    The shared resolver, same as Blender: ARIA_LUDO_OUTPUT, then the
    plugin's own `output_dir`, then Documents/ARIA/Ludo. One answer to
    "where do the files go", because two copies drift and the one
    nobody updated is the one somebody is using.
    """
    from backend.plugins import plugin_settings

    return plugin_settings.output_dir(client.PLUGIN_ID)


def _failed(error: str, kind: str = "", steps: Optional[List] = None,
            ran: bool = False) -> dict:
    return {"success": False, "ran": ran, "error": str(error), "kind": kind,
            "url": None, "result": None, "steps": steps or []}


def _done(kind: str, result: dict, steps: List[str]) -> dict:
    return {"success": True, "ran": True, "error": None, "kind": kind,
            "url": client.asset_url(result), "result": result, "steps": steps}


def _guard(function):
    """Turn a Ludo failure into an answer instead of an exception.

    Every verb below is called from a chat turn eventually, and an
    exception there hands the turn back to a model -- which is the one
    outcome this whole layer exists to prevent.
    """
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except client.LudoUnavailable as error:
            return _failed(str(error), ran=False)
        except client.LudoStillRunning as error:
            # NOT a failure. The work is happening and was charged for,
            # so the job id travels with the answer.
            answer = _failed(str(error), ran=True)
            answer["job_id"] = error.job_id
            answer["pending"] = True
            return answer
        except client.LudoError as error:
            # Ran far enough to be refused, so credits MAY have gone.
            return _failed(str(error), ran=True)
        except Exception as error:  # pragma: no cover - defensive
            logger.exception("a Ludo action fell over")
            return _failed(f"Something went wrong on my side: {error}")

    # functools.wraps, not a hand-copied __name__: without __wrapped__
    # inspect.signature reports (*args, **kwargs), so a caller passing
    # a keyword the action does not take gets a TypeError at the worst
    # possible moment instead of being caught by a test.
    return functools.wraps(function)(wrapped)


# ======================================================
# A. Images, and the models made from them
# ======================================================

@_guard
def generate_image(prompt: str, *, image_type: str = "generic",
                   style: Optional[str] = None,
                   aspect_ratio: Optional[str] = None,
                   perspective: Optional[str] = None) -> dict:
    """The base primitive. Everything visual starts here."""
    if not str(prompt or "").strip():
        return _failed("I need something to make. Say what it should be.",
                       "image")

    payload = {
        "image_type": client.choice(image_type, client.IMAGE_TYPES,
                                    "image_type", "generic"),
        "prompt": str(prompt).strip(),
        "art_style": client.choice(style, client.ART_STYLES, "style"),
        "aspect_ratio": client.choice(aspect_ratio, client.ASPECT_RATIOS,
                                      "aspect_ratio"),
        "perspective": perspective or None,
    }
    result = client.call("image", payload)
    return _done("image", result, ["image"])


@_guard
def generate_model(prompt: str, *, style: Optional[str] = None,
                   polycount: Optional[int] = None,
                   texture_size: Optional[int] = None,
                   texture_type: Optional[str] = None,
                   image_type: str = "3d") -> dict:
    """A 3D model from a description. TWO calls, and two lots of credits.

    Ludo has no text-to-3D endpoint: an image is generated first and
    then converted. Both steps are named in `steps` so the cost is
    visible rather than implied.

    `polycount` is the spec's word for the API's `target_num_faces`.
    """
    if not str(prompt or "").strip():
        return _failed("I need something to make. Say what it should be.",
                       "model")

    faces = DEFAULT_FACES if polycount is None else int(polycount)
    if faces < 100:
        return _failed(f"{faces} faces is not a model anyone could use.",
                       "model")

    # EVERY enum checked before the FIRST call, not before the one it
    # belongs to. Measured: an invalid texture_size raised on the
    # second request, which meant the first had already generated an
    # image and charged for it. A parameter this layer could have
    # refused for free must never cost a credit.
    kind = client.choice(image_type, client.IMAGE_TYPES, "image_type", "3d")
    art = client.choice(style, client.ART_STYLES, "style")
    size = client.choice(texture_size, client.TEXTURE_SIZES, "texture_size")
    surface = client.choice(texture_type, client.TEXTURE_TYPES, "texture_type")

    concept = client.call("image", {
        "image_type": kind,
        "prompt": str(prompt).strip(),
        "art_style": art,
    })
    picture = client.asset_url(concept)
    if not picture:
        return _failed("Ludo made a concept image but did not say where it is.",
                       "model", ["image"], ran=True)

    model = client.call("model_3d", {
        "image": picture,
        "target_num_faces": faces,
        "texture_size": size,
        "texture_type": surface,
    })

    answer = _done("model", model, ["image", "model_3d"])
    answer["concept_url"] = picture
    return answer


def generate_character(prompt: str, *, style: Optional[str] = None,
                       polycount: Optional[int] = None) -> dict:
    """A character as a 3D model.

    Framed as a full-body reference so the conversion has something to
    work from -- a portrait crop makes a bust, not a character.
    """
    described = f"full body character, {str(prompt or '').strip()}, T-pose, " \
                f"neutral background, front view"
    return generate_model(described, style=style, polycount=polycount)


def generate_vehicle(prompt: str, *, style: Optional[str] = None,
                     polycount: Optional[int] = None) -> dict:
    """A vehicle as a 3D model."""
    described = f"{str(prompt or '').strip()}, vehicle, three-quarter view, " \
                f"whole vehicle in frame, neutral background"
    return generate_model(described, style=style, polycount=polycount)


def generate_environment(prompt: str, *, style: Optional[str] = None,
                         three_d: bool = False,
                         polycount: Optional[int] = None) -> dict:
    """A place.

    An image by default, not a model. "Create a forest" wants a scene,
    and converting a scene to a single mesh gives you a diorama nobody
    can walk through. Pass three_d=True when a prop is what was meant.
    """
    described = str(prompt or "").strip()
    if three_d:
        return generate_model(described, style=style, polycount=polycount)
    return generate_image(described, image_type="fixed_background",
                          style=style, aspect_ratio="ar_16_9")


@_guard
def rig_model(model_url: str, *, rig_type: Optional[str] = None,
              joint_naming: str = "mixamo") -> dict:
    """Put a skeleton in a model Ludo already made.

    Defaults to mixamo bone names, which is the naming Unity's
    humanoid avatar mapper handles best -- "unity" is not one of the
    options the API offers.
    """
    if not str(model_url or "").strip():
        return _failed("I need a model to rig.", "rig")

    result = client.call("model_3d_rig", {
        "model": str(model_url).strip(),
        "rig_type": client.choice(rig_type, client.RIG_TYPES, "rig_type"),
        "joint_naming": client.choice(joint_naming, client.JOINT_NAMINGS,
                                      "joint_naming", "mixamo"),
    })
    return _done("rig", result, ["model_3d_rig"])


# ======================================================
# B. Textures
# ======================================================

@_guard
def generate_texture(prompt: str, *, resolution: Optional[int] = None,
                     style: Optional[str] = None, tiling: bool = True) -> dict:
    """A texture image.

    `resolution` is refused rather than ignored. Ludo's image endpoint
    has no size parameter at all -- only `aspect_ratio` -- so accepting
    a number here and returning whatever came back would be a setting
    that does nothing, which is worse than one that says so.
    """
    if resolution is not None:
        return _failed(
            "Ludo's image endpoint has no resolution setting, so I cannot "
            "make a texture at a size you choose. What it does take is an "
            "aspect ratio. (texture_size, 1024 or 2048, is real but belongs "
            "to the 3D conversion, not to a texture image.)", "texture")

    kind = "sprite-tiling-horizontal" if tiling else "texture"
    return generate_image(str(prompt or "").strip(), image_type=kind,
                          style=style, aspect_ratio="ar_1_1")


def generate_material_texture(prompt: str, *,
                              style: Optional[str] = None) -> dict:
    """A texture meant to be read as a surface material."""
    described = (f"seamless tileable {str(prompt or '').strip()} material "
                 f"texture, flat lighting, top-down, no shadows")
    return generate_image(described, image_type="texture", style=style,
                          aspect_ratio="ar_1_1")


# ======================================================
# C. Sprites and animation
# ======================================================

@_guard
def generate_sprite_sheet(prompt: str, *, frames: int = 16,
                          style: Optional[str] = None,
                          motion: Optional[str] = None,
                          frame_size: Optional[int] = None) -> dict:
    """A sprite sheet. TWO calls: a sprite first, then its animation.

    `frames` is not a free number -- the API takes 4, 9, 16, 25, 36,
    49 or 64 and refuses anything else, so it is checked here before a
    credit is spent finding that out.
    """
    if not str(prompt or "").strip():
        return _failed("I need something to animate.", "sprite_sheet")

    count = client.choice(frames, client.SPRITE_FRAMES, "frames", 16)
    art = client.choice(style, client.ART_STYLES, "style")

    sprite = client.call("image", {
        "image_type": "sprite",
        "prompt": str(prompt).strip(),
        "art_style": art,
    })
    picture = client.asset_url(sprite)
    if not picture:
        return _failed("Ludo made a sprite but did not say where it is.",
                       "sprite_sheet", ["image"], ran=True)

    sheet = client.call("sprite_animate", {
        "initial_image": picture,
        "motion_prompt": str(motion or prompt).strip(),
        "frames": count,
        "frame_size": frame_size,
        "image_type": "sprite",
    })

    answer = _done("sprite_sheet", sheet, ["image", "sprite_animate"])
    answer["frames"] = count
    answer["source_url"] = picture
    return answer


@_guard
def generate_animation(prompt: str, *, duration: Optional[float] = None,
                       style: Optional[str] = None,
                       model: Optional[str] = None) -> dict:
    """A short video. TWO calls: a first frame, then the motion.

    `/assets/video` needs an `image` to animate as well as a prompt,
    so there is no one-call version of this either.
    """
    if not str(prompt or "").strip():
        return _failed("I need something to animate.", "animation")

    art = client.choice(style, client.ART_STYLES, "style")
    engine = client.choice(model, client.MODELS, "model")

    first = client.call("image", {
        "image_type": "art",
        "prompt": str(prompt).strip(),
        "art_style": art,
    })
    picture = client.asset_url(first)
    if not picture:
        return _failed("Ludo made a first frame but did not say where it is.",
                       "animation", ["image"], ran=True)

    video = client.call("video", {
        "image": picture,
        "prompt": str(prompt).strip(),
        "duration": float(duration) if duration else None,
        "model": engine,
    })

    answer = _done("animation", video, ["image", "video"])
    answer["first_frame_url"] = picture
    return answer


# ======================================================
# D. Audio and voice
# ======================================================

@_guard
def generate_audio(prompt: str, *, duration: Optional[float] = None,
                   loop: bool = False) -> dict:
    """A sound effect."""
    if not str(prompt or "").strip():
        return _failed("I need to know what it should sound like.", "audio")

    result = client.call("sound_effect", {
        "description": str(prompt).strip(),
        "duration": float(duration) if duration else None,
        "loop": bool(loop),
    })
    return _done("audio", result, ["sound_effect"])


@_guard
def generate_music(prompt: str, *, duration: Optional[int] = None,
                   lyrics: Optional[str] = None) -> dict:
    """A piece of music. Duration is a fixed ladder, 0 to 180 by tens."""
    if not str(prompt or "").strip():
        return _failed("I need to know what it should sound like.", "music")

    result = client.call("music", {
        "description": str(prompt).strip(),
        "lyrics": lyrics or None,
        "duration": client.choice(duration, client.AUDIO_DURATIONS,
                                  "duration"),
    })
    return _done("music", result, ["music"])


@_guard
def generate_voice(prompt: str, *, text: Optional[str] = None,
                   kind: str = "human") -> dict:
    """A spoken line in a described voice.

    `prompt` describes the voice; `text` is what it says. Both are
    required by the API, and a voice with nothing to say is not
    something this can invent -- so it asks instead of guessing.
    """
    described = str(prompt or "").strip()
    if not described:
        return _failed("I need to know what the voice should sound like.",
                       "voice")
    if not str(text or "").strip():
        return _failed(
            "I have a voice but no words. Tell me what it should say -- "
            "Ludo needs both the description and the line.", "voice")

    result = client.call("voice", {
        "voice_description": described,
        "text": str(text).strip(),
        "type": client.choice(kind, ("human", "non-human"), "type", "human"),
    })
    return _done("voice", result, ["voice"])


# ======================================================
# E. Files
# ======================================================

# The first bytes of the formats Ludo returns. A file whose name says
# one thing and whose contents say another is worth catching before
# Unity is asked to import it.
_MAGIC = {
    ".glb": (b"glTF",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    # Measured: a real image generation came back as .webp. RIFF at 0
    # and WEBP at 8, so it needs both the magic and the offset below.
    ".webp": (b"RIFF",),
    ".mp3": (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"),
    ".wav": (b"RIFF",),
    ".mp4": (b"ftyp",),      # at offset 4
    ".webm": (b"\x1a\x45\xdf\xa3",),
}

_OFFSETS = {".mp4": 4}


def _suffix_for(url: str, kind: str) -> str:
    """The extension a downloaded asset should have.

    From the URL when it says, because Ludo's own name is more
    trustworthy than a guess from the verb that asked for it.
    """
    stem = str(url or "").split("?")[0].split("#")[0]
    suffix = Path(stem).suffix.lower()
    if suffix and re.fullmatch(r"\.[a-z0-9]{2,5}", suffix):
        return suffix
    return KIND_SUFFIXES.get(str(kind), ".bin")


def _free_path(folder: Path, base: str, suffix: str) -> Path:
    """A path nothing is using. Never silently replaces a file."""
    cleaned = re.sub(r"[^\w.-]+", "_", str(base or "asset")).strip("._") or "asset"
    cleaned = cleaned[:60]
    candidate, index = folder / f"{cleaned}{suffix}", 1
    while candidate.exists():
        index += 1
        candidate = folder / f"{cleaned}_{index}{suffix}"
    return candidate


def download_asset(url: str, path: Optional[str] = None, *,
                   kind: str = "", name: str = "asset") -> dict:
    """Fetch a generated asset to disk.

    Without a path it goes to the configured output folder under a
    name derived from what was asked for.
    """
    if not str(url or "").strip():
        return {"success": False, "path": None,
                "error": "There is no URL to download."}

    if path:
        target = Path(str(path))
    else:
        try:
            folder = output_dir()
        except OSError as error:
            return {"success": False, "path": None, "error": str(error)}
        target = _free_path(folder, name, _suffix_for(url, kind))

    return client.download(str(url), str(target))


def save_asset(source: str, *, name: str = "", folder: Optional[str] = None) -> dict:
    """Put an asset in the output folder, keeping what is already there."""
    origin = Path(str(source or ""))
    if not origin.is_file():
        return {"success": False, "path": None,
                "error": f"There is no file at {source}."}

    try:
        destination = Path(folder) if folder else output_dir()
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return {"success": False, "path": None, "error": str(error)}

    target = _free_path(destination, name or origin.stem, origin.suffix)
    try:
        target.write_bytes(origin.read_bytes())
    except OSError as error:
        return {"success": False, "path": None,
                "error": f"Could not save to {target}: {error}"}

    return {"success": True, "path": str(target),
            "bytes": target.stat().st_size, "error": None}


def validate_asset(path: str) -> dict:
    """Whether a downloaded file is the thing it claims to be.

    Checks the bytes, not the name. A truncated download and an error
    page saved with a .glb extension both pass an "is it there?" test
    and both fail an import much later, somewhere less obvious.
    """
    target = Path(str(path or ""))
    if not target.is_file():
        return {"valid": False, "path": str(target),
                "error": f"There is no file at {target}."}

    size = target.stat().st_size
    if size == 0:
        return {"valid": False, "path": str(target), "bytes": 0,
                "error": "The file is empty."}

    suffix = target.suffix.lower()
    expected = _MAGIC.get(suffix)
    if not expected:
        # An extension nothing here knows. It exists and has content,
        # which is everything that can honestly be said about it.
        return {"valid": True, "path": str(target), "bytes": size,
                "checked": False, "error": None}

    offset = _OFFSETS.get(suffix, 0)
    with open(target, "rb") as handle:
        head = handle.read(offset + max(len(m) for m in expected))

    if not any(head[offset:offset + len(m)] == m for m in expected):
        return {"valid": False, "path": str(target), "bytes": size,
                "checked": True,
                "error": (f"This does not look like a {suffix} file -- it "
                          f"starts with {head[:8]!r}. A download that failed "
                          f"often lands as an error page with the right name.")}

    return {"valid": True, "path": str(target), "bytes": size,
            "checked": True, "error": None}


# ======================================================
# Answering a request in someone's own words
#
# THE FAILURE THIS EXISTS FOR
# Measured, with the Ludo plugin installed, enabled and holding a
# working key. Typed into chat:
#
#     Create a stylized cartoon girl with bright red hair, a large
#     pink bow, a pink dress, big expressive eyes, and a confident
#     heroic pose in Ludo
#
# ARIA routed it to phi-3-mini, which replied "As an AI, I can't
# directly create images, but I can guide you through the process"
# and then described the character in prose. Every piece needed to
# generate that image existed and was tested; nothing called any of it.
# ======================================================

def answer_request(text: str, *, on_status=None) -> Optional[dict]:
    """Answer a Ludo request, or hand the turn back.

    Returns None when the sentence is not this layer's business --
    not about Ludo, or a question about it that a model should answer.

    Returns {"ran": bool, "text": str, "paths": [...]} when it is.
    """
    from backend.ludo import ludo_nl_mapping as mapping

    said = str(text or "").strip()
    if not mapping.names_ludo(said) or mapping.names_another_tool(said):
        return None

    # Collecting first: it costs nothing, and "collect abc123 in Ludo"
    # has no make-verb, so map_text would return None and the turn
    # would go to a model that cannot fetch anything.
    pending = mapping.collect_request(said)
    if pending:
        return _collect(pending, on_status=on_status)

    plan = mapping.map_text(said)
    if plan is None:
        return None

    if plan["needs"]:
        # Named a format but not a subject. Asking costs nothing;
        # guessing costs a credit.
        return {"ran": False, "paths": [], "text": (
            f"I can {plan['summary']} -- but of what?\n\n"
            f"Tell me what it should be and I will ask Ludo.")}

    action = globals().get(plan["action"])
    if not callable(action):  # pragma: no cover - pinned by a test
        return {"ran": False, "paths": [],
                "text": "I could not find that action, so nothing happened."}

    logger.info("ludo: %s", plan["summary"])

    # The UI has no other sign of life during this. The WebSocket
    # handler awaits the whole turn before reading its next packet, so
    # a generation that takes half a minute looks exactly like a
    # frozen application -- heartbeat acks stop too.
    _say(on_status, "generating")

    made = action(**plan["params"])

    if made.get("pending"):
        return {"ran": True, "paths": [], "job_id": made.get("job_id"),
                "text": (f"{made['error']}\n\n"
                         f"It is still generating and has been paid for. Say "
                         f"\u201ccollect {made.get('job_id')} in Ludo\u201d "
                         f"when you want it.")}

    if not made["ran"]:
        return {"ran": False, "paths": [], "text": (
            f"I did not ask Ludo for anything, and nothing was spent.\n\n"
            f"{made['error']}")}

    if not made["success"]:
        return {"ran": True, "paths": [], "text": (
            f"I asked Ludo and it did not work out: {made['error']}\n\n"
            f"({plan['summary']}.)")}

    _say(on_status, "downloading")
    saved = download_asset(made["url"], kind=made.get("kind", ""),
                           name=_file_name(plan))
    if not saved["success"]:
        # The asset exists and was paid for -- the URL matters now.
        return {"ran": True, "paths": [], "text": (
            f"Ludo made it, but I could not download it: {saved['error']}\n\n"
            f"It is still there: {made['url']}")}

    checked = validate_asset(saved["path"])
    if not checked["valid"]:
        return {"ran": True, "paths": [saved["path"]], "text": (
            f"Ludo made it and I downloaded it, but the file is not right: "
            f"{checked['error']}\n\n"
            f"Saved anyway at {saved['path']} -- the original is {made['url']}")}

    return {"ran": True, "paths": [saved["path"]], "text": (
        f"Made it with Ludo.ai.\n\n"
        f"- {saved['path']} ({saved['bytes'] // 1024}KB)\n\n"
        f"{_sentence_case(plan['summary'])}.")}


def _collect(job_id: str, *, on_status=None) -> dict:
    """Fetch a generation that outran the turn that started it.

    Free -- it was charged for when it was queued, which is what makes
    a timeout an inconvenience rather than a loss.
    """
    _say(on_status, "generating")
    try:
        result = client.collect(job_id)
    except client.LudoStillRunning as error:
        return {"ran": False, "paths": [], "job_id": job_id, "text": (
            f"{error}\n\nStill going. Ask again in a minute.")}
    except client.LudoUnavailable as error:
        return {"ran": False, "paths": [], "text": str(error)}
    except client.LudoError as error:
        return {"ran": False, "paths": [], "text": (
            f"I could not collect {job_id}: {error}")}

    url = client.asset_url(result)
    if not url:
        return {"ran": False, "paths": [], "text": (
            f"Job {job_id} finished, but there is no asset in it to fetch.")}

    _say(on_status, "downloading")
    saved = download_asset(url, name=f"ludo_{job_id[:12]}")
    if not saved["success"]:
        return {"ran": False, "paths": [], "text": (
            f"Job {job_id} is done, but I could not download it: "
            f"{saved['error']}\n\nIt is still there: {url}")}

    return {"ran": False, "paths": [saved["path"]], "text": (
        f"Collected job {job_id}.\n\n"
        f"- {saved['path']} ({saved['bytes'] // 1024}KB)\n\n"
        f"Nothing extra was spent -- it was paid for when it was queued.")}


def _say(on_status, value: str) -> None:
    """Tell the UI something is happening, without ever failing a turn."""
    if on_status is None:
        return
    try:
        on_status(value)
    except Exception:  # pragma: no cover - decoration must not break a turn
        logger.debug("a Ludo status callback raised; continuing")


def _sentence_case(text: str) -> str:
    """Capitalise the first letter and nothing else.

    str.capitalize() lower-cases the remainder, which turned
    "Ludo.ai ... Western Cartoon style" into "ludo.ai ... western
    cartoon style" -- a product name and an API enum, both wrong.
    """
    return text[:1].upper() + text[1:] if text else text


def _file_name(plan: dict) -> str:
    """A short, readable file name from what was asked for.

    The whole prompt would be a sixty-character file name; the first
    few words are enough to recognise it in a folder.
    """
    words = re.findall(r"[A-Za-z0-9]+",
                       str((plan.get("params") or {}).get("prompt") or "asset"))
    return "_".join(words[:4]) or "asset"
