"""ARIA Lite - putting generated motion into a Unity AnimationClip.

WHY NOT FBX
-----------
The obvious route is Blender -> FBX -> Unity, and it does not work.
Ludo's animate endpoint returns an animation-only GLB; the curves
transfer onto the rig in Blender perfectly (315 of 315, and the rig
demonstrably moves), the FBX exports, Blender reads 50 actions and 230
moving curves back out of it -- and Unity reports

    importAnimation=True animationType=Human takes=0

No take, so no AnimationClip. Explicit bake flags and an NLA strip made
no difference. It is a Blender 5.0 FBX exporter problem, not a Ludo or
Unity one, and rather than keep guessing at exporter flags the curves
go straight into a .anim asset instead.

WHAT MAPS, AND WHY IT MAPS SO SIMPLY
------------------------------------
Measured against the real rig rather than derived. Unity's imported
hierarchy has

    skintokens_rig            rotation (-0.7071, 0, 0, 0.7071)
    .../mixamorig:Hips        rotation (0.7071, 0, 0, 0.7071)
    every other bone          rotation (0, 0, 0, 1) -- identity

The whole Z-up to Y-up conversion sits on the root. Every bone below it
is at identity at rest, in Unity and in the source glTF alike, so a
bone-local rotation needs no axis conversion at all -- only Blender's
w,x,y,z quaternion order rewritten as Unity's x,y,z,w.

POSITIONS ARE A DIFFERENT MATTER and are deliberately not written for
ordinary bones. The animation's node translations are in the source
model's scale, and the cleaned model in Unity is scaled to 1.8m with
transforms applied, so the two disagree by a large factor. That costs
nothing: `mode: rot_only` is documented as "rotation + root translation
only, for retargeting", which is exactly what a humanoid clip uses.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Sequence

from logger import get_logger

from backend.unity import unity_delivery as delivery

logger = get_logger(__name__)

__all__ = [
    "ROTATION_PROPERTIES",
    "bone_paths",
    "curves_for_clip",
    "write_clip_curves",
]

# Unity's serialized names for a Transform's local rotation, in the
# order Blender stores the quaternion: Blender is w,x,y,z and Unity is
# x,y,z,w, so index 0 here is Blender's w.
ROTATION_PROPERTIES = ("m_LocalRotation.w", "m_LocalRotation.x",
                       "m_LocalRotation.y", "m_LocalRotation.z")

# Kept for the caller that removes them: writing root motion raw put
# the feet at 7.7m on a 1.8m character, so the export drops it and
# these name what to strip if an older clip still has them.
POSITION_PROPERTIES = ("m_LocalPosition.x", "m_LocalPosition.y",
                       "m_LocalPosition.z")

# C# that reports every transform under an object, as
# "path<tab>localPosition<tab>localRotation" lines. The paths are what
# a curve is addressed by, and guessing them from bone names would get
# the first fork in the skeleton wrong.
_HIERARCHY = """
var go = UnityEngine.GameObject.Find("{name}");
if (go == null) return "NO_OBJECT";
var sb = new System.Text.StringBuilder();
System.Action<UnityEngine.Transform, string> walk = null;
walk = (t, path) => {{
    for (int i = 0; i < t.childCount; i++) {{
        var c = t.GetChild(i);
        var p = string.IsNullOrEmpty(path) ? c.name : path + "/" + c.name;
        sb.Append(p).Append("\\n");
        walk(c, p);
    }}
}};
walk(go.transform, "");
return sb.ToString();
"""


def bone_paths(target: str) -> Dict[str, str]:
    """{bone name: path relative to the animated root}.

    Read from Unity rather than built from the rig, because a curve is
    addressed by its path and two bones can share a name at different
    places in a skeleton. Asking is cheaper than being wrong.
    """
    outcome = delivery._run("eval", {
        "code": _HIERARCHY.format(name=str(target or "").replace('"', "")),
        "timeout": 30000,
    })
    if not outcome["success"]:
        logger.warning("could not read the hierarchy: %s", outcome["error"])
        return {}

    said = str(((outcome["data"] or {}) or {}).get("result") or "")
    if not said or said == "NO_OBJECT":
        return {}

    paths: Dict[str, str] = {}
    for line in said.splitlines():
        line = line.strip()
        if not line:
            continue
        paths.setdefault(line.rsplit("/", 1)[-1], line)
    return paths


def curves_for_clip(dump: Dict[str, Any], paths: Dict[str, str], *,
                    frame_rate: float = 30.0) -> List[dict]:
    """Turn a curve export into set_animation_curve calls.

    `dump` is what blender_curve_export writes:

        {"bones": [{"bone": ..., "property": "rotation",
                    "keys": [{"frame": f, "wxyz": [w, x, y, z]}]}]}

    Frames become seconds here, because Unity times keys in seconds and
    Blender numbers them.

    ROTATIONS ONLY. The export drops translation before it gets here,
    for a measured reason: the animation's node positions are in the
    source model's scale, and writing them raw threw the character
    metres across the scene. There is no option to put them back,
    because there is no correct value to put back without the source
    scale -- a caller who wants root motion should scale it deliberately
    rather than have this guess.

    A bone Unity does not have is skipped rather than guessed at, so a
    rig that has drifted from its animation animates nothing rather
    than something wrong.
    """
    calls: List[dict] = []
    entries = dump.get("bones") if isinstance(dump, dict) else None

    for entry in entries or []:
        bone = entry.get("bone")
        path = paths.get(bone)
        if not path or entry.get("property") != "rotation":
            continue

        keys = entry.get("keys") or []
        for index, prop in enumerate(ROTATION_PROPERTIES):
            calls.append({
                "path": path, "type": "Transform", "property": prop,
                "keys": [{"time": round(float(key["frame"]) / frame_rate, 5),
                          "value": float(key["wxyz"][index])}
                         for key in keys if len(key.get("wxyz") or ()) == 4],
            })

    return [call for call in calls if call["keys"]]


def write_clip_curves(clip_path: str, calls: Sequence[dict], *,
                      on_progress=None) -> dict:
    """Write every curve into the clip, one command at a time.

    IT IS ONE CALL PER CURVE and there is no batch form: a humanoid
    skeleton is 45 bones, so a rotation-only clip is about 180 round
    trips. That is slow rather than wrong, and it is stated here so
    nobody discovers it by watching a progress bar.
    """
    written = 0
    failures: List[str] = []

    for index, call in enumerate(calls):
        body = {
            "clip": clip_path,
            "path": call["path"],
            "type": call.get("type", "Transform"),
            "property": call["property"],
            "keys": json.dumps(call["keys"], separators=(",", ":")),
        }
        outcome = delivery._run("set_animation_curve", body)
        if outcome["success"]:
            written += 1
        else:
            failures.append(f"{call['path']}.{call['property']}: "
                            f"{outcome['error']}")
        if on_progress:
            on_progress(index + 1, len(calls))

    return {"success": written > 0 and not failures,
            "ran": True,
            "error": "; ".join(failures[:3]) if failures else None,
            "written": written,
            "attempted": len(calls),
            "failures": failures}
