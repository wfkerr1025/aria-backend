"""ARIA Lite - a picture in, a playable character out.

    "turn D:\\Art\\wick.png into a playable character"
    "make a playable character of a dwarf miner with a lantern"

    python -m backend.blender.blender_character D:\\Art\\wick.png --name Wick [--no-unity]

THE STAGES, EACH ONE ALREADY BUILT AND MEASURED ON ITS OWN
----------------------------------------------------------
1. Ludo turns the picture into a textured 3D model (/assets/3d-model --
   ONE paid call; a description costs two, the picture first).
2. Blender, in chat's scene (a new one -- the old is kept to undo to):
   import, drop loose bits, fix normals, stand it 1.8 m tall on the
   floor, one mesh named after the character (the clean-up stage,
   measured on a real Ludo character: 0.994 units tall, origin at the
   middle, feet at -0.496).
3. Body landmarks, a Unity Humanoid skeleton, Idle and Walk.
4. Into Unity: FBX, Humanoid avatar, looping clips, controller, prefab
   (blender_to_unity).

A picture that works: the whole figure, front on, arms held a little
away from the body (an A-pose), nothing held across it -- a pick across
the chest fused into the mesh the one time it was tried, and arms
against the sides cannot be told from the body, so they get no bones.
"""

from __future__ import annotations

import argparse
import base64
import mimetypes
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from logger import get_logger

logger = get_logger(__name__)

__all__ = ["picture_to_character", "PICTURE_ADVICE", "main"]

HEIGHT = 1.8
PICTURE_ADVICE = ("the whole figure, front on, arms a little away from the body, nothing held "
                  "across it")
MAX_PICTURE_BYTES = 20 * 1024 * 1024


def _picture_for_ludo(picture: str) -> str:
    """A URL as it is; a file on disk as a data URI Ludo can read."""
    text = str(picture or "").strip().strip('"')
    if re.match(r"https?://", text, re.I):
        return text
    path = Path(text)
    if not path.is_file():
        raise FileNotFoundError(f"There is no picture at {text}.")
    if path.stat().st_size > MAX_PICTURE_BYTES:
        raise ValueError(f"{path.name} is over {MAX_PICTURE_BYTES // (1024 * 1024)} MB.")
    kind = mimetypes.guess_type(path.name)[0] or "image/png"
    if not kind.startswith("image/"):
        raise ValueError(f"{path.name} is not a picture.")
    return f"data:{kind};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _say(on_status, text: str) -> None:
    if on_status:
        try:
            on_status(text)
        except Exception:                           # a status line must never stop the work
            logger.debug("status callback failed", exc_info=True)


def _clean_name(name: str) -> str:
    cleaned = re.sub(r"[^\w-]", "_", str(name or "")).strip("_")
    return cleaned[:40] or "Character"


