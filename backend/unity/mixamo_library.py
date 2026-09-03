"""ARIA Lite - using Mixamo clips instead of generated motion.

WHY THIS EXISTS, AND WHAT IT REPLACES
-------------------------------------
Ludo can generate motion for a rigged model, and it was built, paid
for and measured. The result is not usable for locomotion: the
generated walk has BOTH THIGHS swinging the same way at the same time
-- measured off the source curves, never anti-phase at any frame --
which reads as a shuffle or a swim rather than a walk. A second
generation with a more prescriptive prompt came back with six moving
curves out of 180, effectively static.

Mixamo's clips are hand-authored, free, and already correct. The
tradeoff is that there is no API: somebody downloads a folder of FBX
once, and everything after that is automatic.

WHY THE CLIPS RETARGET AT ALL
-----------------------------
Ludo rigs with `joint_naming: "mixamo"`, so the character came back
with mixamorig:Hips, Spine, Spine2, Neck, Head and both full arm and
leg chains -- Unity's complete required humanoid set. Imported as
Humanoid, both the character and the clips get an Avatar, and Unity
retargets between them. That is what the Avatar system is for, and it
is why this route needs Humanoid where the generated-curve route
needed Generic.

NAMING
------
Mixamo exports as "Character@Clip Name.fbx" and Unity takes the part
after the @ as the clip name. Nothing needs renaming.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from logger import get_logger

from backend.unity import unity_delivery as delivery

logger = get_logger(__name__)

__all__ = [
    "DEFAULT_ALIASES",
    "DEFAULT_FOLDER",
    "HUMANOID_IMPORT",
    "LOOPING_STATES",
    "ONE_SHOT_STATES",
    "assign_clips",
    "configure_humanoid",
    "configure_looping",
    "discover",
    "match_states",
    "set_looping",
]

DEFAULT_FOLDER = "Assets/ARIA/Animations/Mixamo"

# A clip retargets through the Avatar system, which needs both the
# character and the clip imported as Humanoid.
HUMANOID_IMPORT = {"animationType": "Human",
                   "avatarSetup": "CreateFromThisModel"}

# What to look for, per state, IN ORDER. First match wins.
#
# Ordered rather than scored on purpose. "walk" matches both "Walking"
# and "Crouched Walking"; "run" matches "Running" and "Running Jump";
# "idle" matches "Standing Idle", "Crouching Idle" and "Falling Idle".
# A similarity score would pick between those by accident, and the
# accident would change when somebody downloads one more clip. An
# ordered list says which one is meant and can be overridden per call.
DEFAULT_ALIASES: Dict[str, Sequence[str]] = {
    "Idle": ("standing idle", "idle", "breathing idle"),
    "Walk": ("walking", "walk", "slow walk"),
    "Run": ("running", "run", "fast run", "sprint", "jog"),
    "Attack": ("sword and shield slash", "slash", "attack", "punch",
               "swing", "stab", "kick"),
    "Jump": ("jump", "jumping"),
    "Fall": ("falling idle", "falling"),
    "Land": ("landing", "hard landing"),
    "Crouch": ("crouching idle", "crouch"),
    "CrouchWalk": ("crouched walking", "crouch walk", "sneak"),
    "DodgeLeft": ("standing dodge left", "dodge left", "roll left"),
    "DodgeRight": ("standing dodge right", "dodge right", "roll right"),
}

# Which states are cycles and which play once.
#
# MEASURED, and it is the thing somebody notices first: every Mixamo
# clip imports with loopTime FALSE. Walking is 31 frames and Running
# is 19, so without this they play for about a second and stop dead --
# reported as "the clips didn't play very long".
#
# An attack must NOT loop. A one-shot that repeats is a character
# swinging a sword forever, and the Attack state leaves on exit time,
# which never arrives if the clip restarts.
LOOPING_STATES = ("Idle", "Walk", "Run", "Crouch", "CrouchWalk", "Fall")
ONE_SHOT_STATES = ("Attack", "Jump", "Land", "DodgeLeft", "DodgeRight")

# Setting it needs the whole clip list rewritten: clipAnimations starts
# empty and the importer falls back to defaultClipAnimations, so the
# defaults are copied in, edited, and assigned back. Assigning a single
# element of the array does nothing -- it is a struct array and the
# element is a copy.
_SET_LOOP = """
var im = (UnityEditor.ModelImporter)UnityEditor.AssetImporter.GetAtPath("{asset}");
if (im == null) return "NO_IMPORTER";
var clips = im.clipAnimations.Length > 0 ? im.clipAnimations
                                         : im.defaultClipAnimations;
