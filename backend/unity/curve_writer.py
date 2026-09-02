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

A ROTATION CURVE IS A POSE, NOT A MOTION, AND THAT MATTERS
----------------------------------------------------------
An earlier version of this module wrote the animation's quaternions
straight into Unity's curves, on the grounds that Unity's hierarchy has

    skintokens_rig            rotation (-0.7071, 0, 0, 0.7071)
    .../mixamorig:Hips        rotation ( 0.7071, 0, 0, 0.7071)
    every other bone          identity

so the axis conversion sits on the root and the bones need none. That
reasoning was checked against ONE bone whose rest was identity, and one
sample does not validate a basis.

It is wrong for exactly the bone it matters on. Unity's Hips carries
+90 degrees about X, which is what cancels the root's -90. The
animation's Hips curve is near identity, so writing it absolutely
DESTROYED that compensation: the character rotated 90 degrees onto its
back, and every child bone's motion was then applied in a frame rotated
by 90 degrees -- which read as swimming rather than walking.

So a curve value is composed rather than copied:

    unity(t) = unity_rest * inverse(source_first_frame) * source(t)

The animation contributes only its DEVIATION from its own first frame,
and that deviation is applied on top of whatever pose Unity imported.
At t=0 this reproduces Unity's rest exactly, whatever it happens to be,
which is the property the previous version lacked and the reason the
character now stands up.

Bones whose rest is identity in both are unaffected, which is why 42 of
45 looked plausible and the three that did not were the ones holding
the model upright.

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
    "bone_rest",
    "inverse",
    "multiply",
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
# "path<tab>localRotation" lines. The paths are what a curve is
# addressed by, and guessing them from bone names would get the first
# fork in the skeleton wrong.
#
# THE ASSET IS PREFERRED OVER THE SCENE, and that is not a detail. A
# scene object's localRotation is its CURRENT pose, and sampling a clip
# against it leaves it posed. Reading the rest pose from a posed
# instance returned 35 non-identity rotations where the asset has 3 --
# and composing onto those would bake one animation's pose into the
# next one's curves.
_HIERARCHY = """
UnityEngine.GameObject go = null;
var assetPath = "{asset}";
if (!string.IsNullOrEmpty(assetPath))
    go = UnityEditor.AssetDatabase.LoadAssetAtPath<UnityEngine.GameObject>(assetPath);
if (go == null) go = UnityEngine.GameObject.Find("{name}");
if (go == null) return "NO_OBJECT";
var sb = new System.Text.StringBuilder();
System.Action<UnityEngine.Transform, string> walk = null;
walk = (t, path) => {{
    for (int i = 0; i < t.childCount; i++) {{
        var c = t.GetChild(i);
        var p = string.IsNullOrEmpty(path) ? c.name : path + "/" + c.name;
        var r = c.localRotation;
        sb.Append(p).Append("\\t")
          .Append(r.w.ToString("F6")).Append(",").Append(r.x.ToString("F6"))
          .Append(",").Append(r.y.ToString("F6")).Append(",").Append(r.z.ToString("F6"))
          .Append("\\n");
        walk(c, p);
    }}
}};
walk(go.transform, "");
return sb.ToString();
"""


def multiply(left, right):
    """Hamilton product, both quaternions as (w, x, y, z)."""
    lw, lx, ly, lz = left
    rw, rx, ry, rz = right
    return (
        lw * rw - lx * rx - ly * ry - lz * rz,
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
    )


def inverse(quaternion):
    """The conjugate, which is the inverse for a unit quaternion.

    Normalised first rather than assumed: values that have been through
    a JSON round trip and a rounding to six places are unit to about
    six places, and the error compounds across a chain of bones.
    """
    w, x, y, z = quaternion
    norm = (w * w + x * x + y * y + z * z) or 1.0
    return (w / norm, -x / norm, -y / norm, -z / norm)


def bone_paths(target: str, asset_path: str = "") -> Dict[str, str]:
    """{bone name: path relative to the animated root}.

    Read from Unity rather than built from the rig, because a curve is
    addressed by its path and two bones can share a name at different
    places in a skeleton. Asking is cheaper than being wrong.
    """
    outcome = delivery._run("eval", {
        "code": _HIERARCHY.format(
            name=str(target or "").replace('"', ""),
            asset=str(asset_path or "").replace('"', "")),
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
        path = line.split("\t")[0]
        paths.setdefault(path.rsplit("/", 1)[-1], path)
    return paths


def bone_rest(target: str, asset_path: str = "") -> Dict[str, dict]:
    """{bone: {"path": ..., "rest": (w, x, y, z)}} straight from Unity.

    The rest pose is what the animation is applied ON TOP OF, so it has
    to come from the model that will play the clip rather than from the
    file the motion was generated against.

    AND IT HAS TO COME FROM THE ASSET. A scene object's localRotation is
    whatever pose it is currently in, and sampling a clip leaves it
    posed -- reading a posed instance gave 35 non-identity rotations
    where the prefab has 3. Composing onto those would fold one
    animation's pose into the next one's curves, and the error would
    compound every time the pipeline ran.
    """
    outcome = delivery._run("eval", {
        "code": _HIERARCHY.format(
            name=str(target or "").replace('"', ""),
            asset=str(asset_path or "").replace('"', "")),
        "timeout": 30000,
    })
    if not outcome["success"]:
        logger.warning("could not read the rest pose: %s", outcome["error"])
        return {}

    said = str(((outcome["data"] or {}) or {}).get("result") or "")
    if not said or said == "NO_OBJECT":
        return {}

    found: Dict[str, dict] = {}
    for line in said.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        path = parts[0]
        rest = (1.0, 0.0, 0.0, 0.0)
        if len(parts) > 1:
            try:
                rest = tuple(float(v) for v in parts[1].split(","))
            except ValueError:              # pragma: no cover - defensive
                rest = (1.0, 0.0, 0.0, 0.0)
        found.setdefault(path.rsplit("/", 1)[-1],
                         {"path": path, "rest": rest})
    return found


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
        known = paths.get(bone)
        if not known or entry.get("property") != "rotation":
            continue

        # Accept either a bare path or the {"path", "rest"} that
        # bone_rest returns, so a caller with no rest data still works
        # -- it simply composes onto identity, which is the old
        # behaviour and is correct wherever the rest IS identity.
        if isinstance(known, dict):
            path = known.get("path") or ""
            rest = tuple(known.get("rest") or (1.0, 0.0, 0.0, 0.0))
        else:
            path, rest = known, (1.0, 0.0, 0.0, 0.0)
        if not path:
            continue

        keys = [key for key in (entry.get("keys") or [])
                if len(key.get("wxyz") or ()) == 4]
        if not keys:
            continue

        # The animation contributes its deviation from its OWN first
        # frame. At t=0 that is identity, so the output is Unity's rest
        # exactly -- which is what keeps the character standing up.
        reference = inverse(tuple(float(v) for v in keys[0]["wxyz"]))

        posed = []
        for key in keys:
            source = tuple(float(v) for v in key["wxyz"])
            value = multiply(rest, multiply(reference, source))
            posed.append((round(float(key["frame"]) / frame_rate, 5), value))

        for index, prop in enumerate(ROTATION_PROPERTIES):
            calls.append({
                "path": path, "type": "Transform", "property": prop,
                "keys": [{"time": time, "value": value[index]}
                         for time, value in posed],
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
