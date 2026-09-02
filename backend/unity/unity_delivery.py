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
    Editor/Commands/GameObjects/GameObjectCommands.cs
        set_transform(target, position, rotation, scale)  -- LOCAL, and
        an omitted channel is left unchanged rather than zeroed
    Editor/Commands/GameObjects/ComponentCommands.cs
        add_component(target, type)                       -- both required

THE ORDER IS NOT THE OBVIOUS ONE
--------------------------------
"Import it, make a prefab, then put the prefab in the scene" is the
order everybody writes down first, and this CLI cannot do it.
create_prefab takes `source` as a SCENE object -- it saves something
already in a scene -- so the real order is import, instantiate the
imported model, then save THAT as a prefab:

    import_asset -> instantiate_prefab -> create_prefab -> set_transform

Runtime/Models/ObjectRef.cs is what lets the chain cross separate CLI
launches: it resolves globalId, path, guid, instanceId or
hierarchyPath, so each command hands back an identity the next one is
given and nothing has to be held between processes.

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
    "COLLIDERS",
    "HUMANOID_IMPORT",
    "add_component",
    "asset_path_for",
    "ensure_component",
    "has_component",
    "create_prefab",
    "deliver_to_unity",
    "model_to_prefab",
    "move_export_to_unity",
    "place_character",
    "place_in_scene",
    "prefab_path_for",
    "set_import_settings",
    "set_transform",
    "trigger_unity_import",
    "unity_project_root",
]

# Where a Blender export lands unless told otherwise. Its own folder,
# so a person can see at a glance what ARIA put there and delete it
# without taking anything of theirs with it.
# A backslash, named. Unity asset paths use forward slashes and a
# Windows path arriving with backslashes has to be converted.
SLASH = chr(92)

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

    # WITHOUT THIS THE CLI ANSWERS WITH A TABLE.
    # "Command<tab>Success<tab>Result<tab>Parameters" parses as no JSON
    # at all, so every globalId and hierarchyPath was dropped before
    # anything could read it: instantiate_prefab reported success and
    # handed back an empty instance, and set_transform then said it
    # needed an object to move. Found the first time a real Editor
    # answered -- the fakes had always returned parsed JSON, so no test
    # could have caught it.
    argv.append("--json")

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
                "data": _result_of(payload.get("data")),
                "output": outcome.get("output") or ""}

    if outcome.get("success"):
        return {"success": True, "ran": True, "error": None,
                "data": payload, "output": outcome.get("output") or ""}

    return {"success": False, "ran": bool(outcome.get("output")),
            "error": outcome.get("error") or "the command failed",
            "data": None, "output": outcome.get("output") or ""}


def _result_of(data: Any) -> Any:
    """The command's own result, out of the server's envelope.

    `unity cmd X --json` answers

        {"success":..., "data": {"command": "X", "parameters": {...},
                                 "result": {...}, "target": {...}}, ...}

    and the AuthoringResult -- the globalId, the hierarchyPath, the
    thing every following command needs as its target -- is that inner
    `result`. Reading `data` itself finds none of them.

    THIS WAS NOT CAUGHT BY THE TESTS, because the fakes were written
    from the same assumption as the code and put the identity at the
    level the code looked for it. It surfaced the first time a real
    Editor answered: instantiate_prefab reported success and handed
    back an empty instance, and set_transform then said it needed an
    object to move.
    """
    if isinstance(data, dict) and "result" in data and "command" in data:
        return data["result"]
    return data


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


# ======================================================
# Placement
#
# Everything above delivers a file. This puts it in a scene, standing
# on the floor, with the components it needs. It is the stage after
# Blender's clean_for_unity and the two are a pair: that one moves the
# origin to the model's feet so that this one can place at y=0 and have
# the character stand on the ground instead of half through it.
# ======================================================

# Colliders worth naming. Anything else can still be asked for through
# `components`, since add_component takes any type name -- these are
# the ones a character wants, spelled the way Unity spells them so a
# typo is caught here rather than inside the Editor.
COLLIDERS = {
    "capsule": "CapsuleCollider",
    "box": "BoxCollider",
    "sphere": "SphereCollider",
    "mesh": "MeshCollider",
}

