"""ARIA Lite - giving a rigged character something to play.

WHERE THIS SITS
---------------
    ludo_rigging        a skeleton
    blender_rigging     scale, origin, and the rig checked
    unity_rigging       imported Humanoid, prefab, Animator, placed
    THIS                an AnimatorController, and the Animator told
                        about it

The rigging pipeline deliberately stopped at an Animator with no
controller. This is the thing that was being left for.

THE AVATAR IS CHECKED FIRST, AND IT IS NOT A FORMALITY
------------------------------------------------------
Measured during the rigging work: a rigged FBX with a complete
mixamorig: skeleton imports as animationType=Generic with ZERO
avatars unless the importer is told otherwise. Everything downstream
still succeeds -- the controller builds, the states appear, the
inspector looks right -- and nothing moves, because the Animator has
no avatar to drive.

So the avatar is read before a controller is attached, and a character
that is not a valid humanoid is refused rather than decorated.

TWO COMMANDS THE SPECIFICATION ASKED FOR DO NOT EXIST
-----------------------------------------------------
`assign_animator_controller` and `validate_humanoid_avatar` are not in
the package. The controller is assigned by writing the Animator's
m_Controller serialized property -- read off a live Animator, where
m_Controller was null and m_Avatar held a globalId -- and the avatar is
read with `eval`, because isHuman and isValid are properties of the
Avatar object rather than serialized fields anything else exposes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from logger import get_logger

from backend.unity import animation_parameters as params
from backend.unity import animation_state_graph as graphs
from backend.unity import animator_controller_builder as builder
from backend.unity import unity_delivery as delivery

logger = get_logger(__name__)

__all__ = [
    "AnimationResult",
    "REQUIRED_STATES",
    "animate_character",
    "humanoid_avatar",
]

REQUIRED_STATES = ("Idle", "Walk", "Run", "Attack")

# The Animator's serialized property that holds the controller. Read
# off a live Animator rather than guessed: get_component_properties
# answered m_Controller = null and m_Avatar = {globalId: ...}.
CONTROLLER_PROPERTY = "m_Controller"

# isHuman and isValid live on the Avatar object, not in any serialized
# field, so they are read with eval. Written out here so what runs in
# the Editor can be read here.
_AVATAR_REPORT = """
var go = {finder};
if (go == null) return "NO_OBJECT";
var anim = go.GetComponent<UnityEngine.Animator>();
if (anim == null) return "NO_ANIMATOR";
var av = anim.avatar;
if (av == null) return "NO_AVATAR";
return "isHuman=" + av.isHuman + " isValid=" + av.isValid
     + " controller=" + (anim.runtimeAnimatorController == null
                         ? "none" : anim.runtimeAnimatorController.name);
