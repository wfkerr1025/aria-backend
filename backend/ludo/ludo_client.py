"""ARIA Lite - talking to Ludo.ai.

EVERY ENDPOINT, PARAMETER AND ENUM HERE WAS READ, NOT REMEMBERED
---------------------------------------------------------------
Fetched from https://api.ludo.ai/api-documentation/swagger.json
(Ludo.ai API 0.9.9) before a line of this was written. That is not
caution for its own sake: four earlier guesses about the Unity CLI
were wrong -- `--project` does not exist, `--mode` is test-only,
`list` returns `data.tools`, and a pager would hang -- and each cost a
round of "it says it worked and nothing happened".

The spec is Swagger 2.0: bodies are `parameters[in=body]` and types
live under `definitions`, not `components/schemas`.

WHAT THE API IS, AND IS NOT
---------------------------
Ludo generates images, sprites, video, audio and 3D models. It does
NOT take a text prompt for a 3D model: `/assets/3d-model` requires an
`image`, so text -> 3D is two calls, and ludo_actions is where that
pipeline lives.

GENERATION COSTS REAL MONEY
---------------------------
Every POST here carries an `x-credit-action` in the spec and spends
the account's credits. That is unlike every other tool ARIA drives:
Blender is free to run and can be run again. A retry loop, an
accidental double-send, or a chat message read too eagerly all cost
something the user cannot get back. So:

  * nothing here retries a generation automatically;
  * `request_id` is passed on every call, because the API uses it to
    recognise a repeat and not charge twice; and
  * the natural-language layer requires the user to name Ludo, and
    the caller is expected to confirm before spending.

WHY THIS DOES NOT USE tools.http_fetch
--------------------------------------
That helper is the right one for a public JSON endpoint and is used
for the connection test. It cannot express two things this needs: the
difference between 200 (here is your asset) and 202 (queued, go poll),
which it discards; and a binary download, which it would try to decode
as text.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "ART_STYLES",
    "asset_url",
    "asset_urls",
    "ASPECT_RATIOS",
    "AUDIO_DURATIONS",
    "ENDPOINTS",
    "IMAGE_TYPES",
    "JOINT_NAMINGS",
    "LudoError",
    "LudoUnavailable",
    "MODELS",
    "PLUGIN_ID",
    "RIG_TYPES",
    "SPRITE_FRAMES",
    "TEXTURE_SIZES",
    "TEXTURE_TYPES",
    "api_key",
    "call",
    "download",
    "poll_job",
]

PLUGIN_ID = "ludo"
FIELD_KEY = "api_key"
ENV_BASE = "ARIA_LUDO_API_BASE"
ENV_KEY = "ARIA_LUDO_API_KEY"

# host + basePath from the spec. Overridable so a test never reaches
# the real API -- every call here costs credits.
API_BASE = "https://api.ludo.ai/api"

# Measured, not assumed. The docs page renders the header name as
# "Authentication:", and only "Authorization: ApiKey <key>" is read.
AUTH_HEADER = "Authorization"
AUTH_SCHEME = "ApiKey"

# Endpoint paths, exactly as the spec lists them.
ENDPOINTS = {
    "image": "/assets/image",
    "image_edit": "/assets/image/edit",
    "image_style": "/assets/image/style",
    "remove_background": "/assets/image/remove-background",
    "model_3d": "/assets/3d-model",
    "model_3d_rig": "/assets/3d-model/rig",
    "model_3d_animate": "/assets/3d-model/animate",
    "model_3d_animate_preset": "/assets/3d-model/animate-preset",
    "sprite_animate": "/assets/sprite/animate",
    "sprite_keyframes": "/assets/sprite/animate-keyframes",
    "sprite_rotate": "/assets/sprite/rotate",
    "sprite_pose": "/assets/sprite/pose",
    "video": "/assets/video",
    "music": "/audio/music",
    "sound_effect": "/audio/sound-effect",
    "speech": "/audio/speech",
    "speech_preset": "/audio/speech-preset",
    "voice": "/audio/voice",
    "job": "/assets/jobs/{id}",
    "validate": "/auth/validate-api-key",
}

# Allowlists, straight from the spec's enums. A value outside one of
# these is refused here rather than spending a credit to be told no.
IMAGE_TYPES = (
    "generic", "screenshot", "art", "asset", "sprite", "sprite-vfx",
    "sprite-tiling-horizontal", "sprite-tiling-vertical", "icon", "logo",
    "ui_asset", "fixed_background", "side_scrolling_background",
    "vertical_scrolling_background", "parallax_layer", "texture", "tile",
    "item-icon", "portrait", "card-art", "splash", "3d",
)

ART_STYLES = (
    "Any style", "Cel-Shaded", "Inked Painterly", "Illustration",
    "Western Cartoon", "Anime/Manga", "Chibi", "8-Bit", "16-Bit", "32-Bit",
    "Hi-Bit", "Retro 2D", "Hand-Painted", "Digital Painting", "Comic Book",
    "Block Print", "Sketch", "Watercolor", "Stylized 3D", "Pixar Style",
    "Low Poly", "Photorealistic 3D", "Voxel Art", "Retro 3D", "Flat Design",
    "Minimalist", "Silhouette", "Noir", "Neon", "Glitch Art", "Claymation",
    "Paper Craft", "Textile",
)

ASPECT_RATIOS = ("default", "ar_1_1", "ar_4_3", "ar_16_9", "ar_19_9",
                 "ar_3_4", "ar_9_16", "ar_9_19")

# Not free integers: the sprite endpoint takes these and nothing else.
SPRITE_FRAMES = (4, 9, 16, 25, 36, 49, 64)

TEXTURE_SIZES = (1024, 2048)
TEXTURE_TYPES = ("pbr", "simple", "none")

RIG_TYPES = ("general", "humanoid", "game", "humanoid_template",
             "humanoid_template_hands")

# Which engine's bone names the rig comes out with. "unreal" and
# "godot" are here; Unity is not one of the options, and mixamo is the
# one Unity's humanoid avatar mapper handles best.
JOINT_NAMINGS = ("smpl", "mixamo", "humanik", "unreal", "godot", "rigify",
                 "vroid")

MODELS = ("blitz", "standard", "eagle", "eagle-audio", "forge", "forge-pixel",
          "tango")

AUDIO_DURATIONS = tuple(range(0, 181, 10))

# A generation is not a chat lookup. The spec says a synchronous call
# can run for fifteen minutes before it gives up and hands back a job.
REQUEST_TIMEOUT_SECONDS = 180
DOWNLOAD_TIMEOUT_SECONDS = 300

# The job endpoint long-polls for up to 60 seconds per the spec, which
# is far cheaper than asking repeatedly.
JOB_WAIT_SECONDS = 30

# HOW LONG A CHAT TURN MAY WAIT, AND WHY IT IS NOT FIFTEEN MINUTES
# ----------------------------------------------------------------
# This was 900. Measured on the developer's machine: the WebSocket
# handler reads packets with `async for raw in self.websocket:` and
# then `await self._dispatch(packet)`, so NOTHING ELSE IS READ while a
# turn runs -- heartbeats included. A 25-second generation already
# stalled every heartbeat_ack until it finished; a queued job polled
# for 900 seconds would have frozen the entire UI for fifteen minutes
# with no output at all.
#
# Two minutes is what a chat turn can afford. A generation that runs
# longer is not lost: poll_job raises with the job id, the reply hands
# it over, and collect() fetches it afterwards.
JOB_TOTAL_SECONDS = 120

# What a caller with its own patience may wait -- a script, not a chat
# turn. The API's own ceiling for a synchronous call is 15 minutes.
JOB_MAX_SECONDS = 900

# The least time between two asks, whatever the server says. See
# poll_job: without it, an endpoint that answers instantly turns this
# into a hot loop.
MIN_POLL_SECONDS = 0.5

TERMINAL_STATES = frozenset({"succeeded", "failed", "canceled"})

# How big a downloaded asset may be. A 3D model with PBR textures is
# large; a gigabyte is not an asset, it is a mistake.
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024


class LudoUnavailable(RuntimeError):
    """No usable API key, or the plugin is off."""


class LudoError(RuntimeError):
    """Ludo answered, and the answer was a refusal."""


class LudoStillRunning(LudoError):
    """The generation outlived the turn's patience.

    NOT a failure, and the distinction is the whole point: the work is
    happening and has been charged for. Losing the job id here would
    mean paying for an asset nobody can ever reach, so it is carried
    on the exception and handed to the user to collect.
    """

    def __init__(self, job_id: str, waited: int):
        self.job_id = str(job_id)
        self.waited = int(waited)
        super().__init__(
            f"Ludo is still working on it after {waited} seconds. "
            f"Nothing is lost -- the job is {self.job_id}.")


def api_base() -> str:
    return str(os.environ.get(ENV_BASE) or API_BASE).rstrip("/")


def api_key() -> str:
    """The configured key. Environment first, then the plugin.

    Never logged, never returned to the UI, and never put in a URL --
    it goes in a header, which is where the spec says it goes.
    """
    from_env = str(os.environ.get(ENV_KEY) or "").strip()
    if from_env:
        return from_env

    from backend.plugins import plugin_settings

    plugin = plugin_settings.load_plugins().get(PLUGIN_ID)
    if plugin is None or plugin.get("dismissed", False):
        raise LudoUnavailable("The Ludo.ai plugin is not installed.")
    if not plugin.get("enabled", False):
        raise LudoUnavailable(
            "The Ludo.ai plugin is installed but switched off. Enable it on "
            "its page under Plugins first.")

    key = str(plugin.get(FIELD_KEY) or "").strip()
    if not key:
        raise LudoUnavailable(
            "No Ludo.ai API key is set. Add one on its page under Plugins.")
    return key


def _headers(key: str) -> Dict[str, str]:
    return {
        AUTH_HEADER: f"{AUTH_SCHEME} {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def choice(value: Any, allowed, field: str, default=None):
    """One of a fixed set, or a refusal before any credit is spent.

    The same discipline as the Blender templates: an absent value
    takes the default, and a present value that is not allowed is an
    error rather than a silent substitution. Sending "8k" as a texture
    size would cost a credit to be told no.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    candidate = value.strip() if isinstance(value, str) else value

    match = {str(option).casefold(): option for option in allowed}.get(
        str(candidate).casefold())
    if match is None:
        raise LudoError(
            f"{field}: {value!r} is not one of "
            + ", ".join(str(option) for option in allowed))
    return match


