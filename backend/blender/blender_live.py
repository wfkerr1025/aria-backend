"""ARIA Lite - working in the Blender that is open, not a background copy.

    python -m backend.blender.blender_live status
    python -m backend.blender.blender_live run "AddCube('Box')"
    python -m backend.blender.blender_live install     # copies the add-on

Everything else in this layer starts its own Blender in the background,
does the work in a file, and shows a picture. That is right for ARIA
building things on its own. This is for working together: the ARIA Live
add-on (backend/blender/addon/aria_live.py) runs inside the user's own
Blender, and the same actions arrive there -- the user watches them
happen in the viewport, and Ctrl+Z takes any of them back.

THE USER'S SCENE IS THEIRS
--------------------------
So the actions that would cost work are refused here unless asked for
by name: clear_scene (the whole open scene, gone), save_file (over
whatever it points at) and run_python (as everywhere). Undo exists, but
"it can be undone" is not a reason to do the damage.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import struct
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from backend.blender import blender_actions
from backend.blender import blender_script_templates as templates

__all__ = ["live_file", "status", "run", "undo", "install", "main"]

ADDON = Path(__file__).resolve().parent / "addon" / "aria_live.py"
GUARDED = {"clear_scene": "allow_clearing", "save_file": "allow_saving", "run_python": "allow_python"}


def live_file() -> Path:
    return Path(os.environ.get("ARIA_BLENDER_LIVE_FILE")
                or Path.home() / ".aria" / "blender_live.json")


def _connection() -> Optional[dict]:
    try:
        return json.loads(live_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _exchange(message: dict, timeout: float) -> dict:
    info = _connection()
    if info is None:
        return {"ok": False, "error": "no open Blender is running ARIA Live -- enable the add-on "
                                      "(python -m backend.blender.blender_live install)"}
    message = {**message, "token": info["token"]}
    data = json.dumps(message).encode("utf-8")
    try:
        with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=5) as conn:
            conn.settimeout(timeout + 10)
            conn.sendall(struct.pack(">I", len(data)) + data)
            head = b""
            while len(head) < 4:
                chunk = conn.recv(4 - len(head))
                if not chunk:
                    raise ConnectionError("Blender closed the connection")
                head += chunk
            size = struct.unpack(">I", head)[0]
            body = b""
            while len(body) < size:
                chunk = conn.recv(min(65536, size - len(body)))
                if not chunk:
                    raise ConnectionError("Blender closed the connection")
                body += chunk
    except (OSError, ConnectionError) as error:
        return {"ok": False, "error": f"could not reach the open Blender ({error}) -- is it still "
                                      f"running with ARIA Live enabled?"}
    return json.loads(body.decode("utf-8"))


def status() -> dict:
    """Whether an open Blender is listening, and which."""
    reply = _exchange({"ping": True}, timeout=5)
    if not reply.get("ok"):
        return {"live": False, "text": reply.get("error")}
    return {"live": True, "blender": reply.get("version"), "file": reply.get("file"),
            "text": f"Blender {reply.get('version')} is open and listening"
                    + (f", editing {reply['file']}" if reply.get("file") else " (unsaved file)") + "."}


def run(work: Union[str, Sequence[Dict[str, Any]]], *, allow_clearing: bool = False,
        allow_saving: bool = False, allow_python: bool = False, timeout: float = 600) -> dict:
    """Do the actions in the open Blender. The same result shape as run_actions."""
    from backend.blender import blender_session

    try:
        actions = blender_session.plan(work)
    except Exception as refused:
        return {"success": False, "ran": False, "error": str(refused), "result": None}
    allowed = {"allow_clearing": allow_clearing, "allow_saving": allow_saving,
               "allow_python": allow_python}
    blocked = [a["action"] for a in actions if a["action"] in GUARDED and not allowed[GUARDED[a["action"]]]]
    if blocked:
        return {"success": False, "ran": False, "result": None, "error": (
            f"refused in the open Blender: {', '.join(sorted(set(blocked)))}. These cost the "
            f"user's own work; pass " + ", ".join(sorted({GUARDED[b] for b in blocked})) +
            " if that is really wanted.")}
    try:
        script = templates.build_script(actions)
    except (templates.UnknownAction, templates.BadValue) as error:
        return {"success": False, "ran": False, "error": str(error), "result": None}
    label = "ARIA: " + ", ".join(a["action"] for a in actions)[:50]
    readonly = all(a["action"] in blender_session.READ_ONLY_ACTIONS for a in actions)
    reply = _exchange({"script": script, "label": label, "timeout": timeout,
                       "readonly": readonly}, timeout=timeout)
    output = reply.get("output") or ""
    parsed = blender_actions._read_result(output)
    ok = bool(reply.get("ok")) and parsed is not None
    return {"success": ok, "ran": "output" in reply, "result": parsed, "output": output,
            "error": None if ok else (reply.get("error") or "the script did not finish")}


def undo(steps: int = 1) -> dict:
    """Ctrl+Z in the open Blender, `steps` times -- ARIA's changes or anyone's."""
    script = "import bpy\nfor _ in range(%d):\n    bpy.ops.ed.undo()\n" % max(1, int(steps))
    reply = _exchange({"script": script, "label": "undo", "readonly": True, "timeout": 60}, timeout=60)
    return {"success": bool(reply.get("ok")), "error": reply.get("error")}


def blender_addons_dir(version: str = "5.0") -> Path:
    base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return base / "Blender Foundation" / "Blender" / version / "scripts" / "addons"


def install(version: str = "5.0") -> Path:
    """Copy the add-on into the user's Blender. Enabling it is theirs to do."""
    target = blender_addons_dir(version)
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ADDON, target / ADDON.name)
    return target / ADDON.name


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.blender.blender_live",
                                     description="Work in the Blender that is open (ARIA Live add-on).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    back = sub.add_parser("undo")
    back.add_argument("steps", nargs="?", type=int, default=1)
    go = sub.add_parser("run")
    go.add_argument("work", nargs="?")
    go.add_argument("--file", "-f")
    go.add_argument("--allow-clearing", action="store_true")
    go.add_argument("--allow-saving", action="store_true")
    go.add_argument("--allow-python", action="store_true")
    put = sub.add_parser("install")
    put.add_argument("--blender", default="5.0")
    args = parser.parse_args(argv)

    if args.command == "status":
        print(status()["text"])
        return 0
    if args.command == "undo":
        outcome = undo(args.steps)
        print("Undone in the open Blender." if outcome["success"] else f"Not undone: {outcome['error']}")
        return 0 if outcome["success"] else 1
    if args.command == "install":
        path = install(args.blender)
        print(f"Copied ARIA Live to {path}.\nIn Blender: Edit > Preferences > Add-ons, search "
              f"\"ARIA Live\" and tick it.")
        return 0
    work = Path(args.file).read_text(encoding="utf-8") if args.file else (args.work or sys.stdin.read())
    outcome = run(work, allow_clearing=args.allow_clearing, allow_saving=args.allow_saving,
                  allow_python=args.allow_python)
    if outcome["success"]:
        made = (outcome["result"] or {}).get("created") or []
        print("Done in the open Blender" + (f": made {', '.join(made)}" if made else "") +
              ". Ctrl+Z in Blender takes it back.")
        return 0
    print(f"Not done: {outcome['error']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
