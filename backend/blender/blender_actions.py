"""ARIA Lite - performing Blender actions.

Takes a list of actions, turns them into one script through
blender_script_templates, writes it to a temp file, and runs it with
`blender --background --python` through the shared CLI runner.

ONE SCRIPT PER RUN, NOT ONE PER ACTION
--------------------------------------
Blender takes several seconds to start. Twenty actions as twenty
invocations is twenty cold starts, and -- worse -- twenty separate
scenes: an object created by the first would not exist for the second,
because each run begins with an empty file. A run is therefore one
script, and objects created early are there for the steps that follow.

WHERE THE SAFETY IS
-------------------
Not here. It is in the templates: a caller supplies action names and
leaf values, and cannot supply statements. This module never sees
Python it did not generate, and never runs a script from anywhere but
its own temp file.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from backend.core import cli_runner
from backend.blender import blender_script_templates as templates
from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "BlenderUnavailable",
    "PLUGIN_ID",
    "answer_request",
    "output_dir",
    "add_bone",
    "add_cube",
    "add_cylinder",
    "add_plane",
    "add_sphere",
    "add_torus",
    "apply_array",
    "apply_bevel",
    "apply_boolean",
    "apply_decimate",
    "apply_mirror",
    "apply_multires",
    "apply_solidify",
    "apply_subdivision",
    "assign_material",
    "assign_vertex_group",
    "auto_weights",
    "bake_animation",
    "blender_path",
    "create_armature",
    "create_material",
    "enable_dyntopo",
    "export_fbx",
    "export_glb",
    "export_obj",
    "insert_keyframe",
    "mark_seams",
    "move",
    "normalize_weights",
    "parent",
    "parent_mesh_to_armature",
    "rotate",
    "run_actions",
    "scale",
    "sculpt_brush",
    "set_frame",
    "set_pose",
    "smart_uv_project",
    "unwrap",
]

PLUGIN_ID = "blender"
FIELD_PATH = "blender_path"
ENV_BLENDER = "ARIA_BLENDER_PATH"

# A modelling run is not a chat lookup. Subdivision, multires and a
# bake all take real time, and a limit that fits a version check would
# kill the work it was waiting for.
DEFAULT_TIMEOUT_SECONDS = 900


class BlenderUnavailable(RuntimeError):
    """Blender is not configured, or is not where it was said to be."""


def blender_path() -> Path:
    """Where Blender is. Environment first, then the plugin's setting.

    Same order and reasoning as the Unity CLI: an explicit setting
    beats a guess, and an environment variable beats both so a shell
    can override a machine for one run.
    """
    configured = str(os.environ.get(ENV_BLENDER) or "").strip()
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return candidate
        raise BlenderUnavailable(
            f"{ENV_BLENDER} points at {configured}, which is not a file.")

    from backend.plugins import plugin_settings

    plugin = plugin_settings.load_plugins().get(PLUGIN_ID)
    if plugin is None or plugin.get("dismissed", False):
        return _missing("The Blender plugin is not installed.")
    if not plugin.get("enabled", False):
        return _missing("The Blender plugin is installed but switched off. "
                        "Enable it on its page under Plugins first.")

    saved = str(plugin.get(FIELD_PATH) or "").strip()
    if not saved:
        return _missing("No Blender path is set. Add one on its page under "
                        "Plugins.")

    candidate = Path(saved)
    if not candidate.is_file():
        return _missing(f"Nothing runnable at {saved}.")
    return candidate


def _missing(message: str):
    raise BlenderUnavailable(message)


def _read_result(output: str) -> Optional[dict]:
    """The JSON the script printed, out of Blender's very chatty stdout.

    Blender writes add-on banners, Draco notices and export timings
    around whatever a script prints, so the result is fenced by
    sentinels rather than hoped to be the whole of stdout.
    """
    start = output.find(templates.RESULT_OPEN)
    end = output.find(templates.RESULT_CLOSE)
    if start == -1 or end <= start:
        return None
    body = output[start + len(templates.RESULT_OPEN):end].strip()
    try:
        return json.loads(body)
    except ValueError:
        logger.warning("the Blender script printed something that is not JSON")
        return None


def _same_file(left: str, right: str) -> bool:
    """Whether two paths name the same file.

    Compared after resolving and normalising case, because "car.blend"
    and "D:/Work/Car.blend" can be the same file on Windows and a
    guard that only compares strings would wave one of them through.
    """
    try:
        return (os.path.normcase(os.path.abspath(str(left)))
                == os.path.normcase(os.path.abspath(str(right))))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return False


def _would_destroy_the_opened_file(actions: Sequence[Dict[str, Any]],
                                   blend_file: Optional[str]) -> Optional[str]:
    """The one way this layer can lose somebody's work, or None.

    A recipe opens with `clear_scene`, which deletes every object in
    the scene. That is right for the ordinary case, because a run
    starts from an empty Blender and the only things it removes are
    the default cube, camera and light.

    It is NOT right when a caller opened an existing .blend AND the
    run saves back over it: Blender loads their file, the script
    empties it, and `save_file` writes the empty result on top. Their
    work is gone with nothing to undo.

    The guard is deliberately narrow. Opening a file, clearing it and
    exporting an FBX touches nothing on disk, and refusing that would
    be a false alarm -- the loss needs all three of open, clear, and
    save back to the same path.
    """
    if not blend_file or not Path(str(blend_file)).is_file():
        return None
    if not any(str((a or {}).get("action")) == "clear_scene" for a in actions):
        return None

    for entry in actions:
        if str((entry or {}).get("action")) != "save_file":
            continue
        target = ((entry or {}).get("params") or {}).get("path")
        if target and _same_file(target, blend_file):
            return (f"This would open {Path(str(blend_file)).name}, delete "
                    f"everything in it, and save the empty result back over "
                    f"it. Nothing ran.\n\n"
                    f"Save somewhere else, drop the clear_scene step, or pass "
                    f"allow_clearing_saved_file=True if that really is what "
                    f"you want.")
    return None


def run_actions(actions: Sequence[Dict[str, Any]], *,
                blend_file: Optional[str] = None,
                on_output: Optional[Callable[[str, str], None]] = None,
                timeout: Optional[int] = None,
                keep_script: bool = False,
                allow_clearing_saved_file: bool = False,
                allow_python: bool = False) -> dict:
    """Perform a list of actions and report what happened.

    `blend_file` opens an existing .blend first, so a second run can
    modify what the first made. Without it Blender starts from its
    default scene, which is the right default for "make me a thing".

    Opening a file and then clearing AND saving over it is refused --
    see _would_destroy_the_opened_file. `allow_clearing_saved_file`
    is the way to say you meant it.

    Never raises for anything Blender does. A missing object, a bad
    parameter and a crash are all answers, and each says which.
    """
    # run_python is the one action whose body is not a template. It runs
    # only when the caller says so, here; chat never does. See
    # blender_script_templates.run_python for the rest of the fence.
    if not allow_python and any(str((a or {}).get("action")) == "run_python" for a in actions):
        return {"success": False, "ran": False, "output": "", "result": None, "script": None,
                "error": ("run_python was refused: Python runs only when it is explicitly "
                          "allowed -- from the session command line with --allow-python, "
                          "never from chat. Nothing ran.")}

    if not allow_clearing_saved_file:
        destructive = _would_destroy_the_opened_file(actions, blend_file)
        if destructive:
            logger.warning("refused a run that would empty %s", blend_file)
            return {"success": False, "ran": False, "error": destructive,
                    "output": "", "result": None, "script": None}

    try:
        executable = blender_path()
    except BlenderUnavailable as error:
        return {"success": False, "ran": False, "error": str(error),
                "output": "", "result": None, "script": None}

    try:
        source = templates.build_script(actions)
    except (templates.UnknownAction, templates.BadValue) as error:
        return {"success": False, "ran": False, "error": str(error),
                "output": "", "result": None, "script": None}

    handle, script_path = tempfile.mkstemp(prefix="aria-blender-", suffix=".py")
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        file.write(source)

    # --factory-startup so a user's add-ons and startup file cannot
    # change what a script does. Two machines running the same actions
    # should get the same result, and somebody's auto-loading add-on is
    # exactly the kind of difference that is impossible to debug later.
    arguments: List[str] = ["--background", "--factory-startup"]
    if blend_file:
        arguments.append(str(blend_file))
    arguments += ["--python", script_path, "--python-exit-code", "1"]

    try:
        outcome = cli_runner.run(
            executable, arguments, on_output=on_output,
            timeout=int(timeout or DEFAULT_TIMEOUT_SECONDS),
            label="blender")
    finally:
        if not keep_script:
            Path(script_path).unlink(missing_ok=True)

    parsed = _read_result(outcome.get("output") or "")

    # Blender can exit 0 having printed a traceback, so the result
    # block is the better witness: no block means the script did not
    # reach its end.
    succeeded = bool(outcome.get("success")) and parsed is not None
    error = outcome.get("error")
    if outcome.get("success") and parsed is None:
        error = ("Blender finished but the script did not complete -- see the "
                 "output for the traceback.")

    return {
        "success": succeeded,
        "ran": True,
        "error": None if succeeded else error,
        "output": outcome.get("output") or "",
        "result": parsed,
        "script": script_path if keep_script else None,
        "actions": len(actions),
    }


# ======================================================
# One action at a time
#
# Each is a thin front for run_actions with a single step. They exist
# because a caller reading `add_cube(size=2)` learns more than one
# reading a dict literal, and because they are the surface the natural
# language mapper and the tests both name.
# ======================================================

def _one(action: str, **params) -> dict:
    return run_actions([{"action": action, "params": params}])


# --- modelling -----------------------------------------------------

def add_cube(size: float = 2.0, location=(0, 0, 0), name: str = "Cube") -> dict:
    return _one("add_cube", size=size, location=location, name=name)


def add_sphere(radius: float = 1.0, location=(0, 0, 0), name: str = "Sphere") -> dict:
    return _one("add_sphere", radius=radius, location=location, name=name)


def add_cylinder(radius: float = 1.0, depth: float = 2.0, location=(0, 0, 0),
                 name: str = "Cylinder") -> dict:
    return _one("add_cylinder", radius=radius, depth=depth, location=location,
                name=name)


def add_plane(size: float = 2.0, location=(0, 0, 0), name: str = "Plane") -> dict:
    return _one("add_plane", size=size, location=location, name=name)


def add_torus(major_radius: float = 1.0, minor_radius: float = 0.25,
              location=(0, 0, 0), name: str = "Torus") -> dict:
    return _one("add_torus", major_radius=major_radius,
                minor_radius=minor_radius, location=location, name=name)


# --- modifiers -----------------------------------------------------

def apply_subdivision(obj: str, levels: int = 2, apply: bool = False) -> dict:
    return _one("apply_subdivision", object=obj, levels=levels, apply=apply)


def apply_bevel(obj: str, amount: float = 0.02, segments: int = 2,
                apply: bool = False) -> dict:
    return _one("apply_bevel", object=obj, amount=amount, segments=segments,
                apply=apply)


def apply_mirror(obj: str, axis: str = "X", apply: bool = False) -> dict:
    return _one("apply_mirror", object=obj, axis=axis, apply=apply)


def apply_array(obj: str, count: int = 3, offset=(1, 0, 0),
                apply: bool = False) -> dict:
    return _one("apply_array", object=obj, count=count, offset=offset, apply=apply)


def apply_boolean(obj: str, target: str, operation: str = "DIFFERENCE",
                  apply: bool = False) -> dict:
    return _one("apply_boolean", object=obj, target=target,
                operation=operation, apply=apply)


def apply_solidify(obj: str, thickness: float = 0.05, apply: bool = False) -> dict:
    return _one("apply_solidify", object=obj, thickness=thickness, apply=apply)


def apply_decimate(obj: str, ratio: float = 0.5, apply: bool = False) -> dict:
    return _one("apply_decimate", object=obj, ratio=ratio, apply=apply)


# --- transforms ----------------------------------------------------

def move(obj: str, x: float = 0, y: float = 0, z: float = 0) -> dict:
    return _one("move", object=obj, x=x, y=y, z=z)


def rotate(obj: str, x: float = 0, y: float = 0, z: float = 0) -> dict:
    """Degrees. Blender stores radians; nobody says "rotate it by 1.57"."""
    return _one("rotate", object=obj, x=x, y=y, z=z)


def scale(obj: str, x: float = 1, y: float = 1, z: float = 1) -> dict:
    return _one("scale", object=obj, x=x, y=y, z=z)


def parent(child: str, parent_obj: str) -> dict:
    return _one("parent", child=child, parent=parent_obj)


# --- uv ------------------------------------------------------------

def smart_uv_project(obj: str, angle_limit: float = 1.15,
                     margin: float = 0.02) -> dict:
    return _one("smart_uv_project", object=obj, angle_limit=angle_limit,
                margin=margin)


def mark_seams(obj: str, sharpness: float = 0.9) -> dict:
    return _one("mark_seams", object=obj, sharpness=sharpness)


def unwrap(obj: str, margin: float = 0.02) -> dict:
    return _one("unwrap", object=obj, margin=margin)


# --- materials -----------------------------------------------------

def create_material(name: str, color=(0.8, 0.8, 0.8), metallic: float = 0.0,
                    roughness: float = 0.5) -> dict:
    return _one("create_material", name=name, color=color, metallic=metallic,
                roughness=roughness)


def assign_material(obj: str, material: str) -> dict:
    return _one("assign_material", object=obj, material=material)


# --- rigging -------------------------------------------------------

def create_armature(name: str = "Armature", location=(0, 0, 0)) -> dict:
    return _one("create_armature", name=name, location=location)


def add_bone(armature: str, name: str, head=(0, 0, 0), tail=(0, 0, 1)) -> dict:
    return _one("add_bone", armature=armature, name=name, head=head, tail=tail)


def parent_mesh_to_armature(mesh: str, armature: str) -> dict:
    return _one("parent_mesh_to_armature", mesh=mesh, armature=armature)


def auto_weights(mesh: str, armature: str) -> dict:
    return _one("auto_weights", mesh=mesh, armature=armature)


# --- skinning ------------------------------------------------------

def normalize_weights(mesh: str) -> dict:
    return _one("normalize_weights", mesh=mesh)


def assign_vertex_group(mesh: str, bone: str, vertices: Sequence[int],
                        weight: float = 1.0) -> dict:
    return _one("assign_vertex_group", mesh=mesh, bone=bone,
                vertices=list(vertices), weight=weight)


# --- animation -----------------------------------------------------

def insert_keyframe(obj: str, property: str = "location", frame: int = 1) -> dict:
    return _one("insert_keyframe", object=obj, property=property, frame=frame)


def set_pose(armature: str, bone: str, rotation=(0, 0, 0), frame: int = 1) -> dict:
    return _one("set_pose", armature=armature, bone=bone, rotation=rotation,
                frame=frame)


def set_frame(frame: int) -> dict:
    return _one("set_frame", frame=frame)


def bake_animation(obj: str, start: int = 1, end: int = 60) -> dict:
    return _one("bake_animation", object=obj, start=start, end=end)


# --- sculpting -----------------------------------------------------

def sculpt_brush(obj: str, brush_type: str = "DRAW", strength: float = 0.5,
                 detail_size: float = 12.0) -> dict:
    """Configures a sculpt session. It does not paint -- see the
    template's docstring for why that is impossible headless."""
    return _one("sculpt_brush", object=obj, brush_type=brush_type,
                strength=strength, detail_size=detail_size)