def _result_of(payload: Any) -> Any:
    """The useful part of an answer, whatever shape it arrived in.

    A job's `result` is whatever its endpoint would have returned
    directly -- so an image job holds a LIST and a 3D job holds an
    object. An earlier version unwrapped only the dict, which meant a
    collected image job came back as the whole job record and
    asset_url found nothing in it: "there is no asset in it to fetch",
    about a generation that had succeeded and been paid for.

    The same shape mistake as asset_url's, one layer down, found the
    same way -- by a test using the real array.
    """
    if isinstance(payload, dict) and payload.get("result") is not None:
        return payload["result"]
    return payload


def poll_job(job_id: str, *, key: Optional[str] = None,
             total_seconds: Optional[int] = None,
             on_progress: Optional[Any] = None) -> dict:
    """Wait for a queued generation to finish.

    Uses the endpoint's own `wait` parameter, which long-polls for up
    to 60 seconds, rather than asking again in a tight loop -- the
    server is better placed to know when the answer is ready than a
    timer here is.
    """
    token = key or api_key()
    url = f"{api_base()}{ENDPOINTS['job'].format(id=job_id)}"

    # Read here rather than captured as a default argument. A default
    # binds at import, so the deadline could not be changed by a
    # setting or by a test -- which is how a two-second test spent two
    # minutes.
    total_seconds = int(JOB_TOTAL_SECONDS if total_seconds is None
                        else total_seconds)
    deadline = time.monotonic() + max(1, total_seconds)

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LudoStillRunning(job_id, total_seconds)

        wait = max(1, min(JOB_WAIT_SECONDS, int(remaining)))
        response = requests.get(url, params={"wait": wait},
                                headers=_headers(token),
                                timeout=wait + 30)
        if response.status_code >= 400:
            raise LudoError(
                f"Could not read job {job_id}: HTTP {response.status_code} "
                f"{response.text[:200]}")

        job = response.json() if response.content else {}
        status = str(job.get("status") or "").lower()

        if on_progress is not None:
            try:
                on_progress(status, job_id)
            except Exception:  # pragma: no cover - decoration must not break
                logger.debug("a Ludo progress callback raised; continuing")

        if status == "succeeded":
            return _result_of(job)
        if status in ("failed", "canceled"):
            error = job.get("error") or {}
            raise LudoError(
                f"Ludo {status} the job: "
                f"{error.get('message') or 'no reason given'}")

        # Not terminal. The server may say how long to leave it.
        #
        # A FLOOR, NOT JUST THE SERVER'S NUMBER. Against the real API
        # each GET blocks server-side for `wait` seconds, so the loop
        # paces itself. Against anything that answers immediately --
        # a proxy, a stub, a test -- there was no sleep at all and
        # this span a hot loop for the whole deadline. Measured: two
        # tests took four minutes between them and pinned a core.
        pause = job.get("poll_after_ms")
        seconds = (float(pause) / 1000.0
                   if isinstance(pause, (int, float)) and pause > 0
                   else MIN_POLL_SECONDS)
        time.sleep(min(30.0, max(MIN_POLL_SECONDS, seconds)))


