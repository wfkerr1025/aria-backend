"""ARIA Lite - asking Ludo.ai to put a skeleton in a model.

WHAT THIS ADDS TO rig_model, AND WHAT IT DELIBERATELY DOES NOT
--------------------------------------------------------------
Rigging is a paid call, so the spec for this stage asks for reuse, a
ledger, ceilings, a unified error surface and idempotency. Four of
those five already exist and are not re-implemented here:

  reuse      ludo_client.call keys every billable call by a hash of
             what it asks for, so the same model rigged the same way
             twice is bought once. There is no reuse_key to pass -- a
             key computed from "prompt + settings" would be WRONG
             here, because the rig call's inputs are the model URL and
             the joint naming, and the prompt that made the model is
             not among them.

  ledger     the same function writes every outcome to ludo_spend.

  ceilings   ludo_spend.check runs before the POST. They are enforced,
             not passed: `run_id` is the only thing this layer supplies,
             and it is what makes a pipeline's calls count together.

  errors     the {success, ran, error} answer every Ludo verb returns,
             where `ran` is the difference between "credits may have
             gone" and "nothing was sent".

WHAT IS ACTUALLY NEW HERE
-------------------------
Idempotency against the FILESYSTEM. Reuse stops the same request being
bought twice within the ledger's memory; it does not stop a pipeline
re-rigging a model whose rigged file is already sitting on disk from
last week. That check costs nothing, needs no network, and is the one
that matters when a run is repeated days apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from logger import get_logger

from backend.ludo import ludo_actions as actions
from backend.ludo import ludo_client as client
from backend.ludo import ludo_spend as spend

logger = get_logger(__name__)

__all__ = [
    "DEFAULT_JOINT_NAMING",
    "DEFAULT_RIG_TYPE",
    "rig_character",
    "rigged_path_for",
]

# What a character wants. RIG_TYPES also offers "general" and "game",
# and neither produces the humanoid hierarchy Unity's avatar mapper
# needs; "realistic" is not one of the options at all.
DEFAULT_RIG_TYPE = "humanoid"

# Mixamo bone names, because that is the naming Unity's humanoid
# avatar mapper handles best. "unity" is not one of the options.
DEFAULT_JOINT_NAMING = "mixamo"


def rigged_path_for(model_path: str, folder: Optional[str] = None) -> Path:
    """Where the rigged version of a model belongs.

    Beside the model unless told otherwise, with a suffix that says
    what it is -- so a folder of assets reads without anybody having to
    open them.
    """
    source = Path(str(model_path))
    name = f"{source.stem}_rigged.glb"
    return (Path(folder) / name) if folder else source.with_name(name)


def rig_character(model_url: str, *, name: Optional[str] = None,
                  out: Optional[str] = None,
                  folder: Optional[str] = None,
                  rig_type: str = DEFAULT_RIG_TYPE,
                  joint_naming: str = DEFAULT_JOINT_NAMING,
                  run_id: str = "",
                  reuse: bool = True) -> dict:
    """Rig a model Ludo already made, and put the result on disk.

    `model_url` is a Ludo asset URL -- what generate_model returns --
    not a local file. The rig endpoint takes a model it can reach.

    IDEMPOTENCY IS CHECKED FIRST, BEFORE ANYTHING COSTS ANYTHING.
    If the rigged file is already there and `reuse` is on, nothing is
    sent, nothing is charged, and the answer says `reused`. That is
    deliberately the first thing this function does: every check that
    can happen before a paid call should.

    Returns success, ran, error, rigged_path, url, cost, reused, steps
    and warnings.
    """
    source = str(model_url or "").strip()
    if not source:
        return {"success": False, "ran": False,
                "error": "I need a model to rig.", "rigged_path": "",
                "url": "", "cost": {"calls": 0, "reused": 0},
                "reused": False, "steps": [], "warnings": []}

    label = str(name or "").strip()
    if not label:
        # The asset's own filename, which is what a Ludo URL ends with.
        label = Path(source.split("?")[0]).stem or "model"

    destination = Path(str(out)) if out else rigged_path_for(label, folder)

    steps: List[str] = []
    warnings: List[str] = []

    # --- Already done? -----------------------------------------------
    if reuse and destination.is_file() and destination.stat().st_size > 0:
        logger.info("rigged model already at %s; not rigging again",
                    destination)
        return {"success": True, "ran": False, "error": None,
                "rigged_path": str(destination), "url": "",
                "cost": {"calls": 0, "reused": 0}, "reused": True,
                "steps": ["reused_rigged_file"],
                "warnings": []}

    # --- The paid call ------------------------------------------------
    # rig_type and joint_naming are checked against the API's own enums
    # inside rig_model, before the request goes out, so a bad value
    # costs nothing rather than a credit.
    answer = actions.rig_model(source, rig_type=rig_type,
                               joint_naming=joint_naming, run_id=run_id)
    if not answer.get("success"):
        return {"success": False, "ran": bool(answer.get("ran")),
                "error": answer.get("error") or "the rig failed",
                "rigged_path": "", "url": "",
                "cost": answer.get("cost") or {"calls": 0, "reused": 0},
                "reused": False, "steps": steps, "warnings": warnings}

    steps.append("model_3d_rig")
    url = answer.get("url") or ""
    if not url:
        return {"success": False, "ran": True,
                "error": "Ludo rigged the model but did not say where it is.",
                "rigged_path": "", "url": "",
                "cost": answer.get("cost") or {"calls": 0, "reused": 0},
                "reused": False, "steps": steps, "warnings": warnings}

    # --- Get it on disk -----------------------------------------------
    # The credit is already spent by this point, so a download failure
    # must report the URL rather than swallow it: whoever picks this up
    # can still fetch the thing that was paid for.
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        client.download(url, str(destination))
    except Exception as error:      # pragma: no cover - network shapes vary
        logger.exception("could not download the rigged model")
        return {"success": False, "ran": True,
                "error": f"Ludo rigged the model but it could not be "
                         f"downloaded: {error}. It is at {url}",
                "rigged_path": "", "url": url,
                "cost": answer.get("cost") or {"calls": 0, "reused": 0},
                "reused": False, "steps": steps, "warnings": warnings}

    steps.append("download")

    if answer.get("cost", {}).get("reused"):
        warnings.append(
            "this rig came from an earlier identical request, so no credit "
            "was spent -- ask for a fresh one if you wanted a different rig")

    return {"success": True, "ran": True, "error": None,
            "rigged_path": str(destination), "url": url,
            "cost": answer.get("cost") or {"calls": 0, "reused": 0},
            "reused": False, "steps": steps, "warnings": warnings,
            "rig_type": rig_type, "joint_naming": joint_naming}