def enable_dyntopo(obj: str) -> dict:
    return _one("enable_dyntopo", object=obj)


def apply_multires(obj: str, levels: int = 2) -> dict:
    return _one("apply_multires", object=obj, levels=levels)


# --- export --------------------------------------------------------

def export_fbx(path: str, obj: Optional[str] = None) -> dict:
    return _one("export_fbx", path=path, object=obj)


def export_glb(path: str, obj: Optional[str] = None) -> dict:
    return _one("export_glb", path=path, object=obj)


def export_obj(path: str, obj: Optional[str] = None) -> dict:
    return _one("export_obj", path=path, object=obj)


# ======================================================
# Answering a request in someone's own words
#
# The step that was missing, and the reason typing "create a car for
# me in Blender" into chat reached phi-3-mini and got an offer to
# help. Everything below this line existed; nothing called it.
# ======================================================

ENV_OUTPUT = "ARIA_BLENDER_OUTPUT"
FIELD_OUTPUT = "output_dir"


def output_dir() -> Path:
    """Where finished assets are written. Created if missing.

    Resolved by plugin_settings, which every plugin that produces
    files shares. An earlier version answered this question here, and
    answered it with a folder beside the source tree -- wrong twice
    over: it is a repository, and it is not anywhere somebody would
    think to look for a model they asked for.

    Two copies of "where do the files go" drift, and the one nobody
    updated is the one somebody is using.
    """
    from backend.plugins import plugin_settings

    return plugin_settings.output_dir(PLUGIN_ID)