if (clips.Length == 0) return "NO_CLIPS";
for (int i = 0; i < clips.Length; i++) clips[i].loopTime = {loop};
im.clipAnimations = clips;
UnityEditor.EditorUtility.SetDirty(im);
im.SaveAndReimport();
var after = im.clipAnimations;
return after[0].name + "=" + after[0].loopTime;
"""


def set_looping(asset: str, loop: bool) -> dict:
    """Turn a clip's loop-time flag on or off, and re-import.

    Unity's loopTime is not Ludo's `loop` and not the clip's own
    content: it decides whether playback wraps. A cyclic gait needs it
    and a one-shot must not have it.
    """
    if not str(asset or "").strip():
        return delivery._failure("I need an asset to configure.")

    outcome = delivery._run("eval", {
        "code": _SET_LOOP.format(asset=str(asset).replace('"', ""),
                                 loop="true" if loop else "false"),
        "timeout": 120000,
    })
    if not outcome["success"]:
        return {"success": False, "ran": True, "error": outcome["error"],
                "asset": asset}

    said = str(((outcome["data"] or {}) or {}).get("result") or "")
    if said in ("NO_IMPORTER", "NO_CLIPS") or "=" not in said:
        return {"success": False, "ran": True, "asset": asset,
                "error": f"could not set looping on {asset}: "
                         f"{said or 'no answer'}"}

    name, _, value = said.rpartition("=")
    became = value.strip().lower() == "true"
    return {"success": became == loop, "ran": True, "asset": asset,
            "clip": name, "loop": became,
            "error": None if became == loop else
                     f"asked for loopTime={loop} and got {became}"}


def configure_looping(matched: Dict[str, dict], *,
                      looping: Sequence[str] = LOOPING_STATES,
                      one_shot: Sequence[str] = ONE_SHOT_STATES) -> dict:
    """Loop the cycles, leave the one-shots alone.

    A state named in neither list is left as Unity imported it rather
    than guessed at: whether an unfamiliar motion should repeat is not
    something to decide from its name.
    """
    looped: Dict[str, bool] = {}
    skipped: List[str] = []
    failures: List[str] = []

    for state, entry in sorted(matched.items()):
        if state in looping:
            wanted = True
        elif state in one_shot:
            wanted = False
        else:
            skipped.append(state)
            continue

        outcome = set_looping(entry["asset"], wanted)
        if outcome["success"]:
            looped[state] = wanted
        else:
            failures.append(f"{state}: {outcome['error']}")

    return {"success": bool(looped) and not failures, "ran": True,
            "error": "; ".join(failures[:3]) if failures else None,
            "looping": looped, "left_alone": skipped, "failures": failures}


# The C# that lists what is in the folder. FindAssets rather than a
# directory walk, so a file Unity has not imported yet is simply not
# there rather than being reported as a clip that cannot be used.
_DISCOVER = """
UnityEditor.AssetDatabase.Refresh();
var guids = UnityEditor.AssetDatabase.FindAssets("t:Model",
    new string[]{{"{folder}"}});
