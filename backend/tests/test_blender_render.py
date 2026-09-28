"""Seeing the work, and working on one scene over many steps.

Two halves. The session tests need no Blender: run_actions is replaced,
and what is checked is what the session sends and what it keeps on
disk -- versions, undo, where pictures go, what chat is told.

The render tests at the bottom need the real thing, and skip without
it. They are the ones that matter most, because a preview that renders
a black square or leaves its camera in the saved file passes every
check that only reads the script.
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest

from backend.blender import blender_actions
from backend.blender import blender_script_templates as templates
from backend.blender import blender_session as bs


@pytest.fixture(autouse=True)
def _output(tmp_path, monkeypatch):
    monkeypatch.setenv(blender_actions.ENV_OUTPUT, str(tmp_path / "out"))


@pytest.fixture
def fake_blender(monkeypatch):
    """A run_actions that 'saves' the scene and 'renders' what it is asked to."""
    calls = []

    def run(actions, *, blend_file=None, **kwargs):
        calls.append({"actions": [dict(a) for a in actions], "blend_file": blend_file,
                      "kwargs": kwargs})
        renders = []
        for step in actions:
            params = step.get("params") or {}
            if step["action"] == "save_file":
                Path(params["path"]).parent.mkdir(parents=True, exist_ok=True)
                Path(params["path"]).write_text(f"scene after call {len(calls)}")
            if step["action"] in ("render_preview", "render_image"):
                renders.append(params["path"])
        return {"ran": True, "success": True, "error": None, "output": "",
                "result": {"created": ["Box"], "exported": [], "renders": renders,
                           "steps": []}}

    monkeypatch.setattr(blender_actions, "run_actions", run)
    return calls


@pytest.fixture
def failing_blender(monkeypatch):
    def run(actions, **kwargs):
        return {"ran": True, "success": False, "error": "no object called 'Nope'",
                "output": "Traceback ...\nRuntimeError: no object called 'Nope'",
                "result": None}

    monkeypatch.setattr(blender_actions, "run_actions", run)


# ======================================================
# Reading the work
# ======================================================

def test_typed_calls_json_and_lists_all_plan_the_same():
    typed = bs.plan('AddCube("Box", size=2)')
    as_json = bs.plan('[{"action": "add_cube", "params": {"name": "Box", "size": 2}}]')
    as_list = bs.plan([{"action": "add_cube", "params": {"name": "Box", "size": 2}}])
    assert typed == as_json == as_list


def test_a_sentence_is_not_calls():
    with pytest.raises(ValueError, match="one per line"):
        bs.plan("make me a nice chair")


def test_a_misspelt_json_key_is_refused_before_blender_starts(fake_blender):
    outcome = bs.Session("t").run('[{"action": "apply_bevel", "params": {"object": "Box", "ammount": 0.1}}]')
    assert outcome["success"] is False
    assert "ammount" in outcome["text"]
    assert fake_blender == []


def test_every_action_can_say_what_it_takes():
    for action in templates.known_actions():
        assert templates.describe_action(action).startswith(action + "(")


# ======================================================
# One scene, many steps
# ======================================================

def test_the_first_step_starts_from_nothing_and_saves(fake_blender):
    session = bs.Session("t")
    outcome = session.run('AddCube("Box")')

    assert outcome["success"] is True
    assert fake_blender[0]["blend_file"] is None
    assert session.scene.is_file()


def test_the_next_step_opens_what_the_last_one_saved(fake_blender):
    session = bs.Session("t")
    session.run('AddCube("Box")')
    session.run('ApplyBevel("Box", amount=0.02)')

    assert fake_blender[1]["blend_file"] == str(session.scene)


def test_the_scene_is_saved_before_it_is_looked_at(fake_blender):
    """A picture that fails must never cost the work it was a picture of."""
    bs.Session("t").run('AddCube("Box")')
    names = [a["action"] for a in fake_blender[0]["actions"]]
    assert names == ["add_cube", "save_file", "render_preview"]


def test_every_step_ends_with_a_picture_in_renders(fake_blender):
    session = bs.Session("t")
    outcome = session.run('AddCube("Box")', preview="clay")

    preview = fake_blender[0]["actions"][-1]["params"]
    assert preview["look"] == "clay"
    assert preview["skip_empty"] is True
    assert Path(preview["path"]).parent == session.renders
    assert outcome["renders"] == [preview["path"]]


def test_no_preview_means_no_picture(fake_blender):
    bs.Session("t").run('AddCube("Box")', preview=None)
    assert "render_preview" not in [a["action"] for a in fake_blender[0]["actions"]]


def test_asking_for_a_render_replaces_the_automatic_one(fake_blender):
    session = bs.Session("t")
    session.run('AddCube("Box")\nRenderPreview(look="clay")')
    names = [a["action"] for a in fake_blender[0]["actions"]]
    assert names.count("render_preview") == 1
    assert Path(fake_blender[0]["actions"][1]["params"]["path"]).parent == session.renders


def test_clearing_is_allowed_because_a_version_was_kept(fake_blender):
    session = bs.Session("t")
    session.run('AddCube("Box")')
    session.run('ClearScene()')
    assert fake_blender[1]["kwargs"]["allow_clearing_saved_file"] is True
    assert list(session.versions.glob("step_0002_before.blend"))


def test_undo_puts_back_the_version_before_the_last_step(fake_blender):
    session = bs.Session("t")
    session.run('AddCube("Box")')
    first = session.scene.read_text()
    session.run('ApplyBevel("Box")')
    assert session.scene.read_text() != first

    assert session.undo()["success"] is True
    assert session.scene.read_text() == first


def test_undoing_the_first_step_empties_the_scene_without_deleting_it(fake_blender):
    session = bs.Session("t")
    session.run('AddCube("Box")')
    session.undo()
    assert not session.scene.exists()
    assert list(session.versions.glob("undone_*.blend"))


def test_a_failed_step_leaves_the_scene_alone(fake_blender, failing_blender):
    session = bs.Session("t")
    session.folder.mkdir(parents=True)
    session.scene.write_text("good scene")

    outcome = session.run('ApplyBevel("Nope")')

    assert outcome["success"] is False
    assert "left as it was" in outcome["text"]
    assert session.scene.read_text() == "good scene"
    # Nothing changed, so there is nothing to undo back to.
    assert not list(session.versions.glob("*.blend"))


def test_every_step_is_in_the_history(fake_blender):
    session = bs.Session("t")
    session.run('AddCube("Box")')
    session.undo()
    entries = session.history()
    assert [e["step"] for e in entries] == [1, 2]
    assert entries[0]["actions"][0]["action"] == "add_cube"
    assert entries[1]["undo"] is True


def test_a_session_name_cannot_leave_its_folder():
    session = bs.Session("../../Windows")
    assert ".." not in session.folder.name
    assert session.folder.parent.name == "Sessions"


# ======================================================
# Chat
# ======================================================

def test_a_picture_is_a_local_image_the_chat_can_show():
    text = bs.picture_markdown([r"D:\Game Development\Blender\Sessions\chat\renders\step_0001.png"])
    assert text.startswith("![Preview](file:///D:/Game%20Development/Blender/")
    assert r"D:\Game Development" in text      # and a path a person can open


@pytest.mark.parametrize("said, kind", [
    ("undo that in Blender", "undo"),
    ("take it back in blender", "undo"),
    ("start over in Blender", "reset"),
    ("what's in the Blender scene?", "describe"),
    ("show me it in clay in Blender", "look"),
    ("render it from the side in Blender", "look"),
])
def test_things_said_about_the_scene_are_recognised(monkeypatch, said, kind):
    seen = []
    for name in ("undo", "reset", "describe"):
        monkeypatch.setattr(bs.Session, name, lambda self, n=name: seen.append(n) or {"success": True, "text": n})
    monkeypatch.setattr(bs.Session, "look",
                        lambda self, look, views=None, *a, **k: seen.append(("look", look, views))
                        or {"success": True, "renders": []})

    assert bs.answer_command(said) is not None
    assert (seen[0][0] if isinstance(seen[0], tuple) else seen[0]) == kind


def test_the_look_reads_clay_and_the_view_from_the_sentence(monkeypatch):
    seen = []
    monkeypatch.setattr(bs.Session, "look",
                        lambda self, look, views=None, *a, **k: seen.append((look, views))
                        or {"success": True, "renders": []})
    bs.answer_command("show me the front and side in clay in Blender")
    assert seen == [("clay", ["front", "right"])]


@pytest.mark.parametrize("said", [
    "how do I undo in Blender?",
    "can you tell me how to start over in Blender",
    "show me how to render in Blender",
])
def test_a_question_about_a_command_never_runs_it(monkeypatch, said):
    monkeypatch.setattr(bs.Session, "undo", lambda self: pytest.fail("undid"))
    monkeypatch.setattr(bs.Session, "reset", lambda self: pytest.fail("reset"))
    monkeypatch.setattr(bs.Session, "look", lambda self, *a, **k: pytest.fail("looked"))
    assert bs.answer_command(said) is None


def test_undo_needs_blender_named_the_strict_way(monkeypatch):
    monkeypatch.setattr(bs.Session, "undo", lambda self: pytest.fail("undid"))
    assert bs.answer_command("undo the Blender thing", gated=False) is None


def test_chat_answers_what_is_in_the_scene_without_in_blender(monkeypatch):
    monkeypatch.setattr(bs.Session, "describe",
                        lambda self: {"success": True, "text": "2 object(s)"})
    answer = blender_actions.answer_request("what's in the Blender scene?")
    assert answer["text"] == "2 object(s)"


def test_a_build_request_is_not_a_look():
    assert bs.answer_command("build a car in Blender", allow_look=False) is None
    assert bs.answer_command("the renderer is slow") is None


def test_chat_typed_calls_come_back_with_the_picture(fake_blender):
    from backend.blender import blender_typed_calls as tc

    answer = tc.answer_typed('AddCube("Box")')
    assert "![Preview](file:///" in answer["text"]
    assert "undo in Blender" in answer["text"]


def test_chat_typed_calls_build_on_the_last_message(fake_blender):
    from backend.blender import blender_typed_calls as tc

    tc.answer_typed('AddCube("Box")')
    tc.answer_typed('ApplyBevel("Box")')
    assert fake_blender[1]["blend_file"] is not None


# ======================================================
# The command line
# ======================================================

def test_the_command_line_lists_every_action(capsys):
    assert bs.main(["actions"]) == 0
    listed = capsys.readouterr().out
    for action in templates.known_actions():
        assert action + "(" in listed


def test_the_command_line_explains_one_action_by_either_name(capsys):
    bs.main(["actions", "RenderPreview"])
    assert "render_preview(" in capsys.readouterr().out


def test_the_command_line_runs_and_reports(fake_blender, capsys):
    assert bs.main(["-s", "cli", "run", 'AddCube("Box")', "--look", "clay"]) == 0
    out = capsys.readouterr().out
    assert "Step 1 done" in out and "Picture:" in out


# ======================================================
# The real Blender
# ======================================================

def _blender_or_skip():
    try:
        blender_actions.blender_path()
    except Exception:
        pytest.skip("Blender is not configured on this machine")


def _png_size(path: Path):
    with open(path, "rb") as file:
        header = file.read(24)
    return struct.unpack(">II", header[16:24])


def _png_is_not_one_colour(path: Path) -> bool:
    """True when the picture has something in it.

    Decoded by hand -- no imaging library in the test environment. Only
    the first filtered scanline bytes are compared, which is enough to
    tell a render from a flat fill.
    """
    data = path.read_bytes()
    chunks, index = [], 8
    while index < len(data):
        length = struct.unpack(">I", data[index:index + 4])[0]
        kind = data[index + 4:index + 8]
        if kind == b"IDAT":
            chunks.append(data[index + 8:index + 8 + length])
        index += 12 + length
    raw = zlib.decompress(b"".join(chunks))
    return len(set(raw[len(raw) // 3: len(raw) // 3 + 4000])) > 8


def test_a_real_preview_renders_every_view_and_a_sheet(tmp_path):
    _blender_or_skip()
    target = tmp_path / "look.png"
    result = blender_actions.run_actions([
        {"action": "clear_scene"},
        {"action": "add_sphere", "params": {"name": "Head", "radius": 0.5, "location": [0, 0, 1]}},
        {"action": "render_preview", "params": {"path": str(target), "look": "clay", "size": 128,
                                                "views": ["front", "right", "three_quarter"]}},
    ], timeout=300)

    assert result["success"], result["output"][-2000:]
    renders = result["result"]["renders"]
    assert renders[0] == str(target)
    assert len(renders) == 4
    for name in ("front", "right", "three_quarter"):
        assert (tmp_path / f"look_{name}.png").is_file()
    assert _png_size(tmp_path / "look_front.png") == (128, 128)
    assert _png_size(target) == (2 * 128 + 4, 2 * 128 + 4)
    assert _png_is_not_one_colour(tmp_path / "look_front.png")


@pytest.mark.parametrize("look", ["material", "final"])
def test_every_look_renders(tmp_path, look):
    """A sphere at three-quarter: a cube seen head-on is one flat face
    filling the frame, which is a correct render and a useless test.
    "final" is the scene as it stands, so it gets a lamp of its own."""
    _blender_or_skip()
    result = blender_actions.run_actions([
        {"action": "clear_scene"},
        {"action": "add_sphere", "params": {"name": "Ball"}},
        {"action": "add_light", "params": {"type": "SUN", "energy": 3}},
        {"action": "render_preview", "params": {"path": str(tmp_path / "l.png"), "look": look,
                                                "size": 96, "views": ["three_quarter"]}},
    ], timeout=300)
    assert result["success"], result["output"][-2000:]
    assert _png_is_not_one_colour(tmp_path / "l.png")


def test_a_preview_leaves_nothing_behind(tmp_path):
    """No camera, no lamps, the same engine -- the saved scene is the one given."""
    _blender_or_skip()
    result = blender_actions.run_actions([
        {"action": "clear_scene"},
        {"action": "add_cube", "params": {"name": "Box"}},
        {"action": "set_render", "params": {"engine": "CYCLES", "width": 333, "height": 222}},
        {"action": "render_preview", "params": {"path": str(tmp_path / "p.png"), "look": "material",
                                                "size": 64, "views": ["front"]}},
        {"action": "describe_scene"},
    ], timeout=300)

    assert result["success"], result["output"][-2000:]
    scene = result["result"]["scene"]
    assert [o["name"] for o in scene["objects"]] == ["Box"]
    assert scene["engine"] == "CYCLES"
    assert scene["resolution"] == [333, 222]
    assert scene["camera"] is None


def test_an_empty_scene_is_noted_not_failed_when_asked(tmp_path):
    _blender_or_skip()
    result = blender_actions.run_actions([
        {"action": "clear_scene"},
        {"action": "render_preview", "params": {"path": str(tmp_path / "e.png"), "skip_empty": True}},
    ], timeout=300)
    assert result["success"], result["output"][-2000:]
    assert result["result"]["renders"] == []


def test_a_render_needs_a_camera_or_says_so(tmp_path):
    _blender_or_skip()
    result = blender_actions.run_actions([
        {"action": "clear_scene"},
        {"action": "add_cube", "params": {"name": "Box"}},
        {"action": "render_image", "params": {"path": str(tmp_path / "r.png")}},
    ], timeout=300)
    assert not result["success"]
    assert "no camera" in result["output"]


def test_a_camera_shot_renders_through_that_camera(tmp_path):
    _blender_or_skip()
    result = blender_actions.run_actions([
        {"action": "clear_scene"},
        {"action": "add_cube", "params": {"name": "Box"}},
        {"action": "add_camera", "params": {"name": "Shot", "location": [4, -4, 3], "target": "Box"}},
        {"action": "render_image", "params": {"path": str(tmp_path / "r.png"), "engine": "WORKBENCH",
                                              "width": 160, "height": 90}},
    ], timeout=300)
    assert result["success"], result["output"][-2000:]
    assert _png_size(tmp_path / "r.png") == (160, 90)
    assert _png_is_not_one_colour(tmp_path / "r.png")


def test_a_real_session_builds_undoes_and_describes(tmp_path):
    _blender_or_skip()
    session = bs.Session("real", root=tmp_path)
    first = session.run('ClearScene()\nAddCube("Box")', preview="clay", size=96)
    assert first["success"], first.get("output_tail")
    assert Path(first["renders"][0]).is_file()

    second = session.run('ApplySubdivision("Box", levels=1)', preview=None)
    assert second["success"], second.get("output_tail")
    described = session.describe()
    box = next(o for o in described["scene"]["objects"] if o["name"] == "Box")
    assert box["modifiers"]

    session.undo()
    box = next(o for o in session.describe()["scene"]["objects"] if o["name"] == "Box")
    assert "modifiers" not in box