def _free_stem(folder: Path, base: str, suffixes: Sequence[str]) -> str:
    """A stem no existing file uses, for any of the suffixes.

    Checked across all of them at once so the .blend and the .fbx from
    one run keep the same name. Silently overwriting yesterday's Car
    would be the sort of loss nobody notices until they look for it.
    """
    cleaned = re.sub(r"[^\w.-]", "_", base).strip("_") or "Asset"
    candidate, index = cleaned, 1
    while any((folder / f"{candidate}{s}").exists() for s in suffixes):
        index += 1
        candidate = f"{cleaned}_{index}"
    return candidate


def _base_name(actions: Sequence[Dict[str, Any]]) -> str:
    """What to call the files, from what the plan creates.

    "Car_Body" becomes "Car", because the recipe named every part
    after the thing as a whole.
    """
    for entry in actions:
        if not str(entry.get("action", "")).startswith("add_"):
            continue
        name = str((entry.get("params") or {}).get("name") or "").strip()
        if name:
            return name.split("_")[0]
    return "Asset"


def _with_outputs(plan_actions: List[dict], folder: Path) -> tuple:
    """The plan plus somewhere to put the result.

    A plan on its own builds in memory and Blender then exits, which
    leaves nothing on disk -- the run succeeds and the person has
    nothing to show for it.

    Always a .blend, so the work can be opened and carried on. Plus a
    model file: whichever format the sentence asked for, or FBX, which
    is what Unity wants.
    """
    actions = [dict(step) for step in plan_actions]

    exports = [s for s in actions if str(s["action"]).startswith("export_")]
    suffix = {"export_fbx": ".fbx", "export_glb": ".glb",
              "export_obj": ".obj"}.get(
                  exports[0]["action"] if exports else "", ".fbx")

    stem = _free_stem(folder, _base_name(actions), [".blend", suffix])
    written = []

    if exports:
        for step in exports:
            extension = {"export_fbx": ".fbx", "export_glb": ".glb",
                         "export_obj": ".obj"}[step["action"]]
            params = dict(step.get("params") or {})
            # The mapper leaves "<export>.fbx" rather than inventing a
            # path -- ARIA is not allowed to hallucinate one.
            if not params.get("path") or str(params["path"]).startswith("<"):
                params["path"] = str(folder / f"{stem}{extension}")
            step["params"] = params
            written.append(params["path"])
    else:
        target = str(folder / f"{stem}{suffix}")
        actions.append({"action": "export_fbx", "params": {"path": target}})
        written.append(target)

    blend = str(folder / f"{stem}.blend")
    actions.append({"action": "save_file", "params": {"path": blend}})
    written.append(blend)

    return actions, written


