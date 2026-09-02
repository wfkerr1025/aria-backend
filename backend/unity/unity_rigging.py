"""ARIA Lite - a static model in, an animatable Unity prefab out.

THE WHOLE PIPELINE, AND WHERE EACH PIECE ALREADY LIVED
------------------------------------------------------
    ludo_rigging.rig_character       Ludo puts a skeleton in it
    blender_rigging.clean_rigged     scale, origin, normals, and the
                                     armature root back to scale 1
    unity_delivery.place_character   import, prefab, instantiate,
                                     transform
    this module                      an Animator, and the seams

Almost nothing here is new. What is new is the order, the checks
between stages, and one component. That is deliberate: a pipeline that
reimplements its stages is a pipeline with two of everything, and the
copy nobody updated is the one somebody is running.

THE ANIMATOR, AND WHAT THIS PIPELINE WILL NOT DO
------------------------------------------------
An Animator component and nothing else. No AnimatorController, no
states, no clips -- that is the Animation Pipeline's work, and a
controller invented here would be one this pipeline cannot maintain
and the next one has to undo.

An Animator with no controller is a real, valid, useful thing: it is
what makes the prefab animatable, and it is exactly what a later stage
needs to find already present.

WHY THE ORDER CANNOT BE REARRANGED
----------------------------------
Rig BEFORE cleanup, because the cleanup normalises height and origin
and a rig applied afterwards would be built around the wrong scale.
Cleanup BEFORE Unity, because y=0 only stands on the floor once the
origin is at the model's feet. And within Unity, import, instantiate,
prefab, transform -- because create_prefab saves a SCENE object and
cannot be given an asset.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from logger import get_logger

from backend.blender import blender_rigging
from backend.ludo import ludo_rigging
from backend.unity import unity_delivery

logger = get_logger(__name__)

__all__ = [
    "ANIMATOR",
    "rig_to_unity",
]

# The component this stage guarantees. Spelled the way Unity spells it,
# because add_component resolves a type by name and a near miss is an
# error from inside the Editor rather than from here.
ANIMATOR = "Animator"


def _answer(success: bool, error: Optional[str], **fields) -> dict:
    result = {
        "success": success, "ran": fields.pop("ran", True), "error": error,
        "asset_path": "", "rigged_path": "", "prefab_path": "",
        "instance": "", "animator": False, "reused": False,
        "steps": [], "warnings": [], "cost": {"calls": 0, "reused": 0},
    }
    result.update(fields)
    return result


def rig_to_unity(model_url: str, *, name: Optional[str] = None,
                 work_folder: Optional[str] = None,
                 rig_type: str = ludo_rigging.DEFAULT_RIG_TYPE,
                 joint_naming: str = ludo_rigging.DEFAULT_JOINT_NAMING,
                 height: float = blender_rigging.DEFAULT_HEIGHT,
                 scene_path: Optional[str] = None,
                 position=None, rotation=None, scale=None,
                 collider: Optional[str] = None,
                 rigidbody: bool = False,
                 run_id: Optional[str] = None,
                 reuse: bool = True) -> dict:
    """Take a Ludo model URL and leave an animatable prefab in a scene.

    `run_id` groups every paid call this pipeline makes so the per-run
    ceiling applies to the pipeline rather than to each step of it. One
    is generated when not supplied, because a run that shares no id
    with itself cannot be capped as a run.

    Stops at the first stage that fails, and says which stage. A
    pipeline that reports "it failed" without naming where is a
    pipeline somebody has to re-run with print statements in it.

    Returns asset_path, rigged_path, prefab_path, instance, animator,
    reused, steps, warnings -- plus cost, so what the run spent is
    visible in the same answer as what it produced.
    """
    if not str(model_url or "").strip():
        return _answer(False, "I need a model to rig.", ran=False)

    run = str(run_id or f"rig-{uuid.uuid4().hex[:12]}")
    label = str(name or "").strip() or Path(
        str(model_url).split("?")[0]).stem or "character"

    steps: List[str] = []
    warnings: List[str] = []

    # --- 1. Ludo rigs it ----------------------------------------------
    rigged = ludo_rigging.rig_character(
        model_url, name=label, folder=work_folder, rig_type=rig_type,
        joint_naming=joint_naming, run_id=run, reuse=reuse)

    cost = rigged.get("cost") or {"calls": 0, "reused": 0}
    if not rigged["success"]:
        return _answer(False, f"Rigging failed: {rigged['error']}",
                       ran=rigged.get("ran", True), cost=cost,
                       steps=steps, warnings=rigged.get("warnings") or [])

    steps += [f"ludo:{s}" for s in rigged.get("steps", [])]
    warnings += rigged.get("warnings") or []
    rigged_source = rigged["rigged_path"]

    # --- 2. Blender cleans it -----------------------------------------
    cleaned_to = str(Path(rigged_source).with_suffix("")) + "_unity.fbx"
    cleaned = blender_rigging.clean_rigged_model(
        rigged_source, out=cleaned_to, height=height)

    if not cleaned["success"]:
        return _answer(False, f"Cleanup failed: {cleaned['error']}",
                       ran=cleaned.get("ran", True), cost=cost,
                       rigged_path=rigged_source, steps=steps,
                       warnings=warnings)

    steps.append("blender:clean_rigged_model")
    warnings += cleaned.get("warnings") or []

    # A rig that did not survive is worth stopping for. Everything
    # after this point succeeds regardless -- Unity will happily import
    # and prefab an unriggable mesh -- and the failure would then show
    # up only when somebody tried to animate it.
    rig_after = cleaned.get("rig_after") or {}
    if not rig_after.get("bones") or not rig_after.get("weighted_vertices"):
        return _answer(
            False,
            "The cleaned model has no usable skeleton, so there is nothing "
            "for an Animator to drive. Not importing it.",
            cost=cost, rigged_path=rigged_source, steps=steps,
            warnings=warnings + blender_rigging.rig_warnings(
                rig_after, cleaned.get("rig_before") or {}))

    # --- 3. Unity takes it --------------------------------------------
    placed = unity_delivery.place_character(
        cleaned["output"], name=label, scene_path=scene_path,
        position=position, rotation=rotation, scale=scale,
        collider=collider, rigidbody=rigidbody,
        components=[ANIMATOR],
        # Without this the FBX imports Generic with no Avatar, and the
        # Animator we are about to add has nothing to drive.
        import_settings=unity_delivery.HUMANOID_IMPORT,
        reuse=reuse)

    if not placed["success"]:
        return _answer(False, f"Unity failed: {placed['error']}",
                       ran=placed.get("ran", True), cost=cost,
                       rigged_path=rigged_source,
                       asset_path=placed.get("asset_path", ""),
                       prefab_path=placed.get("prefab_path", ""),
                       steps=steps + [f"unity:{s}"
                                      for s in placed.get("steps", [])],
                       warnings=warnings)

    steps += [f"unity:{s}" for s in placed.get("steps", [])]

    # place_character warns about a missing Animator. This pipeline
    # asked for one, so that warning is either untrue or important --
    # and which it is is decided by whether the component went on.
    animator = ANIMATOR in (placed.get("components") or [])
    warnings += [w for w in (placed.get("warnings") or [])
                 if not (animator and "Animator" in w)]

    if not animator:
        warnings.append(
            "the Animator did not attach, so this prefab cannot be animated "
            "yet even though it has a skeleton")

    return _answer(
        True, None,
        asset_path=placed.get("asset_path", ""),
        rigged_path=rigged_source,
        cleaned_path=cleaned["output"],
        prefab_path=placed.get("prefab_path", ""),
        instance=placed.get("instance", ""),
        scene_path=placed.get("scene_path", ""),
        animator=animator,
        bones=rig_after.get("bones", 0),
        reused=bool(rigged.get("reused")) or bool(placed.get("reused")),
        run_id=run,
        cost=cost,
        steps=steps,
        warnings=warnings,
    )