# A character exported from Blender through FBX already faces the way
# Unity means by forward, so the default rotation is no rotation.
FACING_FORWARD = (0.0, 0.0, 0.0)

# Blender's cleanup normalises height, so nothing here rescales.
UNCHANGED_SCALE = (1.0, 1.0, 1.0)


def _vector_arg(values) -> str:
    """A float[] as this CLI wants to receive it.

    THE ONE THING HERE NOT READ FROM SOURCE. The C# says `float[]` and
    Runtime/Common/JsonSchemaGenerator.cs emits {"type": "array"} for
    it, so a JSON array is what the schema describes -- but the binder
    that turns argv into that JSON lives in the CLI executable, not in
    the package, and it cannot be asked without a running Editor.

    So it is one function, named once and used everywhere. If the
    spelling turns out to be `--position 0 0 0` or a repeated flag,
    this is the only line that changes.
    """
    return "[" + ",".join(f"{float(v):g}" for v in values) + "]"


def prefab_path_for(name: str, folder: str = DEFAULT_PREFAB_FOLDER) -> str:
    """Where a prefab of this name belongs, Assets/-relative."""
    clean = str(folder).replace(SLASH, "/").strip("/")
    if not clean.lower().startswith("assets"):
        clean = f"Assets/{clean}"
    return f"{clean}/{name}.prefab"


def _exists_in_project(asset_path: str) -> bool:
    """Whether an asset is already there, asked of the filesystem.

    The Editor could be asked instead, with find_assets. The filesystem
    is the better witness here: it answers with Unity closed, it costs
    no round trip, and an asset path under Assets/ IS a file path.
    Deciding whether to redo work should not itself need a running
    Editor.
    """
    root = unity_project_root()
    if root is None:
        return False
    relative = str(asset_path).replace(SLASH, "/").strip("/")
    return (root / relative).is_file()


def set_transform(target: str, *, position=None, rotation=None,
                  scale=None) -> dict:
    """Position, rotate and scale one object. LOCAL space.

    An omitted channel is left as it is rather than zeroed, which is
    the command's own behaviour and worth not fighting: "stand it on
    the floor" should not silently also undo a rotation somebody set.
    """
    if not str(target or "").strip():
        return _failure("I need an object to move.")

    arguments = {"target": target}
    if position is not None:
        arguments["position"] = _vector_arg(position)
    if rotation is not None:
        arguments["rotation"] = _vector_arg(rotation)
    if scale is not None:
        arguments["scale"] = _vector_arg(scale)

    if len(arguments) == 1:
        return _failure("I need a position, a rotation or a scale to set.")

    result = _run("set_transform", arguments)
    if not result["success"]:
        return result
    return {"success": True, "ran": True, "error": None,
            "target": target, "data": result["data"]}


def add_component(target: str, component: str) -> dict:
    """Add one component to one object, by Unity type name."""
    if not str(target or "").strip():
        return _failure("I need an object to add it to.")

    wanted = str(component or "").strip()
    if not wanted:
        return _failure("I need a component type to add.")
    wanted = COLLIDERS.get(wanted.lower(), wanted)

    result = _run("add_component", {"target": target, "type": wanted})
    if not result["success"]:
        return result
    return {"success": True, "ran": True, "error": None,
            "component": wanted, "data": result["data"]}


# C# for the one thing the command set cannot do. Kept whole and
# readable here rather than joined together at the call site.
#
# NO BACKSLASHES. Path.GetDirectoryName returns them on Windows and
# every layer between here and Roslyn wants to escape them differently
# -- the first version reached the compiler as Replace("\", "/") and
# failed with "Newline in constant". The folder is worked out in
# Python instead, where it is one line, and the C# only ever sees the
# forward-slashed Assets/ paths Unity uses anyway.
_MODEL_TO_PREFAB = """
var model = UnityEditor.AssetDatabase.LoadAssetAtPath<UnityEngine.GameObject>("{model}");
if (model == null) return "NO_MODEL";
if (!UnityEditor.AssetDatabase.IsValidFolder("{folder}"))
{{
    UnityEditor.AssetDatabase.CreateFolder("{parent}", "{leaf}");
}}
var instance = (UnityEngine.GameObject)UnityEditor.PrefabUtility.InstantiatePrefab(model);
if (instance == null) return "INSTANTIATE_FAILED";
bool ok;
var saved = UnityEditor.PrefabUtility.SaveAsPrefabAsset(instance, "{prefab}", out ok);
UnityEngine.Object.DestroyImmediate(instance);
UnityEditor.AssetDatabase.Refresh();
return ok && saved != null ? "{prefab}" : "SAVE_FAILED";
"""


