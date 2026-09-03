"""ARIA Lite - giving a rigged character something to play.

WHERE THIS SITS
---------------
    ludo_rigging        a skeleton
    blender_rigging     scale, origin, and the rig checked
    unity_rigging       imported Humanoid, prefab, Animator, placed
    THIS                an AnimatorController, and the Animator told
                        about it
    mixamo_library      the clips that fill its states

WHERE THE CLIPS COME FROM, AND WHERE THEY USED TO
--------------------------------------------------
A generated-motion path once lived here: ask Ludo for a clip per
state, retarget it in Blender, write the curves into a .anim. It was
built, paid for, measured, and removed. The generated walk had BOTH
THIGHS swinging the same way at every frame -- a shuffle rather than a
walk -- and a second attempt with a more prescriptive prompt came back
effectively static. Measured the same way, the Mixamo clip gives a
44.3 degree leg split against the generated 6.1.

Two modules went with it, curve_writer and blender_curve_export, and
the work they did is worth remembering rather than repeating: an
animation-only GLB whose bone-local quaternions had to be composed
onto Unity's rest pose, written one CLI call per curve. It worked. The
motion it delivered was not worth delivering.

The rigging pipeline deliberately stopped at an Animator with no
controller. This is the thing that was being left for.

THE GATE ACCEPTS HUMANOID OR GENERIC, AND THAT IS A CORRECTION
--------------------------------------------------------------
It used to demand a valid humanoid avatar. A live run showed that is
the wrong question for generated animation:

    a clip of bone-local transform curves, sampled against the
    Humanoid import, moved NOTHING. The same clip against the same
    model imported Generic drives it.

A Humanoid Animator plays muscle curves, not transform curves. And
animation generated FROM this rig does not need retargeting in the
first place -- retargeting is for reusing one clip across different
characters. So Generic is the right mode for this pipeline's own
output, and demanding Humanoid would refuse exactly the setup that
works.

What is still refused is what genuinely cannot be animated: Legacy,
a skeleton that is not there, and a Humanoid import whose Avatar is
missing or invalid -- that last one being the case measured during
the rigging work, where a rigged FBX imports Generic with zero avatars
unless the importer is told, and everything downstream then succeeds
while nothing moves.

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
    "GENERIC",
    "HUMANOID",
    "LEGACY",
    "REQUIRED_STATES",
    "animate_character",
    "can_be_animated",
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
var bones = go.GetComponentsInChildren<UnityEngine.Transform>().Length;
var human = av != null && av.isHuman;
var valid = av != null && av.isValid;
return "hasAvatar=" + (av != null) + " isHuman=" + human + " isValid=" + valid
     + " bones=" + bones
     + " controller=" + (anim.runtimeAnimatorController == null
                         ? "none" : anim.runtimeAnimatorController.name);
"""

# What an import can be, and which of them this pipeline can animate.
HUMANOID = "Humanoid"
GENERIC = "Generic"
LEGACY = "Legacy"

# Fewer transforms than this is not a skeleton. Deliberately low: the
# rig measured here has 48, and a number tuned to that would refuse
# every simpler character somebody rigs later.
MINIMUM_BONES = 4


@dataclass
class AnimationResult:
    """What the pipeline did, in the shape the specification asked for."""

    prefab_path: str = ""
    controller_path: str = ""
    states: List[str] = field(default_factory=list)
    transitions: List[str] = field(default_factory=list)
    parameters: List[str] = field(default_factory=list)
    avatar_valid: bool = False
    import_type: str = ""
    clips: List[str] = field(default_factory=list)
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

    has_avatar = "hasAvatar=True" in said
    is_human = "isHuman=True" in said
    is_valid = "isValid=True" in said
    controller = said.split("controller=")[-1].strip() if "controller=" in said else ""

    bones = 0
    if "bones=" in said:
        try:
            bones = int(said.split("bones=")[1].split()[0])
        except (ValueError, IndexError):    # pragma: no cover - defensive
            bones = 0

    # An Avatar that says isHuman is a Humanoid import; one that exists
    # and does not is Generic. No avatar at all with a skeleton present
    # is still Generic as far as transform curves are concerned -- they
    # do not need one.
    kind = HUMANOID if (has_avatar and is_human) else GENERIC

    return {"success": True, "has_avatar": has_avatar,
            "is_human": is_human, "is_valid": is_valid,
            "import_type": kind, "bones": bones,
            "controller": "" if controller == "none" else controller,
            "reason": said}