def picture_to_character(picture: Optional[str] = None, *, description: Optional[str] = None,
                         session=None, name: Optional[str] = None,
                         clips: Sequence[str] = ("idle", "walk"), send: bool = True,
                         project=None, faces: int = 20000,
                         on_status: Optional[Callable[[str], None]] = None) -> dict:
    """Picture (or description) -> Ludo model -> cleaned, rigged, animated -> Unity."""
    from backend.blender import blender_session, blender_to_unity
    from backend.ludo import ludo_actions, ludo_client

    session = session or blender_session.Session()
    if isinstance(session, blender_session.LiveSession):
        return {"success": False, "text": "I build characters from pictures in chat's own scene, "
                                          "not in your open Blender -- it starts a new scene. Say "
                                          "it without \"my Blender\", then \"send him to Unity\"."}
    name = _clean_name(name or (Path(str(picture)).stem if picture and not re.match(
        r"https?://", str(picture), re.I) else (description or "Character").split(",")[0].title()))
    stages: List[str] = []
    pictures: List[str] = []

    # --- 1. Ludo -------------------------------------------------------
    if picture:
        try:
            source = _picture_for_ludo(picture)
        except (OSError, ValueError) as error:
            return {"success": False, "text": f"I did not start: {error}"}
        _say(on_status, "Ludo is turning the picture into a 3D model (one credit, a minute or two)...")
        made = ludo_actions.generate_model("", image=source, polycount=faces, texture_type="simple",
                                           texture_size=2048)
    elif description:
        _say(on_status, "Ludo is drawing the character, then making it 3D (two credits)...")
        made = ludo_actions.generate_character(description, polycount=faces)
    else:
        return {"success": False, "text": "I need a picture (a file or a link) or a description."}
    if made.get("pending"):
        # Paid for already; waiting longer costs nothing. Measured: a single
        # picture took over 120 s on a busy afternoon.
        _say(on_status, "Ludo is still working -- waiting for it...")
        try:
            late = ludo_client.collect(made["job_id"], total_seconds=300)
            url = ludo_client.asset_url(late)
            if url:
                made = {**made, "success": True, "pending": False, "url": url}
        except ludo_client.LudoError as error:
            logger.info("still not collected: %s", error)
    if made.get("pending"):
        return {"success": False, "text": (
            f"Ludo is still making the model (job {made.get('job_id')}) -- it is paid for and "
            f"will not be lost. Ask again in a minute and I will collect it.")}
    if not made.get("success") or not made.get("url"):
        return {"success": False, "text": f"Ludo did not make a model: {made.get('error')}"}
    cost = made.get("cost") or {}
    stages.append(f"Ludo made the model ({cost.get('calls', '?')} paid call(s)"
                  + (f", {cost['reused']} reused" if cost.get("reused") else "") + ").")
    if made.get("concept_url") and description:
        stages.append(f"Concept picture: {made['concept_url']}")

    glb = session.folder / "ludo" / f"{name}.glb"
    fetched = ludo_client.download(made["url"], str(glb))
    if not fetched.get("success"):
        return {"success": False, "text": f"Ludo made it but the download failed: {fetched.get('error')} "
                                          f"-- it is at {made['url']}"}

    # --- 2. Blender: a new scene, the model cleaned ---------------------
    _say(on_status, "Cleaning it up in Blender...")
    session.reset()
    imported = session.run([{"action": "clear_scene", "params": {}},
                            {"action": "import_model", "params": {"path": str(glb)}}], preview=None)
    if not imported.get("success"):
        return {"success": False, "text": f"Blender could not open Ludo's model: {imported.get('error')}"}
    scene = session.describe().get("scene") or {}
    meshes = [o["name"] for o in scene.get("objects") or [] if o.get("type") == "MESH"]
    if not meshes:
        return {"success": False, "text": "Ludo's file had no mesh in it."}
    # Out of the generator's empties first, or its transform is left behind.
    tidy = [{"action": "flatten_hierarchy", "params": {}}]
    if len(meshes) > 1:
        tidy.append({"action": "join_objects", "params": {"objects": meshes}})
    tidy += [
        {"action": "apply_transforms", "params": {}},
        {"action": "remove_loose", "params": {}},
        {"action": "recalculate_normals", "params": {}},
        {"action": "scale_to_height", "params": {"height": HEIGHT}},
        {"action": "origin_to_floor", "params": {}},
        # Where it stands too: AutoRig swapped the arms and legs of a mesh
        # left 0.9 m up by Ludo's empty (measured).
        {"action": "apply_transforms", "params": {"location": True}},
        {"action": "rename_object", "params": {"object": meshes[0], "name": name}},
    ]
    cleaned = session.run(tidy, preview="material", views=["front", "right", "three_quarter"])
    if not cleaned.get("success"):
        return {"success": False, "text": f"The clean-up failed: {cleaned.get('error')}"}
    stages.append(f"Blender cleaned it: {HEIGHT} m tall, feet on the floor, one mesh called {name}.")
    pictures += (cleaned.get("renders") or [])[:1]

    # --- 3. Skeleton and clips -----------------------------------------
    _say(on_status, "Rigging and animating...")
    steps: List[Dict] = [{"action": "find_landmarks", "params": {"object": name, "kind": "body"}},
                         {"action": "auto_rig", "params": {"object": name}},
                         # Unity takes the rest pose AS the T-pose; Ludo's A-pose
                         # skewed every clip (arms thrown forward, stretched).
                         {"action": "t_pose", "params": {"armature": f"{name}_Rig"}}]
    for clip in clips:
        steps.append({"action": "add_clip", "params": {"armature": f"{name}_Rig", "clip": clip}})
    steps.append({"action": "render_preview", "params": {
        "look": "material", "views": ["right"], "frames": [1, 7, 13, 19], "size": 360}})
    rigged = session.run(steps)
    if not rigged.get("success"):
        return {"success": False, "stages": stages, "pictures": pictures, "text": (
            "\n".join(stages) + f"\n\nRigging failed: {rigged.get('error')}\n\nThe cleaned model is in "
            f"the scene; a picture of {PICTURE_ADVICE} rigs best.")}
    rig = next((n for n in rigged.get("notes") or [] if n.get("step") == "auto_rig"), {})
    marks = next((n for n in rigged.get("notes") or [] if n.get("step") == "find_landmarks"), {})
    stages.append(f"Rigged with {len(rig.get('bones') or [])} Unity Humanoid bones"
                  + ("" if rig.get("arms", True) else " -- but NO ARMS: they could not be told from "
                     "the body, so Unity will not accept it as a Humanoid")
                  + f"; clips: {', '.join(c.capitalize() for c in clips)}.")
    for note in marks.get("notes") or []:
        stages.append(f"Landmarks: {note}.")
    pictures += (rigged.get("renders") or [])[:1]

    # --- 4. Unity --------------------------------------------------------
    result = {"success": True, "name": name, "glb": str(glb), "stages": stages, "pictures": pictures}
    if send:
        _say(on_status, "Sending it to Unity...")
        sent = blender_to_unity.send(session, project=project, name=name)
        result["unity"] = sent
        stages.append(sent.get("text", ""))
        pictures += sent.get("pictures") or []
        result["success"] = bool(sent.get("success"))
    result["text"] = "\n\n".join(stages)
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    from backend.blender import blender_session

    parser = argparse.ArgumentParser(prog="python -m backend.blender.blender_character",
                                     description="A picture (or description) into a rigged, animated "
                                                 "character in Blender and Unity. Spends Ludo credits.")
    parser.add_argument("picture", nargs="?", help="a picture file or URL")
    parser.add_argument("--describe", help="a description instead of a picture (two credits)")
    parser.add_argument("--session", "-s", default=blender_session.DEFAULT_SESSION)
    parser.add_argument("--name")
    parser.add_argument("--project")
    parser.add_argument("--no-unity", action="store_true")
    args = parser.parse_args(argv)
    outcome = picture_to_character(args.picture, description=args.describe,
                                   session=blender_session.Session(args.session), name=args.name,
                                   send=not args.no_unity,
                                   project=Path(args.project) if args.project else None,
                                   on_status=print)
    print(outcome["text"])
    for picture in outcome.get("pictures") or []:
        print(f"- {picture}")
    return 0 if outcome.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
