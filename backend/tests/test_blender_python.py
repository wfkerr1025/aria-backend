"""run_python behind its fence, and the catalogue of Blender's own API.

The fence is the test that matters: Python runs only when a caller
explicitly allows it, chat never does, and it is refused before Blender
starts -- not after half a script has run.
"""

from __future__ import annotations

import json

import pytest

from backend.blender import blender_actions
from backend.blender import blender_api
from backend.blender import blender_script_templates as templates
from backend.blender import blender_session as bs


@pytest.fixture(autouse=True)
def _output(tmp_path, monkeypatch):
    monkeypatch.setenv(blender_actions.ENV_OUTPUT, str(tmp_path / "out"))


def _never_start(monkeypatch):
    started = []
    monkeypatch.setattr(blender_actions.cli_runner, "run", lambda *a, **k: started.append(a))
    return started


def test_python_is_refused_unless_allowed(monkeypatch):
    started = _never_start(monkeypatch)
    result = blender_actions.run_actions([{"action": "run_python", "params": {"code": "x = 1"}}])
    assert result["ran"] is False and "explicitly" in result["error"]
    assert not started


def test_chat_never_runs_python(monkeypatch):
    started = _never_start(monkeypatch)
    from backend.blender import blender_typed_calls as tc

    answer = tc.answer_typed("RunPython(code='bpy.ops.mesh.primitive_cube_add()')")
    assert "did not run Blender" in answer["text"] and "never from chat" in answer["text"]
    assert not started


@pytest.mark.parametrize("code", ["import subprocess", "import shutil\nshutil.rmtree('x')",
                                  "__import__('os').system('x')", "import socket",
                                  "bpy.ops.wm.quit_blender()"])
def test_refused_words(code):
    with pytest.raises(templates.BadValue, match="refuses"):
        templates.build_script([{"action": "run_python", "params": {"code": code}}])


def test_python_that_does_not_parse_is_refused_before_blender():
    with pytest.raises(templates.BadValue, match="does not parse"):
        templates.build_script([{"action": "run_python", "params": {"code": "def broken(:"}}])


def test_the_code_is_carried_as_text_not_spliced():
    source = templates.build_script([{"action": "run_python", "params": {
        "code": "result['a'] = '\"); import os; (\"'"}}])
    compile(source, "x", "exec")


def test_the_command_line_flag_reaches_the_runner(monkeypatch):
    seen = []

    def run(actions, **kwargs):
        seen.append(kwargs.get("allow_python"))
        return {"ran": True, "success": True, "result": {"steps": [], "python": [{"a": 1}]}}

    monkeypatch.setattr(blender_actions, "run_actions", run)
    bs.main(["-s", "p", "run", "RunPython(code='result[\"a\"] = 1')", "--allow-python", "--no-preview"])
    bs.main(["-s", "p", "run", "RunPython(code='result[\"a\"] = 1')", "--no-preview"])
    assert seen == [True, False]


# ======================================================
# The catalogue, from a small stand-in
# ======================================================

@pytest.fixture
def catalogue(tmp_path, monkeypatch):
    folder = tmp_path / "out" / "API"
    folder.mkdir(parents=True)
    (folder / "blender_api_5.0.1.json").write_text(json.dumps({
        "version": "5.0.1",
        "ops": {"mesh.extrude_region": {"description": "Extrude region of faces", "properties": [
                    {"name": "use_normal_flip", "type": "BOOLEAN", "description": "", "default": False}]},
                "mesh.inset": {"description": "Inset new faces into selected faces", "properties": []},
                "object.delete": {"description": "Delete selected objects", "properties": []}},
        "types": {"Object": {"description": "Object data-block", "base": "ID", "properties": [
                    {"name": "location", "type": "FLOAT", "description": "Location of the object"}]}},
    }))
    monkeypatch.setattr(blender_api, "build", lambda: pytest.fail("should use the cache"))


def test_search_finds_by_name_first(catalogue):
    hits = blender_api.search("extrude")
    assert hits[0]["name"] == "mesh.extrude_region"


def test_search_needs_every_word(catalogue):
    assert [h["name"] for h in blender_api.search("inset faces")] == ["mesh.inset"]


def test_show_an_operator_and_a_type(catalogue):
    assert blender_api.show("mesh.extrude_region")["properties"][0]["name"] == "use_normal_flip"
    assert blender_api.show("types.Object")["base"] == "ID"
    assert blender_api.show("Object")["kind"] == "type"
    assert blender_api.show("mesh.nope") is None


def test_the_command_line_shows_a_signature(catalogue, capsys):
    assert blender_api.main(["show", "mesh.extrude_region"]) == 0
    out = capsys.readouterr().out
    assert "bpy.ops.mesh.extrude_region(...)" in out and "use_normal_flip: BOOLEAN = False" in out


# ======================================================
# The real Blender
# ======================================================

def _blender_or_skip():
    try:
        blender_actions.blender_path()
    except Exception:
        pytest.skip("Blender is not configured on this machine")


def test_allowed_python_runs_and_reports():
    _blender_or_skip()
    result = blender_actions.run_actions([
        {"action": "clear_scene"},
        {"action": "run_python", "params": {"code": (
            "bpy.ops.mesh.primitive_monkey_add()\n"
            "m = bpy.context.active_object\n"
            "m.name = 'Suzanne'\n"
            "result['faces'] = len(m.data.polygons)\n"
            "result['found'] = obj('Suzanne').name")}},
        {"action": "describe_scene"},
    ], allow_python=True, timeout=300)
    assert result["success"], result["output"][-2000:]
    assert result["result"]["python"] == [{"faces": 500, "found": "Suzanne"}]
    assert [o["name"] for o in result["result"]["scene"]["objects"]] == ["Suzanne"]


def test_the_real_catalogue_knows_blender_5():
    _blender_or_skip()
    path = blender_api.build()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"].startswith("5.")
    assert "mesh.extrude_region" in data["ops"] and "wm.obj_export" in data["ops"]
    assert len(data["ops"]) > 1500 and "Object" in data["types"]