def _elsewhere(said: str) -> str:
    """Where a request Blender cannot fill could actually be filled.

    Blender's vocabulary limit is REAL, not a gap to be papered over:
    it builds from cubes and cylinders, and no arrangement of
    primitives is an anime swordsman. Ludo has no such limit -- it
    turns a sentence into an image and an image into a mesh.

    Measured. Typed into chat:

        Create a 3D Model in Blender of a Anime Swordsman holding a
        katana

    The answer was the list of recipes and nothing else: honest, and
    unhelpful. The tool that CAN do it was one word away.

    Only offered when Ludo is actually installed, enabled and holding
    a key -- suggesting a route that would fail is worse than
    suggesting nothing. The cost is stated because taking the
    suggestion spends money.
    """
    try:
        from backend.ludo import ludo_client

        ludo_client.api_key()
    except Exception:
        return ""

    subject = re.sub(r"\b(?:in|with|using|from|inside|via)\s+blender\b", " ",
                     str(said or ""), flags=re.I)
    subject = re.sub(r"\s{2,}", " ", subject).strip(" ,.!?-")
    if not subject:
        return ""

    return ("\n\nLudo.ai can make this one -- it generates from a description "
            "rather than from primitives. Try:\n\n"
            f"    {subject} in Ludo\n\n"
            "That is a 3D model, so it is two generations and costs credits.")


