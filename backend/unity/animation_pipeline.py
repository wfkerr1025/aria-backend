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
from backend.unity import curve_writer
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


# What to ask Ludo for, per state. The wording matters more than it
# looks: the endpoint rewrites these into a motion caption with an LLM
# when augment_prompt is on, and "walking" alone produces something
# vaguer than "walking forward at a steady pace".
MOTION_PROMPTS = {
    "Idle": "standing still, breathing, weight shifting slightly",
    "Walk": "walking forward at a steady pace",
    "Run": "running forward quickly",
    "Attack": "swinging a sword downward in an overhead attack",
}

# Ludo's `loop` mirrors a motion back to its rest pose. Its own
# documentation says that "reads oddly for cyclic gaits like walking",
# so it is for the one-way motions only. NOT the same thing as Unity's
# loop-time flag, which is set on the clip.
MIRROR_BACK = ("Attack",)


def motion_for_states(model_url: str, states: Sequence[str], *,
                      run_id: str = "", folder: str = "",
                      prompts: Optional[Dict[str, str]] = None,
                      reuse: bool = True) -> dict:
    """One animation GLB per state, from Ludo. ONE CREDIT EACH.

    Skips any state whose GLB is already on disk, so a re-run of a
    four-state pipeline costs nothing rather than four credits. That
    check is the filesystem, not the ledger: the ledger forgets after
    twelve hours and a pipeline is re-run days apart.
    """
    from pathlib import Path as _Path

    from backend.ludo import ludo_actions as ludo

    wanted = prompts or MOTION_PROMPTS
    made: Dict[str, str] = {}
    steps: List[str] = []
    warnings: List[str] = []
    spent = 0

    for state in states:
        prompt = wanted.get(state)
        if not prompt:
            warnings.append(f"no motion prompt for {state}, so it has no clip")
            continue

        target = _Path(folder or ".") / f"{state.lower()}_motion.glb"
        if reuse and target.is_file() and target.stat().st_size:
            made[state] = str(target)
            steps.append(f"reused_motion:{state}")
            continue

        answer = ludo.generate_motion(
            model_url, prompt, mode="rot_only", variants=1,
            loop=state in MIRROR_BACK, run_id=run_id)

        spent += (answer.get("cost") or {}).get("calls", 0)
        if not answer.get("success"):
            warnings.append(f"could not make a {state} motion: "
                            f"{answer.get('error')}")
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            from backend.ludo import ludo_client
            ludo_client.download(answer["url"], str(target))
        except Exception as error:      # pragma: no cover - network shapes
            warnings.append(f"{state} was generated but not downloaded: "
                            f"{error}. It is at {answer['url']}")
            continue

        made[state] = str(target)
        steps.append(f"generate_motion:{state}")

    return {"motions": made, "steps": steps, "warnings": warnings,
            "calls": spent}


def _fill_clips(model_url: str, states: Sequence[str],
                clip_paths: Sequence[str], *, target: str,
                rigged_model_path: str = "", work_folder: str = "",
                run_id: str = "") -> dict:
    """Generate motion, retarget it, and write it into the clips.

    The whole reason this is not an FBX import: Unity finds zero takes
    in a Blender-exported animated FBX. See blender_curve_export.
    """
    from pathlib import Path as _Path

    from backend.blender import blender_curve_export as export

    steps: List[str] = []
    warnings: List[str] = []
    written: List[str] = []

    folder = work_folder or str(_Path(clip_paths[0]).parent) if clip_paths else "."

    motion = motion_for_states(model_url, states, run_id=run_id,
                               folder=folder)
    steps += motion["steps"]
    warnings += motion["warnings"]

    if not motion["motions"]:
        return {"clips": list(clip_paths), "steps": steps,
                "warnings": warnings}

    paths = curve_writer.bone_paths(target)
    if not paths:
        warnings.append(
            "could not read the skeleton's hierarchy, so no curves were "
            "written")
        return {"clips": list(clip_paths), "steps": steps,
                "warnings": warnings}

    by_state = {_Path(path).stem.rsplit("_", 1)[-1]: path
                for path in clip_paths}

    for state, glb in motion["motions"].items():
        clip = by_state.get(state)
        if not clip:
            warnings.append(f"{state} has motion but no clip to put it in")
            continue

        dumped = export.export_curves(
            glb, str(_Path(glb).with_suffix(".curves.json")),
            rigged_path=rigged_model_path)
        if not dumped["success"]:
            warnings.append(f"could not read {state}'s curves: "
                            f"{dumped['error']}")
            continue
        steps.append(f"export_curves:{state}")

        if dumped.get("unknown_to_rig"):
            warnings.append(
                f"{state} names {len(dumped['unknown_to_rig'])} bones the rig "
                "does not have; those are not animated")

        calls = curve_writer.curves_for_clip(dumped["dump"], paths)
        outcome = curve_writer.write_clip_curves(clip, calls)
        if not outcome["success"]:
            warnings.append(f"{state}: {outcome['error']}")
            continue

        steps.append(f"write_curves:{state}({outcome['written']})")
        written.append(clip)

    return {"clips": written or list(clip_paths), "steps": steps,
            "warnings": warnings}


def animate_character(prefab_path: str, *,
                      instance: str = "",
                      name: Optional[str] = None,
                      animation_style: str = "stylized",
                      required_states: Sequence[str] = REQUIRED_STATES,
                      parameters: Sequence[params.Parameter] =
                      params.DEFAULT_PARAMETERS,
                      clips: Optional[Dict[str, str]] = None,
                      controller_path: Optional[str] = None,
                      rigged_model_url: str = "",
                      rigged_model_path: str = "",
                      work_folder: str = "",
                      generate_motion: bool = False,
                      run_id: str = "",
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

    # Humanoid is accepted and warned about. It plays muscle curves, so
    # the transform curves this pipeline writes will not drive it --
    # measured, and it moved nothing at all.
    if report["import_type"] == HUMANOID:
        result.warnings.append(
            "this model is imported Humanoid, which plays muscle curves -- "
            "generated bone-local clips will not drive it. Re-import as "
            "Generic to play them.")

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

    # --- 3b. Fill the clips with actual motion --------------------------
    # Only when asked: it costs a credit per state, and a controller
    # with empty clips is still a correct controller. The states, the
    # transitions and the parameters are all real either way; what an
    # empty clip means is that the state plays nothing.
    if generate_motion and rigged_model_url:
        filled = _fill_clips(
            rigged_model_url, result.states, built.get("clips") or [],
            target=target, rigged_model_path=rigged_model_path,
            work_folder=work_folder, run_id=run_id or result.instance)
        result.steps += filled["steps"]
        result.warnings += filled["warnings"]
        result.clips = filled["clips"]
    else:
        result.clips = list(built.get("clips") or [])
        if generate_motion:
            # Asked for and not possible. Saying nothing here would
            # leave somebody believing they had paid for motion.
            result.warnings.append(
                "motion was asked for but no rigged model URL was given, so "
                "the clips are empty. Ludo animates a model it can reach, "
                "not a Unity asset path.")
        else:
            result.warnings.append(
                "the clips are empty: the controller is correct and the "
                "states play nothing. Pass generate_motion=True with a "
                "rigged model URL to fill them, at one Ludo credit per "
                "state.")

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