def call(endpoint: str, payload: Dict[str, Any], *,
         key: Optional[str] = None,
         timeout: int = REQUEST_TIMEOUT_SECONDS,
         wait: bool = True,
         total_seconds: Optional[int] = None,
         on_progress: Optional[Any] = None) -> dict:
    """One generation call, and its answer.

    `endpoint` is a key of ENDPOINTS, never a raw URL: a caller cannot
    reach a path this module has not read the schema for.

    A 200 carries the asset. A 202 means it was queued, and the job is
    polled to completion unless `wait` is False -- in which case the
    job record is handed back so a caller can poll itself.
    """
    path = ENDPOINTS.get(str(endpoint))
    if path is None:
        raise LudoError(f"{endpoint!r} is not an endpoint I know.")

    token = key or api_key()
    body = {name: value for name, value in (payload or {}).items()
            if value is not None}

    # The API uses request_id to recognise a repeat and not charge for
    # it twice. Sending one on every call is the difference between a
    # retry that is free and a retry that is not.
    body.setdefault("request_id", uuid.uuid4().hex)

    url = f"{api_base()}{path}"
    logger.info("ludo: POST %s", path)

    try:
        response = requests.post(url, json=body, headers=_headers(token),
                                 timeout=timeout)
    except requests.RequestException as error:
        raise LudoError(f"Could not reach Ludo.ai: {error}") from error

    if response.status_code >= 400:
        detail = ""
        try:
            problem = response.json()
            detail = str(problem.get("message") or problem.get("error") or "")
        except ValueError:
            detail = response.text[:200]
        raise LudoError(f"Ludo.ai refused: HTTP {response.status_code}"
                        + (f" -- {detail}" if detail else ""))

    answer = response.json() if response.content else {}

    if response.status_code == 202 or (isinstance(answer, dict)
                                       and answer.get("status") in TERMINAL_STATES
                                       or isinstance(answer, dict)
                                       and answer.get("status") in ("queued", "running")):
        job_id = str(answer.get("id") or "") if isinstance(answer, dict) else ""
        if job_id and wait:
            logger.info("ludo: queued as job %s", job_id)
            return poll_job(job_id, key=token, total_seconds=total_seconds,
                            on_progress=on_progress)
        return answer if isinstance(answer, dict) else {}

    return _result_of(answer)