def _tail(output: str, lines: int = 12) -> str:
    kept = (output or "").strip().splitlines()[-lines:]
    return "\n".join(kept)


def answer_request(text: str, *,
                   on_output: Optional[Callable[[str, str], None]] = None
                   ) -> Optional[dict]:
    """Answer a Blender request, or hand the turn back.

    Returns None when the sentence is not this layer's business -- not
    about Blender, or a question about it that a model should answer.

    Returns {"ran": bool, "text": str} when it is. Every branch that
    did not build something says so in its first sentence, because
    reporting work that never happened is the failure this exists to
    prevent.
    """
    from backend.blender import blender_nl_mapping as mapping
    from backend.blender import blender_typed_calls as typed

    said = str(text or "").strip()

    # Typed calls first, and before the names_blender gate: AddCube(...)
    # never says the word "Blender", and it should not have to. A message
    # in that syntax identifies itself, the same way the Unity bridge's
    # does. Returns None when the message is not calls at all, so a
    # sentence falls straight through to the mapper below.
    answered = typed.answer_typed(said, on_output=on_output)
    if answered is not None:
        return answered

    from backend.blender import blender_session

    other_tool = mapping.names_another_tool(said)
    gated = mapping.names_blender(said) and not other_tool
    mentioned = bool(re.search(r"\bblender\b", said, re.I)) and not other_tool
    # "in my Blender" -- the Blender open on their screen, through ARIA
    # Live, instead of chat's scene. Same steps; its own guards.
    live = mapping.names_live_blender(said) and not other_tool
    session = blender_session.LiveSession() if live else blender_session.Session()

    # "Send him to Unity" names no Blender, and needs none: "him" is the
    # character chat has been building. A question about it still is not
    # an instruction.
    if (blender_session.SEND_TO_UNITY.search(said) and not other_tool
            and not mapping._ASKING_ABOUT.match(said) and not mapping._WANTS_EXPLANATION.search(said)):
        sent = blender_session.answer_send(said, session)
        if sent is not None:
            return {"ran": True, **sent}

    # Undo, start over and "what is in the scene" are about the scene
    # chat has been building, not requests to build something. Looking
    # and describing change nothing, so "what's in the Blender scene?"
    # is enough for them; answer_command holds undo to the strict gate.
    if mentioned:
        command = blender_session.answer_command(said, session, allow_look=not gated, gated=gated)
        if command is not None:
            return {"ran": command.get("ran", True), "text": command.get("text", "")}

    if not gated:
        return None

    plan = mapping.map_text(said)
    if plan is None:
        # "Show me it in clay in Blender" -- a look, not a build. Only
        # once the mapper has passed: "build a car and show me" is a
        # build, and a build already ends with a picture.
        command = blender_session.answer_command(said, session)
        if command is not None:
            return {"ran": command.get("ran", True), "text": command.get("text", "")}
        if not mapping.wants_something_built(said):
            return None      # a question about Blender -- let a model answer

        # Asked for something, and I do not know how. A model handed
        # this would describe a spaceship and sound like it built one.
        return {"ran": False, "text": (
            "I did not build anything, because I do not know how to make "
            "that yet.\n\n"
            "I can build: " + ", ".join(mapping.recipe_names()) + ".\n"
            "I can also rig, animate a walk or idle, UV unwrap, set up "
            "sculpting, smooth, mirror and export."
            + _elsewhere(said))}

    if live:
        return _build_live(plan, session, on_output)

    folder = output_dir()
    actions, written = _with_outputs(plan["actions"], folder)
    # Run in chat's session: the scene stays open for the next message,
    # a version is kept to undo to, and the step ends with a picture.
    result = blender_session.Session().run(actions, on_output=on_output)

    if not result["ran"]:
        return {"ran": False, "text": (
            f"I did not run Blender, and nothing happened.\n\n{result['error']}")}

    if not result["success"]:
        return {"ran": True, "text": (
            f"I ran Blender and it failed: {result['error']}\n\n"
            f"{result.get('output_tail', '')}")}

    made = result.get("created") or []
    files = "\n".join(f"- {path}" for path in written)
    picture = blender_session.picture_markdown(result.get("renders") or [])
    return {"ran": True, "text": (
        f"Built {_base_name(actions)} in Blender: {len(made)} object"
        f"{'' if len(made) == 1 else 's'} "
        f"({', '.join(made[:6])}{' and more' if len(made) > 6 else ''}).\n\n"
        f"{files}\n\n"
        + (f"{picture}\n\n" if picture else "")
        + f"Steps: {plan['summary']}.")}


