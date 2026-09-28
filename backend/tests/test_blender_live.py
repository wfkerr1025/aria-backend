"""ARIA Live: working in the Blender that is open.

The protocol, the token and the guards are tested against a stand-in
server that speaks the add-on's framing, so they need no Blender. One
test drives a real, windowed Blender -- it opens a window, so it runs
only when ARIA_TEST_LIVE_BLENDER=1.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

import pytest

from backend.blender import blender_actions
from backend.blender import blender_live as live
from backend.blender import blender_script_templates as templates

ADDON = Path(__file__).resolve().parents[1] / "blender" / "addon" / "aria_live.py"


class FakeBlender:
    """Speaks the add-on's framing; replies as a finished script would."""

    def __init__(self, token="t0ken"):
        self.token, self.seen = token, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                size = struct.unpack(">I", conn.recv(4))[0]
                body = b""
                while len(body) < size:
                    body += conn.recv(size - len(body))
                request = json.loads(body)
                self.seen.append(request)
                if request.get("token") != self.token:
                    reply = {"ok": False, "error": "refused: wrong or missing token"}
                elif request.get("ping"):
                    reply = {"ok": True, "version": "5.0.1", "file": "C:/work/scene.blend"}
                else:
                    result = json.dumps({"created": ["Box"], "modified": [], "exported": [], "steps": []})
                    reply = {"ok": True, "output": f"{templates.RESULT_OPEN}\n{result}\n{templates.RESULT_CLOSE}\n"}
                data = json.dumps(reply).encode()
                conn.sendall(struct.pack(">I", len(data)) + data)

    def close(self):
        self.sock.close()


@pytest.fixture
def fake(tmp_path, monkeypatch):
    server = FakeBlender()
    info = tmp_path / "live.json"
    info.write_text(json.dumps({"port": server.port, "token": server.token}))
    monkeypatch.setenv("ARIA_BLENDER_LIVE_FILE", str(info))
    yield server
    server.close()


def test_status_reports_the_open_blender(fake):
    assert live.status() == {"live": True, "blender": "5.0.1", "file": "C:/work/scene.blend",
                             "text": "Blender 5.0.1 is open and listening, editing C:/work/scene.blend."}


def test_no_open_blender_says_how_to_get_one(tmp_path, monkeypatch):
    monkeypatch.setenv("ARIA_BLENDER_LIVE_FILE", str(tmp_path / "missing.json"))
    assert live.status()["live"] is False
    assert "install" in live.status()["text"]


def test_actions_arrive_as_a_template_script_with_the_token(fake):
    outcome = live.run("AddCube('Box')")
    assert outcome["success"] and outcome["result"]["created"] == ["Box"]
    request = fake.seen[-1]
    assert request["token"] == fake.token
    assert "_RESULT" in request["script"] and "primitive_cube_add" in request["script"]
    assert request["readonly"] is False


def test_looking_is_marked_read_only_so_it_leaves_no_undo_steps(fake):
    live.run("DescribeScene()")
    assert fake.seen[-1]["readonly"] is True


@pytest.mark.parametrize("calls, flag", [("ClearScene()", "allow_clearing"),
                                         ("SaveFile('C:/x.blend')", "allow_saving"),
                                         ("RunPython(code='x = 1')", "allow_python")])
def test_what_costs_the_users_work_is_refused_unless_named(fake, calls, flag):
    outcome = live.run(calls)
    assert outcome["ran"] is False and flag in outcome["error"]
    assert not [r for r in fake.seen if "script" in r]
    assert live.run(calls, **{flag: True})["success"]


def test_a_wrong_token_is_refused(fake, tmp_path, monkeypatch):
    info = tmp_path / "wrong.json"
    info.write_text(json.dumps({"port": fake.port, "token": "nope"}))
    monkeypatch.setenv("ARIA_BLENDER_LIVE_FILE", str(info))
    assert "token" in live.run("AddCube('Box')")["error"]


def test_install_copies_the_addon_into_blenders_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    path = live.install("5.0")
    assert path.read_text(encoding="utf-8") == ADDON.read_text(encoding="utf-8")
    assert path.parent == tmp_path / "Blender Foundation" / "Blender" / "5.0" / "scripts" / "addons"


def test_the_addon_only_listens_on_this_machine():
    source = ADDON.read_text(encoding="utf-8")
    assert '"127.0.0.1"' in source and "0.0.0.0" not in source
    assert "compare_digest" in source                          # the token, checked in constant time


# ======================================================
# A real, windowed Blender -- opt in
# ======================================================

@pytest.mark.skipif(os.environ.get("ARIA_TEST_LIVE_BLENDER") != "1",
                    reason="opens a Blender window; set ARIA_TEST_LIVE_BLENDER=1 to run")
def test_a_windowed_blender_builds_refuses_and_undoes(tmp_path, monkeypatch):
    blender = blender_actions.blender_path()
    info = tmp_path / "live.json"
    monkeypatch.setenv("ARIA_BLENDER_LIVE_FILE", str(info))
    start = tmp_path / "start.py"
    start.write_text(
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('aria_live', r'{ADDON}')\n"
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
        "sys.modules['aria_live'] = mod\nmod.register()\n", encoding="utf-8")
    proc = subprocess.Popen([str(blender), "--factory-startup", "--python", str(start)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ.copy())
    try:
        for _ in range(120):
            if info.exists():
                break
            time.sleep(0.5)
        time.sleep(1)
        names = lambda: sorted(o["name"] for o in live.run("DescribeScene()")["result"]["scene"]["objects"])
        before = names()
        assert live.run("AddSphere('Ball')")["success"]
        assert names() == sorted(before + ["Ball"])
        assert live.run("ClearScene()")["ran"] is False
        assert live.undo()["success"]
        assert names() == before
    finally:
        proc.kill()