var sb = new System.Text.StringBuilder();
foreach (var g in guids)
{{
    var p = UnityEditor.AssetDatabase.GUIDToAssetPath(g);
    var im = (UnityEditor.ModelImporter)UnityEditor.AssetImporter.GetAtPath(p);
    var clip = "";
    float len = 0f;
    foreach (var a in UnityEditor.AssetDatabase.LoadAllAssetsAtPath(p))
    {{
        var c = a as UnityEngine.AnimationClip;
        if (c != null && !c.name.StartsWith("__preview"))
        {{ clip = c.name; len = c.length; }}
    }}
    sb.Append(p).Append("\\t").Append(clip).Append("\\t")
      .Append(len.ToString("F2")).Append("\\t")
      .Append(im.animationType.ToString()).Append("\\n");
}}
return sb.ToString();
"""


def discover(folder: str = DEFAULT_FOLDER) -> List[dict]:
    """Every animation model in the folder, as Unity sees it.

    Returns [{"asset", "clip", "length", "import_type"}]. A file Unity
    has not imported yet does not appear, which is the right answer:
    it has no clip to assign.
    """
    where = str(folder or DEFAULT_FOLDER).replace(delivery.SLASH, "/").strip("/")
    outcome = delivery._run("eval", {
        "code": _DISCOVER.format(folder=where.replace('"', "")),
        "timeout": 120000,
    })
    if not outcome["success"]:
        logger.warning("could not list %s: %s", where, outcome["error"])
        return []

    said = str(((outcome["data"] or {}) or {}).get("result") or "")
    found: List[dict] = []
    for line in said.splitlines():
        parts = line.rstrip().split("\t")
        if len(parts) < 2 or not parts[0]:
            continue
        try:
            length = float(parts[2]) if len(parts) > 2 else 0.0
        except ValueError:
            length = 0.0
        found.append({
            "asset": parts[0],
            "clip": parts[1],
            "length": length,
            "import_type": parts[3] if len(parts) > 3 else "",
        })
    return found


def match_states(clips: Sequence[dict], states: Sequence[str], *,
                 aliases: Optional[Dict[str, Sequence[str]]] = None) -> dict:
    """Pick one clip per state, and say what was left over.

    Matching is on the CLIP name rather than the filename, because that
    is what Unity shows and what somebody would look for. Whole-word
    containment, in alias order, first match wins.

    Returns {"matched": {state: entry}, "missing": [...],
             "unused": [entries]}.
    """
    table = dict(DEFAULT_ALIASES)
    table.update(aliases or {})

    by_name = {(entry.get("clip") or "").strip().lower(): entry
               for entry in clips if entry.get("clip")}

    matched: Dict[str, dict] = {}
    taken = set()

    for state in states:
        wanted = table.get(state) or (state.lower(),)
        for alias in wanted:
            alias = alias.strip().lower()
            # Exact first, so "Walking" beats "Crouched Walking" even
            # when both contain the alias.
            hit = by_name.get(alias)
            if hit is None:
                for name, entry in sorted(by_name.items()):
                    if name in taken:
                        continue
                    if alias == name or alias in name.split():
                        hit = entry
                        break
                    if alias in name:
                        hit = entry
                        break
            if hit is not None and hit["asset"] not in taken:
                matched[state] = hit
                taken.add(hit["asset"])
                break

    return {
        "matched": matched,
        "missing": [s for s in states if s not in matched],
        "unused": [e for e in clips if e["asset"] not in taken],
    }


# Assigning a state's motion needs eval, and that is not a shortcut.
# add_animator_state takes `motion` as an ObjectRef, and an ObjectRef
# resolves an FBX path to the model's GameObject -- the AnimationClip
# is a SUB-asset inside the file and there is no path syntax for it.
# So the clip is found by walking LoadAllAssetsAtPath, which is what
# the Editor itself does.
_ASSIGN = """
var ctrl = UnityEditor.AssetDatabase.LoadAssetAtPath<UnityEditor.Animations.AnimatorController>("{controller}");
if (ctrl == null) return "NO_CONTROLLER";
var wanted = new System.Collections.Generic.Dictionary<string,string> {{
{pairs}
}};
var sb = new System.Text.StringBuilder();
foreach (var s in ctrl.layers[0].stateMachine.states)
{{
    string fbx;
    if (!wanted.TryGetValue(s.state.name, out fbx)) continue;
    UnityEngine.AnimationClip clip = null;
    foreach (var a in UnityEditor.AssetDatabase.LoadAllAssetsAtPath(fbx))
    {{
        var c = a as UnityEngine.AnimationClip;
        if (c != null && !c.name.StartsWith("__preview")) clip = c;
    }}
    if (clip == null) {{ sb.Append(s.state.name).Append("=NO_CLIP;"); continue; }}
    s.state.motion = clip;
    sb.Append(s.state.name).Append("=").Append(clip.name)
      .Append(clip.isHumanMotion ? ":human;" : ":generic;");
}}
UnityEditor.EditorUtility.SetDirty(ctrl);
UnityEditor.AssetDatabase.SaveAssets();
return sb.ToString();
"""


def assign_clips(controller_path: str, matched: Dict[str, dict]) -> dict:
    """Point each state at its clip.

    Returns success, assigned {state: clip}, and non_humanoid -- a clip
    that came through as generic will not retarget, and saying so is
    the difference between a character that stands still and an
    afternoon looking for why.
    """
    if not matched:
        return {"success": False, "ran": False,
                "error": "no clips to assign", "assigned": {}}

    pairs = ("," + chr(10)).join(
        # Single braces: this text is inserted AFTER _ASSIGN.format(),
        # so it must not carry the doubling that template needs.
        '    {"%s", "%s"}' % (state, entry["asset"].replace('"', ""))
        for state, entry in sorted(matched.items()))

    outcome = delivery._run("eval", {
        "code": _ASSIGN.format(
            controller=str(controller_path or "").replace('"', ""),
            pairs=pairs),
        "timeout": 120000,
    })
    if not outcome["success"]:
        return {"success": False, "ran": True, "error": outcome["error"],
                "assigned": {}}

    said = str(((outcome["data"] or {}) or {}).get("result") or "")
    if said == "NO_CONTROLLER":
        return {"success": False, "ran": True, "assigned": {},
                "error": f"there is no controller at {controller_path}"}

    assigned: Dict[str, str] = {}
    generic: List[str] = []
    for chunk in said.split(";"):
        if "=" not in chunk:
            continue
        state, _, rest = chunk.partition("=")
        clip, _, kind = rest.rpartition(":")
        if not clip:
            continue
        assigned[state] = clip
        if kind != "human":
            generic.append(f"{state} ({clip})")

    return {"success": bool(assigned), "ran": True,
            "error": None if assigned else "nothing was assigned",
            "assigned": assigned, "non_humanoid": generic}


def configure_humanoid(assets: Sequence[str]) -> dict:
    """Import each model as Humanoid so the Avatar system can retarget.

    A Mixamo FBX carries its own skeleton, so each gets its own avatar
    from itself; Unity maps clip avatar to character avatar at runtime.
    That is the whole reason this route works where writing bone-local
    curves did not.
    """
    done: List[str] = []
    failed: List[str] = []

    for asset in assets:
        outcome = delivery.set_import_settings(asset, HUMANOID_IMPORT)
        if outcome["success"] and not outcome.get("unknown"):
            done.append(asset)
        else:
            failed.append(f"{asset}: "
                          f"{outcome.get('error') or outcome.get('unknown')}")

    return {"success": bool(done) and not failed,
            "ran": True,
            "error": "; ".join(failed[:3]) if failed else None,
            "configured": done, "failures": failed}