def _build_live(plan: dict, session, on_output) -> dict:
    """A recipe built in the Blender that is open, beside what is there.

    Recipes start by clearing the scene -- right for a scene of their
    own, and the one thing never done to somebody's open file. Nothing
    is saved or exported unless the sentence asked for an export.
    """
    from backend.blender import blender_session

    actions = [dict(step) for step in plan["actions"] if step["action"] != "clear_scene"]
    written = []
    folder = output_dir()
    stem = _free_stem(folder, _base_name(actions), [".fbx", ".glb", ".obj"])
    for step in actions:
        if str(step["action"]).startswith("export_"):
            params = dict(step.get("params") or {})
            if not params.get("path") or str(params["path"]).startswith("<"):
                params["path"] = str(folder / f"{stem}.{step['action'].split('_', 1)[1]}")
            step["params"] = params
            written.append(params["path"])

    result = session.run(actions, on_output=on_output)
    if not result.get("ran"):
        return {"ran": False, "text": (
            f"I did not build anything in your Blender.\n\n{result.get('error')}")}
    if not result.get("success"):
        return {"ran": True, "text": result.get("text", "")}

    made = result.get("created") or []
    picture = blender_session.picture_markdown(result.get("renders") or [])
    return {"ran": True, "text": (
        f"Built {_base_name(actions)} in your open Blender: {len(made)} object"
        f"{'' if len(made) == 1 else 's'} "
        f"({', '.join(made[:6])}{' and more' if len(made) > 6 else ''}), beside what was "
        f"already there. Nothing was saved -- that is yours to do."
        + ("\n\n" + "\n".join(f"- {path}" for path in written) if written else "")
        + (f"\n\n{picture}" if picture else "")
        + f"\n\n{session.undo_hint}\n\nSteps: {plan['summary']}.")}