def model_to_prefab(asset_path: str, prefab_path: str, *,
                    timeout_ms: int = 60000) -> dict:
    """Save an imported model as a real .prefab asset.

    WHY THIS NEEDS eval AND CANNOT USE THE PREFAB COMMANDS.
    instantiate_prefab refuses anything whose asset path does not end
    in ".prefab" -- PrefabCommands.ResolvePrefabAsset checks the
    extension -- so an imported FBX cannot be instantiated. And
    create_prefab saves a SCENE object, which is the thing we have no
    way to obtain. Between the two there is no route from a model
    asset to a scene at all.

    Unity itself is happy to do it: PrefabUtility.InstantiatePrefab
    accepts a model. The package's validator is stricter than the
    engine. So `eval`, which is one of the CLI's own commands, runs
    the four lines the command set is missing.

    Measured against a real Editor, which is the only way this was
    ever going to be found: the documented sequence fails on its
    second step with "is not a prefab asset".
    """
    source = str(asset_path or "").strip()
    target = str(prefab_path or "").strip()
    if not source or not target:
        return _failure("I need a model and a prefab path.")

    # Assets/-relative, forward slashes, worked out here so the C#
    # never has to touch a path separator.
    clean = target.replace(SLASH, "/")
    folder = clean.rsplit("/", 1)[0] if "/" in clean else "Assets"
    parent = folder.rsplit("/", 1)[0] if "/" in folder else "Assets"
    leaf = folder.rsplit("/", 1)[-1]

    code = _MODEL_TO_PREFAB.format(model=source.replace(SLASH, "/"),
                                   prefab=clean, folder=folder,
                                   parent=parent, leaf=leaf)
    result = _run("eval", {"code": code, "timeout": timeout_ms})
    if not result["success"]:
        return result

    data = result["data"] or {}
    answered = data.get("result") if isinstance(data, dict) else None

    if answered != target:
        return _failure(
            f"Unity could not make a prefab from {source}: "
            f"{answered or 'no answer'}", ran=True)

    return {"success": True, "ran": True, "error": None,
            "prefab": target, "data": data}


# What makes Unity build an Avatar from a rigged model.
#
# MEASURED, and it is not the default. A rigged FBX with a complete
# mixamorig: skeleton imports as animationType=Generic with ZERO
# avatars in the file -- so an Animator on it has nothing to drive and
# no humanoid animation will retarget onto it. Setting these two makes
# the same file import as Human with one avatar, isHuman and isValid
# both true.
HUMANOID_IMPORT = {"animationType": "Human",
                   "avatarSetup": "CreateFromThisModel"}


def set_import_settings(asset_path: str, settings: Dict[str, Any]) -> dict:
    """Change an importer's settings and re-import.

    `settings` goes over as a JSON object, which is what the command
    takes -- not a flat list of flags.
    """
    if not str(asset_path or "").strip():
        return _failure("I need an asset to configure.")
    if not settings:
        return _failure("I need at least one setting to apply.")

    result = _run("set_import_settings", {
        "asset": asset_path,
        "settings": json.dumps(settings, separators=(",", ":")),
    })
    if not result["success"]:
        return result

    data = result["data"] or {}
    applied = data.get("applied") or [] if isinstance(data, dict) else []
    unknown = data.get("unknown") or [] if isinstance(data, dict) else []
    return {"success": True, "ran": True, "error": None,
            "applied": applied, "unknown": unknown, "data": data}