# The keys an asset URL can arrive under, by result type. Read from
# the spec's Model3DResult / ImageResult / SpriteResult / AudioResult /
# VideoResult definitions -- they do not agree on a name, so nothing
# here can just look for "url".
URL_KEYS = ("model_url", "spritesheet_url", "gif_url", "video_url", "url")


def asset_url(result: Any) -> Optional[str]:
    """The downloadable asset in a result, whatever shape it arrived in.

    THE LIST IS NOT A DETAIL. Measured against the live API: a real
    generation succeeded and this returned None, so a paid-for image
    was thrown away with the message "there is no URL to download".

    The reason is in the spec, and I had read half of it. Three
    endpoints return three shapes:

        POST /assets/image        -> ARRAY of ImageResult   (n can be >1)
        POST /assets/3d-model     -> Model3DResult          (one object)
        POST /audio/sound-effect  -> AudioResult            (one object)

    Reading ImageResult and assuming the wrapper matched the other two
    is exactly the mistake this codebase keeps paying for. Here it
    cost a credit.
    """
    if isinstance(result, list):
        for entry in result:
            found = asset_url(entry)
            if found:
                return found
        return None

    if not isinstance(result, dict):
        return None
    for name in URL_KEYS:
        value = result.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def asset_urls(result: Any) -> list:
    """Every downloadable asset in a result.

    `n` on /assets/image asks for more than one picture, and each is
    paid for. Returning only the first would quietly discard the rest.
    """
    if isinstance(result, list):
        return [url for url in (asset_url(entry) for entry in result) if url]
    single = asset_url(result)
    return [single] if single else []


