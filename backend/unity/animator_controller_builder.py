"""ARIA Lite - turning a graph into an AnimatorController in Unity.

WHERE THESE COMMAND NAMES CAME FROM
-----------------------------------
The package's own C#, not the specification, which named six commands
of which one exists:

    create_animator_controller(path, confirm, dry_run)     exists
    add_animator_state(controller, layer, name, motion,
                       isDefault, position)                exists
    add_animator_parameter(controller, name, type,
                           defaultValue, dry_run)          exists
    add_animator_transition(controller, layer, fromState,
                            toState, conditions, hasExitTime,
                            exitTime, duration, ...)       exists
    create_animation_clip(path, frameRate, loop, confirm)   exists

    add_state / add_transition / add_parameter              DO NOT
    assign_animator_controller                              DOES NOT
    validate_humanoid_avatar                                DOES NOT

The last two are done another way -- see animation_pipeline.

THE ORDER IS NOT ARBITRARY
--------------------------
Parameters before transitions, because a condition names a parameter
and the command validates that it exists. States before transitions,
for the same reason. Clips before states, because a state's motion is
an asset that has to be there to be referenced. Getting this wrong
produces a controller that is built and empty, with four separate
parameter-validation failures and no indication of which command
caused them.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from logger import get_logger

from backend.unity import animation_parameters as params
from backend.unity import animation_state_graph as graphs
from backend.unity import unity_delivery as delivery

logger = get_logger(__name__)

__all__ = [
    "DEFAULT_CLIP_FOLDER",
    "DIRECT_MOTION_SUFFIXES",
    "is_direct_motion",
    "build_controller",
    "clip_path_for",
    "controller_path_for",
]

DEFAULT_CONTROLLER_FOLDER = "Assets/ARIA/Animators"
DEFAULT_CLIP_FOLDER = "Assets/ARIA/Animations"


def controller_path_for(name: str,
                        folder: str = DEFAULT_CONTROLLER_FOLDER) -> str:
    clean = str(folder).replace(delivery.SLASH, "/").strip("/")
    if not clean.lower().startswith("assets"):
        clean = f"Assets/{clean}"
    return f"{clean}/{name}.controller"


def clip_path_for(state: str, character: str,
                  folder: str = DEFAULT_CLIP_FOLDER) -> str:
    clean = str(folder).replace(delivery.SLASH, "/").strip("/")
    if not clean.lower().startswith("assets"):
        clean = f"Assets/{clean}"
    return f"{clean}/{character}_{state}.anim"


# What add_animator_state will accept as a `motion`.
#
# MEASURED. Handing it an FBX gets
#
#   Motion reference '...X Bot@Standing Idle.fbx' resolved to a
#   GameObject, not an AnimationClip or BlendTree
#
# because an ObjectRef resolves a model path to the model, and the
# clip is a SUB-asset with no path syntax of its own. A .anim is a
# clip in its own right and resolves fine. Anything else is left for
# mixamo_library.assign_clips, which finds the sub-asset the way the
# Editor does.
DIRECT_MOTION_SUFFIXES = (".anim",)


def is_direct_motion(path: str) -> bool:
    return str(path or "").lower().endswith(DIRECT_MOTION_SUFFIXES)


def _exists(asset_path: str) -> bool:
    return delivery._exists_in_project(asset_path)


def build_controller(graph: graphs.Graph,
                     parameters: Sequence[params.Parameter],
                     controller_path: str, *,
                     character: str = "character",
                     clip_folder: str = DEFAULT_CLIP_FOLDER,
                     make_missing_clips: bool = True,
                     reuse: bool = True,
                     overwrite: bool = False) -> dict:
    """Realise a graph as an AnimatorController asset.

    EVERY STATE GETS A CLIP, and by default this makes the ones that
    are missing. A state with no motion is legal in Unity and plays
    nothing, so a controller full of them looks correct in the
    inspector and does nothing in play mode -- which is the failure
    this pipeline exists to avoid, not to produce. The generated clips
    are empty .anim assets: real, referenced, loop-flagged for the
    locomotion states, and waiting for somebody to put curves in them.

    Nothing here spends money. Clips come from Unity, not from Ludo,
    so the ceiling that matters is the graph's own size rather than a
    credit count.

    Returns success, ran, error, controller, clips, states,
    transitions, parameters, reused, steps, warnings.
    """
    answer = {"success": False, "ran": False, "error": None,
              "controller": controller_path, "clips": [], "states": [],
              "transitions": [], "parameters": [], "reused": False,
              "steps": [], "warnings": []}

    problems = (params.validate_parameters(parameters)
                + graphs.validate_graph(graph, parameters))
    if problems:
        answer["error"] = "; ".join(problems)
        return answer

    steps: List[str] = answer["steps"]
    warnings: List[str] = answer["warnings"]
    warnings.extend(graphs.graph_advice(graph))

    # --- the controller ------------------------------------------------
    if reuse and not overwrite and _exists(controller_path):
        logger.info("animator controller already at %s", controller_path)
        answer.update({"success": True, "ran": False, "reused": True,
                       "steps": ["reused_controller"],
                       "states": [s.name for s in graph.states],
                       "transitions": [f"{t.source}->{t.target}"
                                       for t in graph.transitions],
                       "parameters": [p.name for p in parameters]})
        return answer

    made = delivery._run("create_animator_controller", {
        "path": controller_path,
        "confirm": True if _exists(controller_path) else None,
    })
    if not made["success"]:
        answer["error"] = (f"Unity could not create the controller at "
                           f"{controller_path}: {made['error']}")
        answer["ran"] = True
        return answer
    steps.append("create_animator_controller")
    answer["ran"] = True

    # --- the clips ------------------------------------------------------
    # Before the states, because a state's motion has to exist to be
    # referenced.
    clips: Dict[str, str] = {}
    deferred: List[str] = []
    for state in graph.states:
        if state.clip:
            if is_direct_motion(state.clip):
                clips[state.name] = state.clip
            else:
                # A model file. The state is created without a motion
                # and something else assigns the sub-asset; passing it
                # here fails the whole build on the first state.
                deferred.append(state.name)
            continue
        if not make_missing_clips:
            warnings.append(
                f"{state.name} has no clip, so that state will play nothing")
            continue


        path = clip_path_for(state.name, character, clip_folder)
        if _exists(path):
            clips[state.name] = path
            continue

        # Locomotion loops; a one-shot like an attack does not.
        looping = state.name.lower() not in ("attack", "hit", "die", "death")
        clip = delivery._run("create_animation_clip",
                             {"path": path, "loop": looping})
        if not clip["success"]:
            warnings.append(f"could not make a clip for {state.name}: "
                            f"{clip['error']}")
            continue
        clips[state.name] = path
        steps.append(f"create_animation_clip:{state.name}")

    answer["clips"] = sorted(clips.values())
    if deferred:
        warnings.append(
            f"{len(deferred)} states were given a model file rather than a "
            ".anim, so their motion is not set here: "
            f"{', '.join(sorted(deferred))}. Use "
            "mixamo_library.assign_clips to attach the clips inside them.")

    # --- the parameters -------------------------------------------------
    # Before the transitions: a condition names a parameter and the
    # command checks that it exists.
    for parameter in parameters:
        body = dict(parameter.payload())
        body["controller"] = controller_path
        outcome = delivery._run("add_animator_parameter", body)
        if not outcome["success"]:
            answer["error"] = (f"Unity refused the parameter "
                               f"{parameter.name!r}: {outcome['error']}")
            return answer
        answer["parameters"].append(parameter.name)
        steps.append(f"add_animator_parameter:{parameter.name}")

    # --- the states -----------------------------------------------------
    for state in graph.states:
        body: Dict[str, Any] = {"controller": controller_path,
                                "name": state.name}
        if clips.get(state.name):
            body["motion"] = clips[state.name]
        if state.is_default:
            body["isDefault"] = True

        outcome = delivery._run("add_animator_state", body)
        if not outcome["success"]:
            answer["error"] = (f"Unity refused the state {state.name!r}: "
                               f"{outcome['error']}")
            return answer
        answer["states"].append(state.name)
        steps.append(f"add_animator_state:{state.name}")

    # --- the transitions ------------------------------------------------
    import json as _json

    for transition in graph.transitions:
        body = {"controller": controller_path,
                "fromState": transition.source,
                "toState": transition.target}
        if transition.conditions:
            body["conditions"] = _json.dumps(
                [c.payload() for c in transition.conditions],
                separators=(",", ":"))
        if transition.has_exit_time:
            body["hasExitTime"] = True
            body["exitTime"] = transition.exit_time
        if transition.duration is not None:
            body["duration"] = transition.duration

        outcome = delivery._run("add_animator_transition", body)
        edge = f"{transition.source}->{transition.target}"
        if not outcome["success"]:
            answer["error"] = (f"Unity refused the transition {edge}: "
                               f"{outcome['error']}")
            return answer
        answer["transitions"].append(edge)
        steps.append(f"add_animator_transition:{edge}")

    # add_animator_transition has no argument for canTransitionToSelf,
    # and Unity's default is true. Left alone, an AnyState transition
    # re-enters its own destination on every frame its condition holds
    # and the clip never gets past its opening frames.
    settled = _stop_self_interruption(controller_path)
    if settled:
        steps.append("no_self_interruption")
    else:
        warnings.append(
            "could not clear canTransitionToSelf on the AnyState "
            "transitions; states entered from AnyState may restart every "
            "frame their condition holds")

    answer["success"] = True
    return answer


_NO_SELF = """
var ctrl = UnityEditor.AssetDatabase.LoadAssetAtPath<UnityEditor.Animations.AnimatorController>("{controller}");
if (ctrl == null) return "NO_CONTROLLER";
int changed = 0;
foreach (var layer in ctrl.layers)
    foreach (var t in layer.stateMachine.anyStateTransitions)
        if (t.canTransitionToSelf) {{ t.canTransitionToSelf = false; changed++; }}
UnityEditor.EditorUtility.SetDirty(ctrl);
UnityEditor.AssetDatabase.SaveAssets();
return "cleared:" + changed;
"""


def _stop_self_interruption(controller_path: str) -> bool:
    outcome = delivery._run("eval", {
        "code": _NO_SELF.format(
            controller=str(controller_path or "").replace('"', "")),
        "timeout": 60000,
    })
    if not outcome["success"]:
        return False
    said = str(((outcome["data"] or {}) or {}).get("result") or "")
    return said.startswith("cleared:")
