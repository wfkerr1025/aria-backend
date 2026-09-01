"""ARIA Lite - putting a generated asset into Unity.

Shared by every tool that makes one. Blender exports an FBX, Ludo.ai
downloads a GLB, and from there the steps are identical: get it under
Assets/, import it, place it, save a prefab.

WHERE THE ARGUMENT NAMES CAME FROM
----------------------------------
Not from memory. Four earlier guesses about this CLI were wrong --
`--project` does not exist, `--mode` is test-only, `list` returns
`data.tools`, and a pager would hang -- so the parameter names below
were read out of the Pipeline package's own source in the project:

    Editor/Commands/Assets/AssetCommands.cs
        import_asset(source, path, confirm, dry_run)
    Editor/Commands/Prefabs/PrefabCommands.cs
        create_prefab(source, path)
        instantiate_prefab(prefab, scene_path, name)

and the `--snake_case value` syntax from the package's own documented
examples (`unity command run_tests --mode editor --filter ...`).

This module exists because that evidence should be written down once.
Two copies of a measured argument name drift, and the one nobody
updated is the one somebody is using.

WHAT NEEDS THE EDITOR OPEN
--------------------------
Everything except the copy. `unity cmd` talks to a running Editor over
its Pipeline server, and with nothing listening it answers "No Unity
Editor instances found". So `move_export_to_unity` works with Unity
closed -- it is a file copy -- and the other three do not, and say so
rather than pretending.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "asset_path_for",
    "create_prefab",
    "deliver_to_unity",
    "move_export_to_unity",
    "place_in_scene",
    "trigger_unity_import",
    "unity_project_root",
]

# Where a Blender export lands unless told otherwise. Its own folder,
# so a person can see at a glance what ARIA put there and delete it
# without taking anything of theirs with it.
DEFAULT_FOLDER = "Assets/ARIA"

# Prefabs go beside them rather than in Assets/Prefabs, for the same
# reason.
DEFAULT_PREFAB_FOLDER = "Assets/ARIA/Prefabs"

# Formats Unity imports as a model. Anything else is copied but not
# claimed to be a model.
MODEL_SUFFIXES = frozenset({".fbx", ".glb", ".gltf", ".obj", ".blend", ".dae"})


def _failure(error: str, **extra) -> dict:
    result = {"success": False, "ran": False, "error": error}
    result.update(extra)
    return result


def unity_project_root() -> Optional[Path]:
    """The Unity project ARIA is pointed at, or None.

    Read from the Unity CLI plugin's own setting. Never guessed, and
    never derived from a path somebody mentioned in chat -- ARIA is not
    allowed to invent a project location.
    """
    from backend.unity import unity_cli_engine

    configured = unity_cli_engine._plugin_setting(unity_cli_engine.FIELD_PROJECT)
    if not configured:
        return None
    root = Path(str(configured))
    return root if (root / "Assets").is_dir() else None


def asset_path_for(export: Path, folder: str = DEFAULT_FOLDER) -> str:
    """The Unity asset path a given export should live at.

    Forward slashes, Assets/-relative. Unity asset paths are not
    filesystem paths and a backslash in one is a bug that surfaces much
    later, as a missing texture.
    """
    clean = str(folder).replace("\\", "/").strip("/")
    if not clean.lower().startswith("assets"):
        clean = f"Assets/{clean}"
    return f"{clean}/{Path(export).name}"


def move_export_to_unity(export: str, folder: str = DEFAULT_FOLDER,
                         *, overwrite: bool = True) -> dict:
    """Copy a Blender export into the Unity project.

    A plain filesystem copy, so it works with Unity closed. Unity
    imports whatever appeared under Assets/ the next time it has focus;
    `trigger_unity_import` is how you stop waiting for that.
    """
    source = Path(str(export))
    if not source.is_file():
        return _failure(f"There is no file at {source}.")

    root = unity_project_root()
    if root is None:
        return _failure("No Unity project is set. Choose one on the Unity CLI "
                        "page under Plugins first.")

    relative = asset_path_for(source, folder)
    destination = root / relative
    if destination.exists() and not overwrite:
        return _failure(f"{relative} already exists.", asset_path=relative)

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    except OSError as error:
        return _failure(f"Could not copy into the project: {error}")

    logger.info("copied %s into %s", source.name, relative)
    return {
        "success": True, "ran": True, "error": None,
        "asset_path": relative,
        "absolute": str(destination),
        "imported": False,
        "note": ("Copied into the project. Unity imports it the next time the "
                 "Editor has focus."),
    }


def _run(command: str, arguments: Dict[str, Any], *, timeout: Optional[int] = None) -> dict:
    """Run one `unity cmd <name> --k v` and hand back its JSON.

    Values are passed as separate argv entries, never interpolated into
    a string, so a path with a space in it stays one argument.
    """
    from backend.unity import unity_cli_engine

    argv = []
    for key, value in arguments.items():
        if value is None:
            continue
        argv.append(f"--{key}")
        argv.append("true" if value is True else
                    "false" if value is False else str(value))

    outcome = unity_cli_engine.run_invocation(f"cmd {command}", argv)
    payload = outcome.get("json")

    # The CLI answers with {"success": ..., "data": ..., "errors": [...]}
    # and exits 0 for a command that failed inside the Editor, so the
    # envelope is the thing to believe, not the exit code.
    if isinstance(payload, dict) and "success" in payload:
        if not payload.get("success"):
            errors = payload.get("errors") or []
            message = "; ".join(str(e.get("message") or e) for e in errors) \
                if errors else "the command failed"
            return {"success": False, "ran": True, "error": message,
                    "data": None, "output": outcome.get("output") or ""}
        return {"success": True, "ran": True, "error": None,
                "data": payload.get("data"), "output": outcome.get("output") or ""}

    if outcome.get("success"):
        return {"success": True, "ran": True, "error": None,
                "data": payload, "output": outcome.get("output") or ""}

    return {"success": False, "ran": bool(outcome.get("output")),
            "error": outcome.get("error") or "the command failed",
            "data": None, "output": outcome.get("output") or ""}


def _object_ref(data: Any) -> Optional[str]:
    """A reference the next command can use to name this object.

    AuthoringResult carries several identities; globalId is the one
    that survives a scene reload, so it is preferred, with the
    hierarchy path as a readable fallback.
    """
    if not isinstance(data, dict):
        return None
    for key in ("globalId", "GlobalId", "hierarchyPath", "HierarchyPath",
                "assetPath", "AssetPath", "guid", "Guid"):
        value = data.get(key)
        if value:
            return str(value)
    return None


def trigger_unity_import(export: str, folder: str = DEFAULT_FOLDER,
                         *, overwrite: bool = True) -> dict:
    """Copy AND import in one step, through the running Editor.

    Unity's own `import_asset` does the copy itself, so this does not
    copy first -- File.Copy onto itself throws, and an earlier draft
    that copied and then imported would have failed on every asset.

    With the Editor closed this falls back to the plain copy, and says
    which of the two happened.
    """
    source = Path(str(export))
    if not source.is_file():
        return _failure(f"There is no file at {source}.")

    root = unity_project_root()
    if root is None:
        return _failure("No Unity project is set. Choose one on the Unity CLI "
                        "page under Plugins first.")

    relative = asset_path_for(source, folder)
    result = _run("import_asset", {
        "source": str(source.resolve()),
        "path": relative,
        "confirm": True if overwrite else None,
    })

    if result["success"]:
        # The ASSET PATH, not _object_ref. AuthoringResult carries
        # several identities and _object_ref prefers globalId, which
        # is right for a scene object and wrong here: it turned
        # "Assets/ARIA/car.fbx" into "abc", and everything downstream
        # then asked Unity to instantiate a prefab called abc.
        returned = result["data"] if isinstance(result["data"], dict) else {}
        found = returned.get("assetPath") or returned.get("AssetPath")
        return {"success": True, "ran": True, "error": None,
                "asset_path": str(found) if found else relative,
                "absolute": str(root / relative),
                "imported": True,
                "note": "Imported through the running Editor."}

    # The Editor not being up is the ordinary case, not an error worth
    # failing on -- the file can still be put where Unity will find it.
    fallback = move_export_to_unity(str(source), folder, overwrite=overwrite)
    if fallback["success"]:
        fallback["note"] = (
            "Unity was not reachable, so the file was copied into the project "
            "instead. It will import when the Editor next has focus.")
        fallback["unity_error"] = result["error"]
    return fallback


def place_in_scene(asset_path: str, *, name: Optional[str] = None,
                   scene_path: Optional[str] = None) -> dict:
    """Instantiate an imported asset into a loaded scene.

    An imported FBX is a model prefab as far as Unity is concerned, so
    `instantiate_prefab` is the right command for it as well as for a
    real .prefab.
    """
    result = _run("instantiate_prefab", {
        "prefab": asset_path,
        "name": name,
        "scene_path": scene_path,
    })
    if not result["success"]:
        return result

    data = result["data"]
    return {"success": True, "ran": True, "error": None,
            "instance": _object_ref(data),
            "hierarchy_path": (data or {}).get("hierarchyPath")
            if isinstance(data, dict) else None,
            "data": data}


def create_prefab(source: str, prefab_path: str) -> dict:
    """Save a GameObject that is in a scene as a prefab asset.

    `source` is a scene object, not an asset -- that is what the
    Pipeline command takes. To make a prefab out of a freshly imported
    model you place it first and pass what `place_in_scene` returns,
    which is what `deliver_to_unity` does.
    """
    result = _run("create_prefab", {"source": source, "path": prefab_path})
    if not result["success"]:
        return result

    return {"success": True, "ran": True, "error": None,
            "prefab": _object_ref(result["data"]) or prefab_path,
            "data": result["data"]}


def deliver_to_unity(export: str, *, folder: str = DEFAULT_FOLDER,
                     place: bool = True, prefab: bool = False,
                     prefab_folder: str = DEFAULT_PREFAB_FOLDER,
                     name: Optional[str] = None,
                     scene_path: Optional[str] = None) -> dict:
    """Import, optionally place, optionally save as a prefab.

    Every step is reported separately, and a later step failing does
    not erase the fact that an earlier one worked. "The import
    succeeded and the placement did not" is a true and useful sentence;
    a single success flag over four steps is not.
    """
    steps: Dict[str, Any] = {}

    imported = trigger_unity_import(str(export), folder)
    steps["import"] = imported
    if not imported["success"]:
        return {"success": False, "steps": steps,
                "error": imported.get("error"),
                "summary": "Nothing reached the project."}

    asset_path = imported["asset_path"]
    if Path(str(export)).suffix.lower() not in MODEL_SUFFIXES:
        place = prefab = False

    if not imported.get("imported") and (place or prefab):
        # A copy is on disk but Unity has not looked at it, so there is
        # no asset to instantiate yet. Saying so beats a confusing
        # "prefab not found" from the next command.
        return {"success": True, "steps": steps, "error": None,
                "asset_path": asset_path,
                "summary": (f"Copied to {asset_path}. Open the Unity Editor to "
                            f"import it, then it can be placed.")}

    instance = None
    if place or prefab:
        placed = place_in_scene(asset_path, name=name, scene_path=scene_path)
        steps["place"] = placed
        if not placed["success"]:
            return {"success": True, "steps": steps, "error": placed["error"],
                    "asset_path": asset_path,
                    "summary": (f"Imported {asset_path}, but it could not be "
                                f"placed: {placed['error']}")}
        instance = placed["instance"]

    if prefab and instance:
        target = f"{str(prefab_folder).strip('/')}/{Path(str(export)).stem}.prefab"
        saved = create_prefab(instance, target)
        steps["prefab"] = saved
        if not saved["success"]:
            return {"success": True, "steps": steps, "error": saved["error"],
                    "asset_path": asset_path,
                    "summary": (f"Imported and placed {asset_path}, but the "
                                f"prefab was not saved: {saved['error']}")}
        return {"success": True, "steps": steps, "error": None,
                "asset_path": asset_path, "prefab": saved["prefab"],
                "summary": (f"Imported {asset_path}, placed it in the scene, "
                            f"and saved {saved['prefab']}.")}

    if instance:
        return {"success": True, "steps": steps, "error": None,
                "asset_path": asset_path, "instance": instance,
                "summary": f"Imported {asset_path} and placed it in the scene."}

    return {"success": True, "steps": steps, "error": None,
            "asset_path": asset_path,
            "summary": f"Imported {asset_path}."}