# ======================================================
# The cleanup stage
#
# Everything above is one action. This is a STAGE: the fixed sequence
# that turns something a generator produced into something Unity can
# take, in ONE Blender launch.
#
# One launch matters. Blender starts cold every time -- --background
# --factory-startup, no add-ons, no startup file -- and that start is
# seconds. A stage built as eight calls would pay it eight times and
# would also lose the scene between them, since nothing persists
# across processes.
# ======================================================

# What a person is, in metres, when Unity says one unit is one metre.
DEFAULT_HEIGHT = 1.8

EXPORT_ACTIONS = {"fbx": "export_fbx", "glb": "export_glb", "obj": "export_obj"}


def _warnings_about(after: dict, before: dict) -> list:
    """What is still wrong after the cleanup, in words.

    This is the gate a pipeline reads. Every entry is something that
    will cost real time later -- a rig that fails, a texture that never
    arrives, a mesh Unity renders inside out -- and finding it here is
    the difference between a bad asset and a bad afternoon.
    """
    problems = []
    if not after.get("meshes"):
        problems.append("there is no mesh in the file")
    if after.get("broken_edges"):
        problems.append(
            f"{after['broken_edges']} edges have three or more faces, which "
            "will fail an automatic rig and confuse a mesh collider")
    if not after.get("uv_layers"):
        problems.append("no UV map, so nothing can be textured")
    if not after.get("materials"):
        problems.append("no material")
    if not after.get("images"):
        problems.append("no texture image -- the model will import grey")
    if after.get("ngons"):
        problems.append(f"{after['ngons']} faces have more than four sides")
    if before.get("triangles") and after.get("triangles"):
        if after["triangles"] > before["triangles"]:
            problems.append("the cleanup added geometry, which it should not")
    return problems