"""


@dataclass
class AnimationResult:
    """What the pipeline did, in the shape the specification asked for."""

    prefab_path: str = ""
    controller_path: str = ""
    states: List[str] = field(default_factory=list)
    transitions: List[str] = field(default_factory=list)
    parameters: List[str] = field(default_factory=list)
    avatar_valid: bool = False
    reused: bool = False
    steps: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    success: bool = False
    ran: bool = False
    error: Optional[str] = None
    instance: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def _finder(target: str) -> str:
    """C# that resolves the object, by name or by hierarchy path.

    GameObject.Find takes a path like "/Hero" as well as a bare name,
    so one expression covers both -- and a globalId is turned into a
    name by the caller, since Find cannot take one.
    """
    name = str(target or "").strip()
    quoted = name.replace('"', '')
    return f'UnityEngine.GameObject.Find("{quoted}")'


def humanoid_avatar(target: str) -> dict:
    """What the object's Animator and Avatar actually are.

    Returns success, is_human, is_valid, controller and a reason. A
    missing object, a missing Animator and a missing Avatar are three
    different problems and are reported as three different answers.
    """
    if not str(target or "").strip():
        return {"success": False, "is_human": False, "is_valid": False,
                "controller": "", "reason": "I need an object to check."}

    outcome = delivery._run("eval", {
        "code": _AVATAR_REPORT.format(finder=_finder(target)),
        "timeout": 30000,
    })
    if not outcome["success"]:
        return {"success": False, "is_human": False, "is_valid": False,
                "controller": "", "reason": outcome["error"] or "eval failed"}

    data = outcome["data"] or {}
    said = str((data or {}).get("result") or "")

    if said in ("NO_OBJECT", "NO_ANIMATOR", "NO_AVATAR") or not said:
        reasons = {
            "NO_OBJECT": f"there is no object called {target!r} in the scene",
            "NO_ANIMATOR": f"{target} has no Animator component",
            "NO_AVATAR": (f"{target} has an Animator but no Avatar -- the "
                          "model was imported Generic rather than Humanoid"),
        }
        return {"success": False, "is_human": False, "is_valid": False,
                "controller": "",
                "reason": reasons.get(said, "the Editor said nothing useful")}

    is_human = "isHuman=True" in said
    is_valid = "isValid=True" in said
    controller = said.split("controller=")[-1].strip() if "controller=" in said else ""

    return {"success": True, "is_human": is_human, "is_valid": is_valid,
            "controller": "" if controller == "none" else controller,
            "reason": said}


def attach_controller(target: str, controller_path: str) -> dict:
    """Point the object's Animator at a controller.

    There is no assign_animator_controller command. The Animator's
    m_Controller property takes an object reference, and
    set_component_properties assigns one from a handle-shaped value --
    hence {"path": ...} rather than the bare string.
    """
    if not str(target or "").strip():
        return delivery._failure("I need an object to attach it to.")
    if not str(controller_path or "").strip():
        return delivery._failure("I need a controller to attach.")

    import json as _json

    outcome = delivery._run("set_component_properties", {
        "target": target,
        "type": "Animator",
        "properties": _json.dumps(
            {CONTROLLER_PROPERTY: {"path": controller_path}},
            separators=(",", ":")),
    })
    if not outcome["success"]:
        return outcome
    return {"success": True, "ran": True, "error": None,
            "controller": controller_path, "data": outcome["data"]}


def animate_character(prefab_path: str, *,
                      instance: str = "",
                      name: Optional[str] = None,
                      animation_style: str = "stylized",
                      required_states: Sequence[str] = REQUIRED_STATES,
                      parameters: Sequence[params.Parameter] =
                      params.DEFAULT_PARAMETERS,
                      clips: Optional[Dict[str, str]] = None,
                      controller_path: Optional[str] = None,
                      complete_graph: bool = True,
                      reuse: bool = True,
                      overwrite: bool = False) -> AnimationResult:
    """Give a rigged character a controller it can actually play.

    `instance` is the scene object to attach to -- what the rigging
    pipeline returned. Its name is used when a globalId cannot be
    resolved by GameObject.Find, which is why `name` exists too.

    THE AVATAR GATE. Nothing is attached to a character whose Avatar
    is not a valid humanoid. A controller on a Generic import builds
    perfectly and moves nothing, and that failure is invisible until
    play mode.

    `animation_style` is recorded and does not yet change the graph.
    It is the seam where generated or authored clip sets will be
    chosen, and it is carried through rather than dropped so a caller
    can already say what it wanted.
    """
    label = str(name or Path(str(prefab_path or "")).stem or "character")
    result = AnimationResult(prefab_path=str(prefab_path or ""),
                             instance=str(instance or label))

    if not str(prefab_path or "").strip():
        result.error = "I need a rigged prefab to animate."
        return result

    target = str(instance or "").strip() or label

    # --- 1. Is it actually animatable? --------------------------------
    avatar = humanoid_avatar(target)
    result.ran = True
    result.avatar_valid = bool(avatar["is_human"] and avatar["is_valid"])

    if not avatar["success"]:
        result.error = f"Could not check the avatar: {avatar['reason']}"
        return result

    if not result.avatar_valid:
        result.error = (
            f"{target} is not a valid humanoid -- isHuman="
            f"{avatar['is_human']}, isValid={avatar['is_valid']}. A "
            "controller would build and nothing would move, so it was not "
            "attached. Re-import the model with animationType=Human.")
        return result

    result.steps.append("validate_humanoid_avatar")

    # --- 2. The graph, decided before Unity is asked anything ---------
    graph = graphs.default_graph(required_states, clips=clips,
                                 complete=complete_graph)
    problems = (params.validate_parameters(parameters)
                + graphs.validate_graph(graph, parameters))
    if problems:
        result.error = "; ".join(problems)
        return result

    # Legal-but-probably-unintended shapes are reported rather than
    # refused: a caller who asked for the four specified transitions
    # and nothing else gets them, and gets told what they do.
    result.warnings.extend(graphs.graph_advice(graph))

    # --- 3. Build it ---------------------------------------------------
    where = controller_path or builder.controller_path_for(label)
    built = builder.build_controller(
        graph, parameters, where, character=label,
        reuse=reuse, overwrite=overwrite)

    result.controller_path = where
    result.steps += built.get("steps", [])
    result.warnings += built.get("warnings", [])

    if not built["success"]:
        result.error = built["error"]
        return result

    result.states = built.get("states") or [s.name for s in graph.states]
    result.transitions = built.get("transitions") or []
    result.parameters = built.get("parameters") or [p.name for p in parameters]
    result.reused = bool(built.get("reused"))

    # --- 4. Tell the Animator about it ---------------------------------
    attached = attach_controller(target, where)
    if not attached["success"]:
        result.error = (f"The controller was built but could not be attached "
                        f"to {target}: {attached['error']}")
        return result

    result.steps.append("attach_controller")

    if avatar.get("controller"):
        result.warnings.append(
            f"{target} already had the controller {avatar['controller']!r} "
            "attached, which has been replaced")

    result.success = True
    return result
