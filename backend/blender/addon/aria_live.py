"""ARIA Live -- lets ARIA work in this Blender while it is open.

Install: Edit > Preferences > Add-ons > Install from Disk... > this file,
then tick "ARIA Live". Or: python -m backend.blender.blender_live install

WHAT IT DOES
------------
Listens on 127.0.0.1 only, on a port it writes -- with a random token --
to a file in the user's home folder (~/.aria/blender_live.json, or
ARIA_BLENDER_LIVE_FILE). A request must carry that token. It carries a
script ARIA built from its action templates; the script is run on
Blender's main thread (Blender's data is not safe to touch from any
other), with one undo step pushed after it, so Ctrl+Z takes back whatever
ARIA did, the same as anything done by hand.

WHAT IT DOES NOT DO
-------------------
Take connections from other machines; run anything without the token;
run while a modal operator (a grab, a sculpt stroke) is in progress --
the request waits for the next quiet moment instead.
"""

bl_info = {
    "name": "ARIA Live",
    "author": "ARIA Lite",
    "version": (1, 2, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > ARIA",
    "description": "Lets ARIA build, sculpt and render in this Blender while it is open",
    "category": "System",
}

import contextlib
import io
import json
import os
import queue
import secrets
import socket
import struct
import threading
import time
import traceback

import bpy

_jobs = queue.Queue()
_server = None
_thread = None
_state = {"port": 0, "token": "", "file": "", "paused": False, "jobs": 0, "since": 0.0}
# The last few things ARIA did, newest first, for the ARIA tab:
# {"label", "ok", "time", "error"}.
_recent = []
RECENT_KEPT = 6


def _live_file():
    return os.environ.get("ARIA_BLENDER_LIVE_FILE") or os.path.join(
        os.path.expanduser("~"), ".aria", "blender_live.json")


def _read(conn):
    head = b""
    while len(head) < 4:
        chunk = conn.recv(4 - len(head))
        if not chunk:
            return None
        head += chunk
    size = struct.unpack(">I", head)[0]
    if size > 64 * 1024 * 1024:
        return None
    body = b""
    while len(body) < size:
        chunk = conn.recv(min(65536, size - len(body)))
        if not chunk:
            return None
        body += chunk
    return json.loads(body.decode("utf-8"))


def _send(conn, message):
    data = json.dumps(message).encode("utf-8")
    conn.sendall(struct.pack(">I", len(data)) + data)


def _serve(sock):
    while True:
        try:
            conn, _addr = sock.accept()
        except OSError:
            return                                   # the socket was closed: unregistered
        threading.Thread(target=_handle, args=(conn,), daemon=True).start()


def _handle(conn):
    with conn:
        try:
            request = _read(conn)
            if not request or not secrets.compare_digest(str(request.get("token", "")), _state["token"]):
                _send(conn, {"ok": False, "error": "refused: wrong or missing token"})
                return
            if request.get("ping"):
                _send(conn, {"ok": True, "version": bpy.app.version_string,
                             "file": bpy.data.filepath, "paused": _state["paused"],
                             "jobs": _state["jobs"]})
                return
            if _state["paused"]:
                # Paused from the ARIA tab. Only the person at Blender can
                # resume -- nothing sent here does, whatever it carries.
                _send(conn, {"ok": False, "error": "paused in Blender -- press Resume on the "
                                                   "ARIA tab (3D Viewport sidebar) to let ARIA work"})
                return
            done = threading.Event()
            job = {"script": str(request.get("script", "")), "done": done, "reply": None,
                   "label": str(request.get("label", "ARIA"))[:60],
                   "readonly": bool(request.get("readonly"))}
            _jobs.put(job)
            if not done.wait(float(request.get("timeout", 600))):
                _send(conn, {"ok": False, "error": "timed out waiting for Blender -- it may be "
                                                   "busy; the job will still run when it is free"})
                return
            _send(conn, job["reply"])
        except Exception as error:                   # a bad request must not stop the server
            try:
                _send(conn, {"ok": False, "error": repr(error)})
            except OSError:
                pass


def _run_jobs():
    """On the main thread, every tenth of a second: run what is waiting."""
    while not _jobs.empty():
        job = _jobs.get()
        out = io.StringIO()
        # One undo step per job that changes something, pushed AFTER it:
        # the top of the stack already is the scene as it was before (an
        # operator pushes its own step when it finishes). A "Before" step
        # as well made every job two steps, and the second Ctrl+Z landed
        # on a copy of the first -- "undo" that visibly did nothing.
        # Looks push nothing, for the same reason.
        try:
            with contextlib.redirect_stdout(out):
                exec(compile(job["script"], "<aria-live>", "exec"), {"__name__": "__aria_live__"})
            job["reply"] = {"ok": True, "output": out.getvalue()}
        except Exception:
            job["reply"] = {"ok": False, "output": out.getvalue(),
                            "error": traceback.format_exc()[-3000:]}
        if not job["readonly"]:
            try:
                bpy.ops.ed.undo_push(message=job["label"])
            except Exception:
                pass
        _note(job)
        job["done"].set()
    return 0.1


def _note(job):
    """Remember what ran, for the ARIA tab, and redraw it."""
    _state["jobs"] += 1
    error = job["reply"].get("error") if job["reply"] else None
    if error:
        # The last line, without "RuntimeError: " -- the sidebar is narrow,
        # and Blender cuts a long line in the middle, where the useful words were.
        error = error.strip().splitlines()[-1]
        error = error.split(": ", 1)[1] if error.split(": ", 1)[0].endswith(("Error", "Exception")) else error
    _recent.insert(0, {"label": job["label"].replace("ARIA: ", "", 1), "ok": bool(job["reply"]
                       and job["reply"].get("ok")), "time": time.strftime("%H:%M:%S"),
                       "error": (error or "").strip().splitlines()[-1][:120] if error else "",
                       "look": job["readonly"]})
    del _recent[RECENT_KEPT:]
    _redraw()


def _redraw():
    for window in getattr(bpy.context.window_manager, "windows", []):
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


# ======================================================
# The ARIA tab -- 3D Viewport sidebar (N)
# ======================================================

class ARIA_OT_live_pause(bpy.types.Operator):
    """Stop ARIA working in this Blender until Resume is pressed"""
    bl_idname = "aria.live_pause"
    bl_label = "Pause ARIA"

    def execute(self, context):
        _state["paused"] = not _state["paused"]
        _redraw()
        self.report({"INFO"}, "ARIA paused" if _state["paused"] else "ARIA resumed")
        return {"FINISHED"}


class VIEW3D_PT_aria_live(bpy.types.Panel):
    bl_label = "ARIA Live"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ARIA"

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        if _server is None:
            box.label(text="Not running", icon="ERROR")
        elif _state["paused"]:
            box.label(text="Paused -- ARIA cannot work here", icon="PAUSE")
        else:
            box.label(text="Listening for ARIA", icon="LINKED")
        if _server is not None:
            box.label(text="Local only, port %d" % _state["port"], icon="LOCKED")
        box.label(text="%d job%s since Blender opened" % (_state["jobs"], "" if _state["jobs"] == 1 else "s"))

        row = layout.row()
        row.scale_y = 1.3
        if _state["paused"]:
            row.operator("aria.live_pause", text="Resume ARIA", icon="PLAY")
        else:
            row.operator("aria.live_pause", text="Pause ARIA", icon="PAUSE")

        layout.label(text="Recent:")
        if not _recent:
            layout.label(text="  Nothing yet", icon="BLANK1")
        for item in _recent:
            icon = ("HIDE_OFF" if item["look"] else "CHECKMARK") if item["ok"] else "CANCEL"
            layout.label(text="%s  %s" % (item["time"], item["label"])[:48], icon=icon)
            if item["error"]:
                layout.label(text="  " + item["error"][:46], icon="BLANK1")
        layout.separator()
        layout.label(text="Ctrl+Z undoes ARIA's changes", icon="LOOP_BACK")


_CLASSES = (ARIA_OT_live_pause, VIEW3D_PT_aria_live)


def register():
    global _server, _thread
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", int(os.environ.get("ARIA_BLENDER_LIVE_PORT", "0"))))
    sock.listen(4)
    _server = sock
    _state.update(port=sock.getsockname()[1], token=secrets.token_hex(16), file=_live_file())
    os.makedirs(os.path.dirname(_state["file"]), exist_ok=True)
    with open(_state["file"], "w", encoding="utf-8") as handle:
        json.dump({"port": _state["port"], "token": _state["token"], "pid": os.getpid(),
                   "blender": bpy.app.version_string}, handle)
    try:
        os.chmod(_state["file"], 0o600)
    except OSError:
        pass
    _thread = threading.Thread(target=_serve, args=(sock,), daemon=True)
    _thread.start()
    bpy.app.timers.register(_run_jobs, first_interval=0.1, persistent=True)
    _state.update(paused=False, since=time.time())
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    global _server
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
    if bpy.app.timers.is_registered(_run_jobs):
        bpy.app.timers.unregister(_run_jobs)
    if _server is not None:
        _server.close()
        _server = None
    try:
        with open(_state["file"], encoding="utf-8") as handle:
            if json.load(handle).get("token") == _state["token"]:
                os.remove(_state["file"])
    except (OSError, ValueError):
        pass