def clean_for_unity(path: str, *, out: Optional[str] = None,
                    height: float = DEFAULT_HEIGHT,
                    merge: bool = False,
                    merge_distance: float = 0.0001,
                    export: str = "fbx",
                    timeout: Optional[int] = None,
                    on_output: Optional[Callable[[str, str], None]] = None) -> dict:
    """Take a generated model and hand back one Unity can use.

    WHAT THIS FIXES, AND WHY IT IS THESE THINGS
    -------------------------------------------
    Measured on a real Ludo character rather than guessed at:

      * it arrived 0.994 units tall, so every model needs the same
        rescale -- Unity reads one unit as one metre and a person is
        not one metre;
      * its origin sat at the model's centre, feet at Z -0.496, so
        placing it on the ground buries it to the waist;
      * it had no loose geometry and no inverted faces, but both are
        cheap to check and expensive to meet later, so both are done.

    MERGING IS OFF BY DEFAULT AND THAT IS DELIBERATE
    ------------------------------------------------
    glTF stores attributes per vertex, so it splits vertices at every
    UV seam: the probe model had 18,442 vertices where 9,790 would do,
    and 13,542 edges that read as boundaries because of it. Merging
    them looks like an obvious win.

    It is not. On that same model the merge created 727 edges with
    three or more faces where there had been none, by welding surfaces
    that only touch. Unity does not care about the duplicates -- it
    splits vertices on import anyway -- so the default is to leave
    them, and `merge=True` is for when clean topology is actually
    needed, such as before decimating or rigging. Even then each mesh
    is checked and a merge that damages one is discarded.

    Returns the usual answer dict plus `before`, `after`, `warnings`
    and `output`.
    """
    source = str(path or "").strip()
    if not source:
        return {"success": False, "ran": False,
                "error": "I need a model file to clean.",
                "output": "", "result": None, "script": None}

    if not Path(source).exists():
        return {"success": False, "ran": False,
                "error": f"There is no file at {source}.",
                "output": "", "result": None, "script": None}

    kind = str(export or "fbx").strip().lower()
    if kind not in EXPORT_ACTIONS:
        return {"success": False, "ran": False,
                "error": f"I can export fbx, glb or obj, not {export!r}.",
                "output": "", "result": None, "script": None}

    destination = str(out or "").strip()
    if not destination:
        destination = str(Path(source).with_name(
            Path(source).stem + f"_clean.{kind}"))

    steps = [
        {"action": "clear_scene", "params": {}},
        {"action": "import_model", "params": {"path": source}},
        {"action": "measure_mesh", "params": {}},
        {"action": "remove_loose", "params": {}},
        {"action": "recalculate_normals", "params": {}},
    ]
    if merge:
        steps.append({"action": "merge_by_distance",
                      "params": {"distance": merge_distance}})
    steps += [
        {"action": "scale_to_height", "params": {"height": height}},
        {"action": "origin_to_floor", "params": {}},
        {"action": "measure_mesh", "params": {}},
        {"action": EXPORT_ACTIONS[kind], "params": {"path": destination}},
    ]

    answer = run_actions(steps, timeout=timeout, on_output=on_output)

    result = answer.get("result") or {}
    measurements = result.get("measurements") or []
    before = measurements[0] if measurements else {}
    after = measurements[-1] if len(measurements) > 1 else {}

    answer["before"] = before
    answer["after"] = after
    answer["output"] = destination if answer.get("success") else ""
    answer["warnings"] = _warnings_about(after, before) if answer.get("success") else []
    return answer