def download(url: str, path: str, *, timeout: int = DOWNLOAD_TIMEOUT_SECONDS) -> dict:
    """Fetch a generated asset to a file.

    Streamed and capped. Written to a neighbouring .part first and
    moved into place at the end, so a half-downloaded file is never
    left sitting where something else will try to import it.
    """
    target = Path(str(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")

    try:
        with requests.get(str(url), stream=True, timeout=timeout) as response:
            if response.status_code >= 400:
                return {"success": False, "path": None,
                        "error": (f"Could not download the asset: HTTP "
                                  f"{response.status_code}")}

            written = 0
            with open(partial, "wb") as handle:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > MAX_DOWNLOAD_BYTES:
                        handle.close()
                        partial.unlink(missing_ok=True)
                        return {"success": False, "path": None,
                                "error": (f"The asset is larger than "
                                          f"{MAX_DOWNLOAD_BYTES // (1024 * 1024)}MB "
                                          f"and was not kept.")}
                    handle.write(chunk)

        if written == 0:
            partial.unlink(missing_ok=True)
            return {"success": False, "path": None,
                    "error": "The asset came back empty."}

        partial.replace(target)
        return {"success": True, "path": str(target), "bytes": written,
                "error": None}

    except requests.RequestException as error:
        partial.unlink(missing_ok=True)
        return {"success": False, "path": None,
                "error": f"Could not download the asset: {error}"}
    except OSError as error:
        partial.unlink(missing_ok=True)
        return {"success": False, "path": None,
                "error": f"Could not write {target}: {error}"}


def collect(job_id: str, *, key: Optional[str] = None,
            total_seconds: Optional[int] = None) -> dict:
    """Fetch a generation that outran the turn that started it.

    The other half of LudoStillRunning. The work was paid for when it
    was queued, so this costs nothing and is the reason a timeout is
    an inconvenience rather than a loss.
    """
    return poll_job(str(job_id).strip(), key=key, total_seconds=total_seconds)