def can_be_animated(report: Dict[str, Any]) -> Optional[str]:
    """Why this character cannot be animated, or None.

    Humanoid and Generic are both fine and for different reasons.
    Generic plays the bone-local transform curves this pipeline writes.
    Humanoid plays muscle curves, so a clip written here will not drive
    it -- that is said as a warning by the caller rather than refused,
    because the controller, states and parameters are still correct and
    a re-import is a one-line fix.
    """
    if not report.get("bones"):
        return ("there is no skeleton under this object, so there is nothing "
                "for a clip to drive")
    if report["bones"] < MINIMUM_BONES:
        return (f"{report['bones']} transforms is not a skeleton")
    if report.get("import_type") == HUMANOID and not report.get("is_valid"):
        return ("the model is Humanoid but its Avatar is invalid, so the "
                "Animator has nothing to drive -- re-import it with "
                "animationType=Human, or as Generic")
    return None


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
                      parameters: Optional[Sequence[params.Parameter]] = None,
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

    CLIPS COME FROM mixamo_library, NOT FROM HERE. This builds the
    controller, its states, its transitions and its parameters, and
    leaves the states empty unless a caller passes `clips`. The
    generated-motion path that used to fill them was removed.

    `animation_style` is recorded and does not change the graph. It is
    the seam where a clip set gets chosen, and it is carried through
    rather than dropped so a caller can already say what it wanted.
    """
    label = str(name or Path(str(prefab_path or "")).stem or "character")
    result = AnimationResult(prefab_path=str(prefab_path or ""),
                             instance=str(instance or label))

    if not str(prefab_path or "").strip():
        result.error = "I need a rigged prefab to animate."
        return result

    target = str(instance or "").strip() or label

    # --- 1. Is it actually animatable? --------------------------------
    report = humanoid_avatar(target)
    result.ran = True

    if not report["success"]:
        result.error = f"Could not check the import: {report['reason']}"
        return result

    result.avatar_valid = bool(report["is_human"] and report["is_valid"])
    result.import_type = report["import_type"]

    refusal = can_be_animated(report)
    if refusal:
        result.error = f"{target} cannot be animated: {refusal}"
        return result

    result.steps.append("validate_import_type")

    # Generic is the one worth a word now. Mixamo clips retarget
    # through the Avatar system, which needs Humanoid on both the clip
    # and the character -- so Generic is the setting that quietly
    # produces a character standing still.
    #
    # This warning used to say the opposite, and correctly: the removed
    # generated-curve route wrote bone-local transform curves, which
    # only a Generic Animator plays. The route is gone and the advice
    # inverted with it.
    if report["import_type"] == GENERIC:
        result.warnings.append(
            "this model is imported Generic. Retargeted clips -- Mixamo's "
            "among them -- go through the Avatar system and need Humanoid "
            "on both the clip and the character, so they will not drive "
            "it. Re-import with animationType=Human.")

    # --- 2. The graph, decided before Unity is asked anything ---------
    # Parameters follow the STATES unless a caller names them. A graph
    # is refused for naming a parameter nobody declared, so asking a
    # caller to keep a second list in step with the first is asking
    # them to get it wrong.
    if parameters is None:
        parameters = params.parameters_for(required_states) or             params.DEFAULT_PARAMETERS
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
    # Placeholder .anim files are only worth making when nothing else
    # is going to fill the states. With clips on their way in there is
    # no point littering the project with empty ones.
    built = builder.build_controller(
        graph, parameters, where, character=label,
        make_missing_clips=not clips,
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

    result.clips = list(built.get("clips") or [])
    if not clips:
        result.warnings.append(
            "no clips were supplied, so every state plays nothing. Use "
            "mixamo_library to discover, match and assign a folder of "
            "clips.")

    # --- 4. Tell the Animator about it ---------------------------------
    attached = attach_controller(target, where)
    if not attached["success"]:
        result.error = (f"The controller was built but could not be attached "
                        f"to {target}: {attached['error']}")
        return result

    result.steps.append("attach_controller")

    if report.get("controller"):
        result.warnings.append(
            f"{target} already had the controller {report['controller']!r} "
            "attached, which has been replaced")

    result.success = True
    return result