def has_component(target: str, component: str) -> bool:
    """Whether the object already carries this component."""
    if not str(target or "").strip() or not str(component or "").strip():
        return False
    wanted = COLLIDERS.get(str(component).strip().lower(), str(component).strip())
    return bool(_run("get_component_properties",
                     {"target": target, "type": wanted})["success"])


def ensure_component(target: str, component: str) -> dict:
    """Add a component, unless the object already has one.

    MEASURED, AND IT CHANGES WHAT SUCCESS MEANS. A model imported as
    Human arrives with an Animator already on its root, carrying the
    Avatar Unity built. Asking for another gets

        Failed to add component 'Animator' to 'X'
        (it may be disallowed on this GameObject)

    -- because Unity allows one Animator per GameObject. Reporting that
    as a failure says the character cannot be animated at exactly the
    moment it can, which is the wrong answer twice over.

    So a refused add is checked rather than believed: if the component
    is there, that is a success with `added` false.
    """
    outcome = add_component(target, component)
    if outcome["success"]:
        outcome["added"] = True
        return outcome

    wanted = COLLIDERS.get(str(component).strip().lower(), str(component).strip())
    if has_component(target, wanted):
        return {"success": True, "ran": True, "error": None,
                "component": wanted, "added": False, "data": None}

    outcome["added"] = False
    return outcome


