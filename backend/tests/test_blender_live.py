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
from backend.blender import blender_nl_mapping as mapping
from backend.blender import blender_script_templates as templates

ADDON = Path(__file__).resolve().parents[1] / "blender" / "addon" / "aria_live.py"


class FakeBlender:
    """Speaks the add-on's framing; replies as a finished script would."""

    def __init__(self, token="t0ken"):
        self.token, self.seen, self.paused, self.fail = token, [], False, False
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
                    reply = {"ok": True, "version": "5.0.1", "file": "C:/work/scene.blend",
                             "paused": self.paused, "jobs": 3}
                elif self.paused:
                    reply = {"ok": False, "error": "paused in Blender -- press Resume on the ARIA tab"}
                elif self.fail and not request.get("readonly"):
                    reply = {"ok": False, "output": "", "error": "RuntimeError: it broke half way"}
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
                             "paused": False, "jobs": 3,
                             "text": "Blender 5.0.1 is open and listening, editing C:/work/scene.blend."}


def test_a_paused_blender_says_so_and_refuses_work(fake):
    fake.paused = True
    assert live.status()["paused"] is True
    assert "PAUSED" in live.status()["text"]
    outcome = live.run("AddCube('Box')")
    assert not outcome["success"] and "Resume" in outcome["error"]


def test_the_addon_has_an_aria_tab_and_only_blender_can_resume():
    source = ADDON.read_text(encoding="utf-8")
    assert 'bl_category = "ARIA"' in source and 'bl_region_type = "UI"' in source
    # A paused add-on answers every job with a refusal -- nothing sent
    # over the socket reaches the code that could un-pause it.
    handle = source[source.index("def _handle"):source.index("def _run_jobs")]
    assert handle.index('if _state["paused"]') < handle.index("_jobs.put(job)")


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
# Chat -> the open Blender: "... in my Blender"
# ======================================================

@pytest.fixture
def chat(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(blender_actions, "output_dir", lambda: tmp_path / "out")
    return fake


def scripts(fake):
    return [r for r in fake.seen if "script" in r]


@pytest.mark.parametrize("said, live_one", [
    ("make a car in my Blender", True),
    ("rig him in my open Blender", True),
    ("show me my Blender scene in clay", True),
    ("in the open Blender, make a table", True),
    ("make a car in Blender", False),
    ("make the nose bigger in Blender", False),
])
def test_my_blender_means_the_open_one(said, live_one):
    assert mapping.names_live_blender(said) is live_one
    assert mapping.names_blender(said) or said.startswith("show me")


def test_a_build_in_my_blender_goes_beside_what_is_there_unsaved(chat):
    answer = blender_actions.answer_request("make a table in my Blender")
    assert answer["ran"] and "your open Blender" in answer["text"] and "Ctrl+Z" in answer["text"]
    [sent] = scripts(chat)
    assert "Before" not in sent["label"] and sent["readonly"] is False
    # The recipe's clear_scene is dropped, nothing is saved or exported,
    # and the picture is of what this step made -- not the whole room.
    assert "# --- step 1: clear_scene" not in sent["script"]
    assert "wm.save_as_mainfile" not in sent["script"]
    assert "export_scene" not in sent["script"]
    assert '_RESULT["created"] if _n in bpy.data.objects' in sent["script"]


def test_start_over_in_my_blender_is_refused_and_sends_nothing(chat):
    answer = blender_actions.answer_request("start over in my Blender")
    assert "did not clear your Blender" in answer["text"]
    assert not scripts(chat)


def test_undo_in_my_blender_is_one_ctrl_z_there(chat):
    answer = blender_actions.answer_request("undo in my Blender")
    assert "Undone in your Blender" in answer["text"]
    [sent] = scripts(chat)
    assert "bpy.ops.ed.undo()" in sent["script"] and "range(1)" in sent["script"]


def test_a_question_about_my_blender_does_nothing(chat):
    assert blender_actions.answer_request("how do I make a car in my Blender?") is None
    assert not scripts(chat)


def test_a_step_that_fails_in_my_blender_is_undone(chat):
    chat.fail = True
    answer = blender_actions.answer_request("make a table in my Blender")
    assert "undone in your Blender" in answer["text"]
    assert "bpy.ops.ed.undo()" in scripts(chat)[-1]["script"]


def test_the_addon_pushes_one_undo_step_per_job():
    # Two steps a job ("Before X" and "X") made every second Ctrl+Z land
    # on a copy of the first -- an undo that visibly did nothing.
    source = ADDON.read_text(encoding="utf-8")
    run_jobs = source[source.index("def _run_jobs"):source.index("def _note")]
    assert run_jobs.count("undo_push") == 1


# ======================================================
# A real, windowed Blender -- opt in
# ======================================================

@pytest.mark.skipif(os.environ.get("ARIA_TEST_LIVE_BLENDER") != "1",
                    reason="opens a Blender window; set ARIA_TEST_LIVE_BLENDER=1 (and "
                           "ARIA_TEST_BLENDER_EXE) to run")
def test_a_windowed_blender_builds_refuses_and_undoes(tmp_path, monkeypatch):
    # The suite hides the real plugin registry, so the Blender to open is
    # named here: ARIA_TEST_BLENDER_EXE=".../blender.exe".
    blender = os.environ.get("ARIA_TEST_BLENDER_EXE") or blender_actions.blender_path()
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
        assert live.run("AddCube('Box')")["success"]
        assert names() == sorted(before + ["Ball", "Box"])
        assert live.run("ClearScene()")["ran"] is False
        # One Ctrl+Z per job: the second undo must take the Ball, not
        # land on a copy of the scene the first one left.
        assert live.undo()["success"]
        assert names() == sorted(before + ["Ball"])
        assert live.undo()["success"]
        assert names() == before
    finally:
        proc.kill()
