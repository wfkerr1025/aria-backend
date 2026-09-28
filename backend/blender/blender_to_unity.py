"""ARIA Lite - a rigged character from Blender into Unity, in one step.

    "send him to Unity"      (chat's scene)
    "send him to Unity in my Blender"
    "send the Miner to Unity in the Aria Test Project"

    python -m backend.blender.blender_to_unity -s <session> [--project <path>] [--name Miner]

WHAT HAPPENS
------------
1. The rig and every mesh under it are exported as one FBX -- all clips
   as separate takes -- into <project>/Assets/ARIA/Characters/<Name>/,
   with <Name>.aria.json beside it saying which clips loop.
2. ARIACharacterImport.cs (Assets/ARIA/Editor) is put into the project
   if it is missing or older. On import it makes the model a Humanoid,
   names and loops the clips, and builds an AnimatorController (Idle,
   Walk on Speed > 0.1) and a prefab. See that file.
3. Unity is asked to import it:
   - Unity CLOSED on that project: a batch run does it now, and takes a
     picture of every clip on the character, which comes back to chat.
   - Unity OPEN: the ARIA Editor Bridge is asked to refresh. Without a
     bridge answering, the files are there and Unity finishes the job
     the next time its window has focus -- and the reply says so.

The project is the one the Unity plugin points at, unless the sentence
names another beside it ("... in the Aria Test Project").
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from logger import get_logger

logger = get_logger(__name__)

__all__ = ["IMPORTER", "configured_project", "projects", "project_named", "project_is_open",
           "install_importer", "send", "main"]

REPO_ROOT = Path(__file__).resolve().parents[2]
IMPORTER = REPO_ROOT / "Assets" / "ARIA" / "Editor" / "ARIACharacterImport.cs"
IMPORTER_IN_PROJECT = Path("Assets", "ARIA", "Editor", "ARIACharacterImport.cs")
CHARACTERS = "Assets/ARIA/Characters"
IMPORT_METHOD = "ARIA.Characters.ARIACharacterImporter.ImportPending"
LOOPING = ("Walk", "Idle")
BATCH_TIMEOUT = 900       # a project's first import can take minutes


# ======================================================
# Which project
# ======================================================

def _plugin(name: str) -> dict:
    from backend.plugins import plugin_settings

    return plugin_settings.load_plugins().get(name) or {}


def _is_project(path: Path) -> bool:
    return (path / "Assets").is_dir() and (path / "ProjectSettings").is_dir()


def configured_project() -> Optional[Path]:
    """The Unity project the Unity plugin points at."""
    for plugin, field in (("unity", "project_path"), ("unity_cli", "unity_cli_project")):
        value = str(_plugin(plugin).get(field) or "").strip()
        if value and _is_project(Path(value)):
            return Path(value)
    return None


def projects() -> List[Path]:
    """The configured project and the projects beside it."""
    home = configured_project()
    if home is None:
        return []
    found = [p for p in sorted(home.parent.iterdir()) if p.is_dir() and _is_project(p)]
    return found or [home]


def project_named(said: str) -> Optional[Path]:
    """A project the sentence names by its folder name, if any."""
    flat = re.sub(r"\s+", " ", str(said or "")).lower()
    for project in sorted(projects(), key=lambda p: -len(p.name)):
        if project.name.lower() in flat:
            return project
    return None


def project_is_open(project: Path) -> bool:
    """Whether a Unity editor has this project open (it holds Temp/UnityLockfile)."""
    lock = Path(project) / "Temp" / "UnityLockfile"
    if not lock.exists():
        return False
    try:
        with open(lock, "a"):
            return False                  # a lock nobody holds: left by a crash
    except OSError:
        return True


def install_importer(project: Path) -> bool:
    """Put ARIACharacterImport.cs into the project. True when it changed."""
    target = Path(project) / IMPORTER_IN_PROJECT
    source = IMPORTER.read_bytes()
    if target.is_file() and target.read_bytes() == source:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source)
    return True


# ======================================================
# What to send
# ======================================================

def _clean_name(name: str) -> str:
    cleaned = re.sub(r"[^\w-]", "_", str(name or "")).strip("_")
    return cleaned or "Character"


def pick_rig(scene: dict, wanted: Optional[str] = None) -> tuple:
    """The armature to send and a name for the character -- or (None, reason)."""
    objects = (scene or {}).get("objects") or []
    rigs = [o for o in objects if o.get("type") == "ARMATURE"]
    meshes = [o for o in objects if o.get("type") == "MESH"]
    if wanted:
        named = [o for o in objects if o["name"].lower() == wanted.lower()]
        if named and named[0].get("type") == "MESH" and named[0].get("parent"):
            named = [o for o in rigs if o["name"] == named[0]["parent"]]
        rigs = [o for o in named if o.get("type") == "ARMATURE"] or rigs
    if not rigs:
        if meshes:
            return None, ("nothing in the scene is rigged yet -- say \"rig him\" first "
                          "(Unity needs a skeleton to make a Humanoid)")
        return None, "there is nothing in the scene to send"
    if len(rigs) > 1 and not wanted:
        return None, ("the scene has more than one skeleton (" + ", ".join(r["name"] for r in rigs)
                      + ") -- say which, e.g. \"send " + rigs[0]["name"] + " to Unity\"")
    rig = rigs[0]
    children = [m["name"] for m in meshes if m.get("parent") == rig["name"]]
    if not children:
        return None, f"{rig['name']} moves no mesh -- nothing would arrive in Unity but bones"
    base = children[0] if len(children) == 1 else rig["name"]
    return rig["name"], re.sub(r"_Rig$", "", base)


# ======================================================
# Unity's side
# ======================================================

def _unity_exe() -> Optional[Path]:
    value = str(_plugin("unity").get("unity_path") or "").strip()
    return Path(value) if value and Path(value).is_file() else None


def report_path(project: Path, name: str) -> Path:
    return Path(project) / "ARIA" / "characters" / f"{name}.json"


def _read_report(project: Path, name: str, since: float) -> Optional[dict]:
    path = report_path(project, name)
    if not path.is_file() or path.stat().st_mtime < since:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError:
        return None


def _batch_import(project: Path, name: str, since: float) -> dict:
    unity = _unity_exe()
    if unity is None:
        return {"done": False, "text": "no Unity editor is set on the Unity plugin page, so it "
                                       "will import the next time the project is opened"}
    log = Path(project) / "ARIA" / "characters" / "import.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    command = [str(unity), "-batchmode", "-projectPath", str(project),
               "-executeMethod", IMPORT_METHOD, "-quit", "-logFile", str(log)]
    try:
        subprocess.run(command, timeout=BATCH_TIMEOUT, capture_output=True)
    except subprocess.TimeoutExpired:
        return {"done": False, "text": f"Unity did not finish importing within {BATCH_TIMEOUT // 60} "
                                       f"minutes (log: {log})"}
    report = _read_report(project, name, since)
    if report is None:
        errors = [line for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
                  if "error CS" in line or "[ARIA]" in line][:8] if log.is_file() else []
        return {"done": False, "text": "Unity ran but did not build the character"
                + (":\n" + "\n".join(errors) if errors else f" (log: {log})")}
    return {"done": True, "report": report}


def _open_import(project: Path, name: str, since: float, wait: float) -> dict:
    try:
        sys.path.insert(0, str(REPO_ROOT)) if str(REPO_ROOT) not in sys.path else None
        from aria.unity_editor_bridge import UnityEditorBridge

        bridge = UnityEditorBridge(project, timeout=20)
        bridge.refresh_assets()
    except Exception as error:     # no bridge in the project, or it is not answering
        logger.info("bridge refresh not possible: %s", error)
        return {"done": False, "text": "Unity has the project open but did not answer, so it will "
                                       "import the character the next time you click into Unity"}
    deadline = time.time() + wait
    while time.time() < deadline:
        report = _read_report(project, name, since)
        if report is not None:
            return {"done": True, "report": report}
        time.sleep(1.0)
    return {"done": False, "text": "Unity is importing it -- the prefab appears when it finishes "
                                   "(new editor scripts compile first, which can take a minute)"}


# ======================================================
# The one step
# ======================================================

def send(session, *, project: Optional[Path] = None, rig: Optional[str] = None,
         name: Optional[str] = None, loop: Sequence[str] = LOOPING,
         preview_clips: Sequence[str] = (), wait: float = 120) -> dict:
    """Export the rigged character from `session` and set it up in Unity."""
    project = Path(project) if project else configured_project()
    if project is None or not _is_project(project):
        return {"success": False, "text": "No Unity project is set -- choose one on the Unity "
                                          "plugin page first."}
    scene = session.describe()
    if not scene.get("success"):
        return {"success": False, "text": scene.get("text", "Could not read the Blender scene.")}
    armature, named = pick_rig(scene.get("scene") or {}, rig)
    if armature is None:
        return {"success": False, "text": f"I did not send anything: {named}."}
    name = _clean_name(name or named)

    folder = Path(project) / CHARACTERS / name
    folder.mkdir(parents=True, exist_ok=True)
    fbx = folder / f"{name}.fbx"
    since = time.time()
    # The sidecar first: an editor that imports the FBX the moment it lands
    # must already find it, or the model imports as Generic.
    (folder / f"{name}.aria.json").write_text(json.dumps({
        "name": name, "loop": list(loop), "idle": "Idle", "walk": "Walk",
        "preview_clips": list(preview_clips)}, indent=2), encoding="utf-8")
    exported = session.run([{"action": "export_fbx", "params": {
        "objects": [armature], "path": str(fbx)}}], preview=None)
    if not exported.get("success"):
        return {"success": False, "text": "The export failed, so nothing reached Unity: "
                                          + str(exported.get("error"))}
    changed = install_importer(project)

    opened = project_is_open(project)
    outcome = (_open_import(project, name, since, wait) if opened
               else _batch_import(project, name, since))
    relative = f"{CHARACTERS}/{name}"
    result: Dict[str, Any] = {"success": True, "project": str(project), "name": name,
                              "fbx": str(fbx), "folder": relative, "imported": outcome["done"],
                              "installed_importer": changed}
    if not outcome["done"]:
        result["text"] = (f"Sent {name} to {project.name}: {relative}/{name}.fbx. "
                          f"{outcome['text'][0].upper()}{outcome['text'][1:]}.")
        return result
    report = outcome["report"]
    result["report"] = report
    result["pictures"] = [c["picture"] for c in report.get("clips") or [] if c.get("picture")]
    result["success"] = not report.get("error")
    result["text"] = describe(report, project)
    return result


def describe(report: dict, project: Path) -> str:
    """What Unity made of it, for chat."""
    if report.get("error"):
        return f"Unity imported {report.get('name')} but could not set it up: {report['error']}"
    clips = report.get("clips") or []
    own = [c for c in clips if c.get("name") in {"Walk", "Idle", "Wave", "Nod", "Jump"} or c.get("loop")]
    lines = [f"{report['name']} is in {Path(project).name}: prefab {report.get('prefab')}, "
             f"controller {report.get('controller')}."]
    if report.get("avatarHuman"):
        lines.append(f"Unity made it a Humanoid ({report.get('humanBones')} bones mapped), so "
                     f"clips from other Humanoids -- Mixamo's included -- play on it.")
    else:
        missing = ", ".join(report.get("missingBones") or [])
        lines.append("It is NOT a valid Humanoid" + (f" (missing {missing})" if missing else "")
                     + ", so only its own clips will play on it.")
    if clips:
        lines.append("Clips: " + ", ".join(f"{c['name']} ({c['length']:.1f} s"
                                           + (", loops" if c.get("loop") else "") + ")"
                                           for c in clips) + ".")
    if any(c.get("name") == "Idle" for c in own) and any(c.get("name") == "Walk" for c in own):
        lines.append("The controller starts on Idle and walks while its Speed parameter is above 0.1.")
    for warning in report.get("warnings") or []:
        lines.append(f"Note: {warning}.")
    return "\n\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    from backend.blender import blender_session

    parser = argparse.ArgumentParser(prog="python -m backend.blender.blender_to_unity",
                                     description="Send a rigged character from Blender into Unity.")
    parser.add_argument("--session", "-s", default=blender_session.DEFAULT_SESSION)
    parser.add_argument("--live", action="store_true", help="from the open Blender (ARIA Live)")
    parser.add_argument("--project", help="Unity project folder (default: the Unity plugin's)")
    parser.add_argument("--rig", help="the armature to send, when there is more than one")
    parser.add_argument("--name", help="what to call it in Unity")
    parser.add_argument("--preview-clip", action="append", default=[],
                        help="an Assets/ path to another clip to picture on it (batch runs)")
    args = parser.parse_args(argv)
    session = blender_session.LiveSession() if args.live else blender_session.Session(args.session)
    outcome = send(session, project=Path(args.project) if args.project else None, rig=args.rig,
                   name=args.name, preview_clips=args.preview_clip)
    print(outcome["text"])
    for picture in outcome.get("pictures") or []:
        print(f"- {picture}")
    return 0 if outcome.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
