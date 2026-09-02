"""ARIA Lite - cleaning up a model that has a skeleton in it.

WHY THIS IS NOT JUST clean_for_unity
------------------------------------
It nearly is, and that was worth measuring rather than assuming. A
two-bone armature with automatic weights was built, exported, and put
through the static cleanup:

    bones             2 -> 2
    vertex groups     2 -> 2
    weighted vertices 24 -> 24
    ARMATURE modifier still present, still bound to the rig
    mesh still parented to the armature

So the static stage does not break a rig. What it leaves behind is the
reason this module exists:

    armature root scale  1.000 -> 0.900

scale_to_height multiplies object scale, which is harmless on a prop
and is not harmless here. Unity reads the armature root's transform
when it builds a humanoid avatar, and a root at 0.9 makes retargeted
animation subtly wrong in a way that is very hard to trace back to an
export step months later. So this stage applies transforms afterwards,
and then CHECKS the rig is still bound rather than trusting that it is.

WHAT "STILL BOUND" MEANS
------------------------
Not a bone count. A rig can come through an export with every bone
present and no weights on any of them: it looks right in the outliner
and does nothing at all when animated. So the check is bones AND
weighted vertices AND the armature modifier's binding, measured before
and after, and a stage that loses any of them says so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from logger import get_logger

from backend.blender import blender_actions as actions

logger = get_logger(__name__)

__all__ = [
    "DEFAULT_HEIGHT",
    "clean_rigged_model",
    "rig_warnings",
]

# A person, in metres, the same number the static stage uses. Repeated
# through an import rather than redefined, so the two stages cannot
# drift into disagreeing about how tall a character is.
DEFAULT_HEIGHT = actions.DEFAULT_HEIGHT


def rig_warnings(after: Dict[str, Any], before: Dict[str, Any]) -> List[str]:
    """What is wrong with the skeleton, in words.

    Each of these is something that produces a model which imports
    without complaint and cannot be animated, which is the worst shape
    of failure available here: everything downstream succeeds and the
    problem surfaces when somebody tries to use it.
    """
    problems = []

    if not after.get("armatures"):
        problems.append("the rig is gone -- there is no armature in the export")
        return problems

    if not after.get("bones"):
        problems.append("the armature has no bones")

    if not after.get("weighted_vertices"):
        problems.append(
            "no vertex is weighted to any bone, so the skeleton will move "
            "and the mesh will not follow it")

    if not after.get("bound_meshes"):
        problems.append(
            "no mesh has an armature modifier bound to the rig")

    if before.get("bones") and after.get("bones", 0) < before["bones"]:
        problems.append(
            f"{before['bones'] - after['bones']} bones were lost in the "
            "cleanup")

    if before.get("weighted_vertices") and \
            after.get("weighted_vertices", 0) < before["weighted_vertices"]:
        lost = before["weighted_vertices"] - after["weighted_vertices"]
        problems.append(f"{lost} vertices lost their weights in the cleanup")

    scales = after.get("root_scales") or []
    off = [s for s in scales if abs(float(s) - 1.0) > 0.001]
    if off:
        problems.append(
            f"the armature root is at scale {off[0]} rather than 1, which "
            "will distort a retargeted animation")

    return problems


def clean_rigged_model(path: str, *, out: Optional[str] = None,
                       height: float = DEFAULT_HEIGHT,
                       merge: bool = False,
                       export: str = "fbx",
                       timeout: Optional[int] = None,
                       on_output=None) -> dict:
    """Clean a rigged model and hand back one Unity can animate.

    The static stage's steps, in the same order and for the same
    measured reasons, plus two the skeleton needs:

        measure_rig      before anything, so loss can be detected
        ...the static cleanup...
        apply_transforms so the armature root goes back to scale 1
        measure_rig      again, and compare

    Merging stays off by default for the same reason it does there: on
    a real model it created 727 non-manifold edges to save vertices
    Unity re-splits on import anyway. On a SKINNED mesh it is worse
    than that, because welding two vertices with different weights has
    to discard one of them -- so `merge` here is very rarely the right
    answer and is never the default.

    Returns the static stage's answer plus `rig_before`, `rig_after`
    and rig warnings folded into `warnings`.
    """
    source = str(path or "").strip()
    if not source:
        return {"success": False, "ran": False,
                "error": "I need a rigged model to clean.",
                "output": "", "result": None, "script": None,
                "warnings": [], "rig_before": {}, "rig_after": {}}

    if not Path(source).exists():
        return {"success": False, "ran": False,
                "error": f"There is no file at {source}.",
                "output": "", "result": None, "script": None,
                "warnings": [], "rig_before": {}, "rig_after": {}}

    kind = str(export or "fbx").strip().lower()
    if kind not in actions.EXPORT_ACTIONS:
        return {"success": False, "ran": False,
                "error": f"I can export fbx, glb or obj, not {export!r}.",
                "output": "", "result": None, "script": None,
                "warnings": [], "rig_before": {}, "rig_after": {}}

    destination = str(out or "").strip()
    if not destination:
        destination = str(Path(source).with_name(
            Path(source).stem + f"_rigged.{kind}"))

    steps: List[Dict[str, Any]] = [
        {"action": "clear_scene", "params": {}},
        {"action": "import_model", "params": {"path": source}},
        {"action": "measure_rig", "params": {}},
        {"action": "measure_mesh", "params": {}},
        {"action": "remove_loose", "params": {}},
        {"action": "recalculate_normals", "params": {}},
    ]
    if merge:
        steps.append({"action": "merge_by_distance", "params": {}})
    steps += [
        {"action": "scale_to_height", "params": {"height": height}},
        {"action": "origin_to_floor", "params": {}},
        # After the moves, never before: applying a transform that is
        # about to be changed again achieves nothing.
        {"action": "apply_transforms", "params": {}},
        {"action": "measure_mesh", "params": {}},
        {"action": "measure_rig", "params": {}},
        {"action": actions.EXPORT_ACTIONS[kind], "params": {"path": destination}},
    ]

    answer = actions.run_actions(steps, timeout=timeout, on_output=on_output)

    result = answer.get("result") or {}
    measurements = result.get("measurements") or []
    rigs = result.get("rigs") or []

    before = measurements[0] if measurements else {}
    after = measurements[-1] if len(measurements) > 1 else {}
    rig_before = rigs[0] if rigs else {}
    rig_after = rigs[-1] if len(rigs) > 1 else {}

    answer["before"] = before
    answer["after"] = after
    answer["rig_before"] = rig_before
    answer["rig_after"] = rig_after
    answer["output"] = destination if answer.get("success") else ""

    if answer.get("success"):
        answer["warnings"] = (actions._warnings_about(after, before)
                              + rig_warnings(rig_after, rig_before))
    else:
        answer["warnings"] = []

    return answer
