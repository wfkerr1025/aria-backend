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
other), with an undo step pushed first, so Ctrl+Z takes back whatever
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
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "Runs in the background once enabled",
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
import traceback

import bpy

_jobs = queue.Queue()
_server = None
_thread = None
_state = {"port": 0, "token": "", "file": ""}


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
                             "file": bpy.data.filepath})
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
        # Undo steps only round a job that changes something: pushed round
        # a look as well, Ctrl+Z after ARIA had merely looked stepped
        # through copies of the same scene before reaching anything real.
        if not job["readonly"]:
            try:
                bpy.ops.ed.undo_push(message="Before " + job["label"])
            except Exception:
                pass                                 # no undo stack (background): run anyway
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
        job["done"].set()
    return 0.1


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


def unregister():
    global _server
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
