"""ARIA Lite - getting a Ludo.ai asset into Unity.

Ludo hands back a URL. Unity wants a file under Assets/, imported,
made into a prefab and placed in a scene. This is the four steps
between.

The Unity half is not Ludo's, and is not written here: it lives in
backend.unity.unity_delivery and is the same code the Blender layer
uses. The Pipeline argument names in it were read out of the package's
own C# rather than guessed, and that evidence is worth exactly one
copy.

What IS Ludo's, and is here: a URL has to be fetched before any of
that can happen, and a fetch can fail in ways a local export cannot.
An expired link, a truncated body, an error page served with a 200 --
each of those produces a file that exists, has a plausible name, and
is not an asset. So every download is validated by its bytes before
Unity is asked to look at it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from backend.ludo import ludo_actions
from backend.unity import unity_delivery
from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "create_prefab",
    "deliver_to_unity",
    "download_and_deliver",
    "move_export_to_unity",
    "place_in_scene",
    "trigger_unity_import",
]

# Ludo assets get their own folder under the shared ARIA one, so a
# person can see at a glance which tool made what and delete either
# without taking the other with it.
DEFAULT_FOLDER = "Assets/ARIA/Ludo"
DEFAULT_PREFAB_FOLDER = "Assets/ARIA/Ludo/Prefabs"

# The four Unity steps, unchanged. Named here so a reader of the Ludo
# layer finds them where they expect, and so a caller never has to
# know which tool's module owns the implementation.
move_export_to_unity = unity_delivery.move_export_to_unity
trigger_unity_import = unity_delivery.trigger_unity_import
place_in_scene = unity_delivery.place_in_scene
create_prefab = unity_delivery.create_prefab


def deliver_to_unity(path: str, *, folder: str = DEFAULT_FOLDER,
                     place: bool = True, prefab: bool = False,
                     prefab_folder: str = DEFAULT_PREFAB_FOLDER,
                     name: Optional[str] = None,
                     scene_path: Optional[str] = None) -> dict:
    """Import an already-downloaded asset, optionally place and prefab it."""
    return unity_delivery.deliver_to_unity(
        path, folder=folder, place=place, prefab=prefab,
        prefab_folder=prefab_folder, name=name, scene_path=scene_path)


def download_and_deliver(generated: Dict[str, Any], *,
                         name: str = "asset",
                         folder: str = DEFAULT_FOLDER,
                         place: bool = True, prefab: bool = False,
                         to_unity: bool = True) -> dict:
    """A Ludo result, all the way to a scene.

    Takes what ludo_actions returned rather than a bare URL, so a
    generation that failed cannot be mistaken for one that produced
    nothing downloadable -- those are different sentences and a person
    needs the right one.

    Every step is reported separately. "It generated and downloaded but
    Unity was closed" is true and useful; one success flag over four
    steps is not.
    """
    steps: Dict[str, Any] = {}

    if not isinstance(generated, dict):
        return {"success": False, "steps": steps,
                "error": "I was not given a Ludo result to deliver.",
                "summary": "Nothing to do."}

    if not generated.get("success"):
        # Pass the generation's own reason through rather than
        # inventing a download failure on top of it.
        return {"success": False, "steps": {"generate": generated},
                "error": generated.get("error"),
                "summary": (f"Nothing was made, so there is nothing to "
                            f"deliver: {generated.get('error')}")}

    url = generated.get("url")
    if not url:
        return {"success": False, "steps": {"generate": generated},
                "error": "Ludo made something but did not say where it is.",
                "summary": "Nothing to download."}

    downloaded = ludo_actions.download_asset(
        url, kind=str(generated.get("kind") or ""), name=name)
    steps["download"] = downloaded
    if not downloaded["success"]:
        return {"success": False, "steps": steps,
                "error": downloaded["error"],
                "summary": f"Ludo made it, but I could not fetch it: "
                           f"{downloaded['error']}"}

    checked = ludo_actions.validate_asset(downloaded["path"])
    steps["validate"] = checked
    if not checked["valid"]:
        # Kept, not deleted: a person may want to look at what arrived,
        # and deleting somebody's file to tidy up an error message is
        # not this function's decision to make.
        return {"success": False, "steps": steps, "error": checked["error"],
                "path": downloaded["path"],
                "summary": (f"Downloaded to {downloaded['path']}, but it is "
                            f"not a usable asset: {checked['error']}")}

    if not to_unity:
        return {"success": True, "steps": steps, "error": None,
                "path": downloaded["path"],
                "summary": f"Saved {downloaded['path']}."}

    delivered = deliver_to_unity(downloaded["path"], folder=folder,
                                 place=place, prefab=prefab, name=name)
    steps["unity"] = delivered

    return {"success": bool(delivered.get("success")), "steps": steps,
            "error": delivered.get("error"),
            "path": downloaded["path"],
            "asset_path": delivered.get("asset_path"),
            "summary": (f"Saved {Path(downloaded['path']).name} and "
                        f"{str(delivered.get('summary') or '').lower()}"
                        if delivered.get("success")
                        else f"Saved {downloaded['path']}, but Unity: "
                             f"{delivered.get('error')}")}