def place_character(fbx_path: str, *, name: Optional[str] = None,
                    folder: str = DEFAULT_FOLDER,
                    prefab_folder: str = DEFAULT_PREFAB_FOLDER,
                    scene_path: Optional[str] = None,
                    position=None, rotation=None, scale=None,
                    collider: Optional[str] = None,
                    rigidbody: bool = False,
                    components: Optional[Sequence[str]] = None,
                    import_settings: Optional[Dict[str, Any]] = None,
                    reuse: bool = True,
                    overwrite: bool = False) -> dict:
    """Take a cleaned FBX and stand it up in a Unity scene.

    THE ORDER, AND WHY IT IS NOT THE OBVIOUS ONE
    --------------------------------------------
        import_asset -> instantiate_prefab -> create_prefab -> set_transform

    create_prefab saves a SCENE object, so a prefab cannot be made
    before something has been placed. Reading that off PrefabCommands.cs
    is the difference between this working and a plausible-looking
    sequence that fails on its second command.

    WHY y=0 IS ENOUGH TO STAND ON THE FLOOR
    ---------------------------------------
    Only because Blender's clean_for_unity moved the origin to the
    model's feet first. Straight from Ludo the origin sits at the
    model's centre, and placing THAT at y=0 buries it to the waist. The
    two stages are a pair and this is the seam between them.

    RUNNING IT TWICE
    ----------------
    With `reuse` (the default) an existing prefab is instantiated
    rather than the model being imported and prefabbed again. So a
    second call adds a second instance and touches no asset, which is
    what "place another one" should do. `overwrite` re-imports over the
    existing asset and is the only thing here that sends confirm=true.

    EVERY COMMAND NEEDS A RUNNING EDITOR
    ------------------------------------
    `unity cmd` talks to a live Editor over the Pipeline server. With
    nothing listening the CLI answers "No Pipeline instance found for
    project ...", which is carried through as the error rather than
    being reworded into something vaguer.
    """
    if not str(fbx_path or "").strip():
        return _failure("I need a model file to place.")

    source = Path(str(fbx_path).strip())
    if not source.is_file():
        return _failure(f"There is no file at {source}.")

    root = unity_project_root()
    if root is None:
        return _failure(
            "No Unity project is configured -- set one on the Unity CLI "
            "plugin page before placing anything.")

    label = str(name or source.stem).strip() or source.stem
    asset_path = asset_path_for(source, folder)
    prefab_path = prefab_path_for(label, prefab_folder)

    steps: List[str] = []
    warnings: List[str] = []
    reused = False

    # --- 1. The asset ------------------------------------------------
    have_prefab = reuse and not overwrite and _exists_in_project(prefab_path)
    if have_prefab:
        reused = True
        steps.append("reused_prefab")
    else:
        already = _exists_in_project(asset_path)
        if already and not overwrite:
            steps.append("reused_asset")
        else:
            imported = _run("import_asset", {
                "source": str(source.resolve()),
                "path": asset_path,
                # True only when replacing something. The command wants
                # it for an overwrite and refuses without it, which is a
                # guard worth keeping rather than defeating by always
                # sending true.
                "confirm": True if already else None,
            })
            if not imported["success"]:
                return _failure(
                    f"Unity could not import {source.name}: {imported['error']}",
                    ran=imported.get("ran", True), asset_path=asset_path,
                    steps=steps)
            steps.append("import_asset")

    # --- 1b. Tell the importer what it is -----------------------------
    # Before the prefab is made, never after: the prefab captures the
    # model as it is imported, so a prefab built from a Generic import
    # keeps an Animator with no Avatar even once the asset is fixed.
    if import_settings and not have_prefab:
        configured = set_import_settings(asset_path, import_settings)
        if not configured["success"]:
            return _failure(
                f"Unity imported {label} but could not apply the import "
                f"settings: {configured['error']}",
                ran=True, asset_path=asset_path, steps=steps)
        if configured.get("unknown"):
            warnings.append(
                f"the importer did not recognise: "
                f"{', '.join(configured['unknown'])}")
        steps.append("set_import_settings")

    # --- 2. The model becomes a prefab --------------------------------
    # Not "instantiate then save", which is what the command set looks
    # like it supports and is not: instantiate_prefab will not touch an
    # FBX. See model_to_prefab.
    if not have_prefab:
        made = model_to_prefab(asset_path, prefab_path)
        if not made["success"]:
            return _failure(made["error"], ran=True, asset_path=asset_path,
                            prefab_path=prefab_path, steps=steps)
        steps.append("create_prefab")

    # --- 3. Into the scene --------------------------------------------
    placed = place_in_scene(prefab_path, name=label, scene_path=scene_path)
    if not placed["success"]:
        return _failure(
            f"Unity could not place {label} in the scene: {placed['error']}",
            ran=placed.get("ran", True), asset_path=asset_path,
            prefab_path=prefab_path, steps=steps)
    steps.append("instantiate_prefab")

    instance = placed.get("instance") or placed.get("hierarchy_path") or label

    # --- 4. Stand it on the floor ------------------------------------
    where = tuple(position) if position is not None else (0.0, 0.0, 0.0)
    facing = tuple(rotation) if rotation is not None else FACING_FORWARD
    size = tuple(scale) if scale is not None else UNCHANGED_SCALE

    moved = set_transform(instance, position=where, rotation=facing, scale=size)
    if not moved["success"]:
        return _failure(
            f"Unity placed {label} but could not set its transform: "
            f"{moved['error']}",
            ran=True, asset_path=asset_path, prefab_path=prefab_path,
            instance=instance, steps=steps)
    steps.append("set_transform")

    # --- 5. Components -------------------------------------------------
    wanted: List[str] = []
    if collider:
        wanted.append(str(collider))
    if rigidbody:
        wanted.append("Rigidbody")
    wanted += [str(c) for c in (components or []) if str(c).strip()]

    added: List[str] = []
    for component in wanted:
        outcome = ensure_component(instance, component)
        if not outcome["success"]:
            # Not fatal. The character is in the scene and standing up;
            # a missing collider is a warning, not a reason to report
            # the whole placement as a failure.
            warnings.append(f"could not add {component}: {outcome['error']}")
            continue
        added.append(outcome["component"])
        steps.append(("add_component:" if outcome.get("added", True)
                      else "already_had:") + outcome["component"])

    if not collider:
        warnings.append(
            "no collider, so nothing will physically touch this character")
    if "Animator" not in added:
        warnings.append(
            "no Animator -- rigging and animation are a separate step, and a "
            "model straight from Ludo has no armature to drive one")

    return {
        "success": True, "ran": True, "error": None,
        "scene_path": scene_path or "the active scene",
        "asset_path": asset_path,
        "prefab_path": prefab_path,
        "instance": instance,
        "name": label,
        "position": where, "rotation": facing, "scale": size,
        "components": added,
        "reused": reused,
        "steps": steps,
        "warnings": warnings,
    }
