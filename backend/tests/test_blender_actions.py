"""Driving Blender without letting a model write the Python.

THE FAILURE THIS EXISTS FOR
---------------------------
Measured on the developer's machine. Typed into chat:

    blender --background --python <script>

ARIA routed it to phi-3-mini, which answered with:

    def main:
        bpy.ops.preferences.addonSettings(module="Blender3D", reset=True)

`def main:` is not valid Python and that operator does not exist. A
model asked to drive a program it cannot run does not decline; it
writes what such a request usually produces.

So the Python is generated from templates, and the only thing a caller
-- a person, a mapper, or a model -- gets to choose is which action and
what numbers. The tests below are mostly about that boundary holding.

WHAT IS AND IS NOT MEASURED HERE
--------------------------------
These run without Blender installed, so they prove the generated
source is valid Python, that the boundary holds, and that a failure is
reported as one. They cannot prove an operator exists in Blender 5.0.1
-- that was measured separately by running all 39 actions against the
real 5.0.1 and reading the results, and is why `test_every_template_is_
valid_python` compiles rather than merely matching strings.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from backend.blender import blender_actions as actions
from backend.blender import blender_asset_pipeline as pipeline
from backend.blender import blender_nl_mapping as mapping
from backend.blender import blender_script_templates as templates


# ======================================================
# Fixtures -- a fake Blender that answers like the real one
# ======================================================

# Prints the sentinel block the runner looks for, so the parsing path
# is exercised without a 300MB install. The real Blender was measured
# separately; this is about what ARIA does with the answer.
_RESULT = json.dumps({"steps": [{"step": "add_cube", "object": "Cube"}],
                      "created": ["Cube"], "exported": []})

_WINDOWS = f"""@echo off
echo Blender 5.0.1
echo {templates.RESULT_OPEN}
echo {_RESULT.replace('"', '^"')}
echo {templates.RESULT_CLOSE}
exit /b 0
"""

_POSIX = f"""#!/bin/sh
echo "Blender 5.0.1"
echo '{templates.RESULT_OPEN}'
echo '{_RESULT}'
echo '{templates.RESULT_CLOSE}'
exit 0
"""

# A Blender that starts, prints a traceback and exits 0 -- which the
# real one does when a script raises without --python-exit-code taking
# effect. No result block means the script did not finish.
_WINDOWS_BROKEN = """@echo off
echo Blender 5.0.1
echo Traceback (most recent call last): 1>&2
exit /b 0
"""

_POSIX_BROKEN = """#!/bin/sh
echo "Blender 5.0.1"
echo "Traceback (most recent call last):" >&2
exit 0
"""


def _make_program(folder: Path, name: str, windows: str, posix: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        path = folder / f"{name}.cmd"
        path.write_text(windows.replace("\n", "\r\n"), encoding="utf-8")
    else:
        path = folder / name
        path.write_text(posix, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def blender(tmp_path, monkeypatch):
    path = _make_program(tmp_path / "bin", "blender", _WINDOWS, _POSIX)
    monkeypatch.setenv(actions.ENV_BLENDER, str(path))
    return path


@pytest.fixture
def broken_blender(tmp_path, monkeypatch):
    path = _make_program(tmp_path / "broken", "blender",
                         _WINDOWS_BROKEN, _POSIX_BROKEN)
    monkeypatch.setenv(actions.ENV_BLENDER, str(path))
    return path


@pytest.fixture
def no_blender(monkeypatch, tmp_path):
    """Nothing configured anywhere -- the state on a fresh machine."""
    monkeypatch.delenv(actions.ENV_BLENDER, raising=False)
    from backend.plugins import plugin_settings
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE,
                       str(tmp_path / "empty.json"))


# Representative parameters for every template. Doubles as the record
# of what each action takes -- if a template grows a placeholder and
# this table is not updated, the compile test fails rather than the
# omission being discovered in Blender.
EVERY_ACTION = [
    ("clear_scene", {}),
    ("add_cube", {"name": "Cube", "size": 2, "location": [0, 0, 0]}),
    ("add_sphere", {"name": "Ball", "radius": 1, "location": [0, 0, 1]}),
    ("add_cylinder", {"name": "Pipe", "radius": 0.5, "depth": 2,
                      "location": [0, 0, 0]}),
    ("add_plane", {"name": "Floor", "size": 10, "location": [0, 0, 0]}),
    ("add_torus", {"name": "Ring", "major_radius": 1, "minor_radius": 0.25,
                   "location": [0, 0, 0]}),
    ("apply_subdivision", {"object": "Cube", "levels": 2, "apply": False}),
    ("apply_bevel", {"object": "Cube", "amount": 0.02, "segments": 2,
                     "apply": False}),
    ("apply_mirror", {"object": "Cube", "axis": "X", "apply": False}),
    ("apply_array", {"object": "Cube", "count": 3, "offset": [1, 0, 0],
                     "apply": False}),
    ("apply_boolean", {"object": "Cube", "target": "Ball",
                       "operation": "DIFFERENCE", "apply": False}),
    ("apply_solidify", {"object": "Cube", "thickness": 0.05, "apply": False}),
    ("apply_decimate", {"object": "Cube", "ratio": 0.5, "apply": True}),
    ("apply_multires", {"object": "Cube", "levels": 2}),
    ("move", {"object": "Cube", "x": 1, "y": 2, "z": 3}),
    ("rotate", {"object": "Cube", "x": 90, "y": 0, "z": 0}),
    ("scale", {"object": "Cube", "x": 2, "y": 2, "z": 2}),
    ("parent", {"child": "Ball", "parent": "Cube"}),
    ("smart_uv_project", {"object": "Cube", "angle_limit": 1.15,
                          "margin": 0.02}),
    ("mark_seams", {"object": "Cube", "sharpness": 0.9}),
    ("unwrap", {"object": "Cube", "margin": 0.02}),
    ("create_material", {"name": "Paint", "color": [0.5, 0.1, 0.1],
                         "metallic": 0.8, "roughness": 0.3}),
    ("assign_material", {"object": "Cube", "material": "Paint"}),
    ("create_armature", {"name": "Rig", "location": [0, 0, 0]}),
    ("add_bone", {"armature": "Rig", "name": "Spine", "head": [0, 0, 0],
                  "tail": [0, 0, 1]}),
    ("parent_mesh_to_armature", {"mesh": "Cube", "armature": "Rig"}),
    ("auto_weights", {"mesh": "Cube", "armature": "Rig"}),
    ("normalize_weights", {"mesh": "Cube"}),
    ("assign_vertex_group", {"mesh": "Cube", "bone": "Spine",
                             "vertices": [0, 1, 2], "weight": 1.0}),
    ("insert_keyframe", {"object": "Cube", "property": "location", "frame": 1}),
    ("set_pose", {"armature": "Rig", "bone": "Spine", "rotation": [10, 0, 0],
                  "frame": 5}),
    ("set_frame", {"frame": 12}),
    ("bake_animation", {"object": "Rig", "start": 1, "end": 60}),
    ("sculpt_brush", {"object": "Cube", "brush_type": "DRAW", "strength": 0.5,
                      "detail_size": 12.0}),
    ("enable_dyntopo", {"object": "Cube"}),
    ("export_fbx", {"path": "C:/tmp/a.fbx", "object": None}),
    ("export_glb", {"path": "C:/tmp/a.glb", "object": None}),
    ("export_obj", {"path": "C:/tmp/a.obj", "object": None}),
    ("save_file", {"path": "C:/tmp/a.blend"}),

    # The cleanup stage. Exercised properly in test_blender_cleanup.py;
    # listed here so the coverage guard above stays honest.
    ("import_model", {"path": "C:/tmp/a.glb"}),
    ("measure_mesh", {}),
    ("remove_loose", {}),
    ("recalculate_normals", {}),
    ("merge_by_distance", {"distance": 0.0001}),
    ("scale_to_height", {"height": 1.8}),
    ("origin_to_floor", {}),
]


# ======================================================
# The generated script
# ======================================================

def test_the_table_covers_every_template():
    """A new action with no test is an untested action."""
    assert sorted(name for name, _ in EVERY_ACTION) == sorted(templates.TEMPLATES)


@pytest.mark.parametrize("name,params", EVERY_ACTION, ids=[n for n, _ in EVERY_ACTION])
def test_every_template_is_valid_python(name, params):
    """The failure that started this was `def main:` -- source that
    does not parse. Compiling is the cheapest way to be sure."""
    source = templates.build_script([{"action": name, "params": params}])

    compile(source, f"<{name}>", "exec")


def test_a_whole_run_is_one_script():
    """Twenty actions are one Blender start and one scene, not twenty
    of each -- an object made by step 1 has to exist at step 20."""
    source = templates.build_script([
        {"action": "add_cube", "params": {"name": "A"}},
        {"action": "move", "params": {"object": "A", "x": 1}},
    ])

    compile(source, "<run>", "exec")
    assert source.count(templates.RESULT_OPEN) == 1


def test_an_unknown_action_is_refused_before_anything_runs():
    with pytest.raises(templates.UnknownAction) as raised:
        templates.build_script([{"action": "add_cube"},
                                {"action": "delete_everything"}])

    assert "delete_everything" in str(raised.value)
    # The step number matters: a person needs to know which one.
    assert "2" in str(raised.value)


# ======================================================
# The boundary: values are values, never code
# ======================================================

_HOSTILE = [
    'x"); import os; os.system("calc"); _note("',
    "x'); __import__('shutil').rmtree('/'); _note('",
    'name\\"; bpy.ops.wm.quit_blender(); "',
    "line1\nimport os\nos.system('calc')",
    '"""\nimport os\n"""',
    "\\'; import os; #",
]


@pytest.mark.parametrize("hostile", _HOSTILE)
def test_a_name_cannot_become_code(hostile):
    """Object names come from a sentence, and a sentence is not to be
    trusted. Text goes through repr(), so the worst a name can do is
    be a strange name."""
    source = templates.build_script(
        [{"action": "add_cube", "params": {"name": hostile}}])

    compile(source, "<hostile>", "exec")
    for smell in ("import os", "os.system", "rmtree", "quit_blender"):
        # It may appear inside the quoted literal; it must never appear
        # as a statement of its own.
        for line in source.splitlines():
            assert not line.strip().startswith(smell)


@pytest.mark.parametrize("hostile", _HOSTILE)
def test_a_path_cannot_become_code(hostile):
    source = templates.build_script(
        [{"action": "export_fbx", "params": {"path": hostile}}])

    compile(source, "<hostile>", "exec")


def test_a_number_that_is_not_a_number_is_refused():
    """A numeric slot takes a number. "0); import os; (" is not one.

    An earlier version substituted the default and said nothing, which
    is safe -- it cannot become code either way -- but silent. A caller
    who asked for something impossible should hear about it, and
    hearing about it BEFORE Blender starts means no half-built scene.
    """
    with pytest.raises(templates.BadValue):
        templates.build_script([{"action": "add_cube",
                                 "params": {"size": "0); import os; ("}}])


def test_an_absent_number_is_not_a_bad_one():
    """Every optional parameter works by being left out, so None and
    "" have to keep taking the default."""
    for params in ({}, {"size": None}, {"size": ""}):
        source = templates.build_script([{"action": "add_cube", "params": params}])

        compile(source, "<default>", "exec")
        assert "size=2.0" in source


def test_a_choice_outside_the_allowlist_is_refused():
    """Modifier types, boolean operations, brushes and axes are fixed
    lists. A caller cannot invent a new one -- and silently performing
    DIFFERENCE for a requested DESTROY would quietly ruin a model."""
    with pytest.raises(templates.BadValue):
        templates.build_script([{"action": "apply_boolean",
                                 "params": {"object": "A", "target": "B",
                                            "operation": "DESTROY"}}])


def test_a_bad_value_never_starts_blender(blender, monkeypatch):
    """Refusing at build time is only worth anything if the runner
    reports it as a refusal instead of crashing the turn."""
    started = []
    from backend.core import cli_runner
    monkeypatch.setattr(cli_runner, "run", lambda *a, **k: started.append(a) or {})

    result = actions.run_actions([{"action": "add_cube", "params": {"size": "huge"}}])

    assert started == []
    assert result["ran"] is False
    assert "not a number" in result["error"]


def test_a_keyframe_path_is_not_silently_replaced():
    """Measured: _choice upper-cased the candidate before comparing,
    but KEYFRAME_PATHS holds attribute names in lower case. Nothing
    ever matched, so every request silently became the default and
    `insert_keyframe` keyed a position when asked to key a rotation.

    It was invisible for exactly as long as the fallback was silent.
    """
    for asked in ("rotation_euler", "ROTATION_EULER", "Rotation_Euler"):
        source = templates.build_script(
            [{"action": "insert_keyframe",
              "params": {"object": "Cube", "property": asked, "frame": 3}}])

        assert "data_path='rotation_euler'" in source, asked


def test_an_allowlist_keeps_its_own_casing():
    """The lists are not all the same shape: Blender enums shout
    (SUBSURF, DRAW) and attribute names do not (location)."""
    shouting = templates.build_script(
        [{"action": "sculpt_brush", "params": {"object": "C", "brush_type": "draw"}}])
    quiet = templates.build_script(
        [{"action": "insert_keyframe", "params": {"object": "C", "property": "SCALE"}}])

    assert "'DRAW'" in shouting
    assert "'scale'" in quiet


def test_the_allowlists_are_not_empty():
    """A guard over an empty list would allow everything."""
    assert templates.MODIFIER_TYPES
    assert templates.BOOLEAN_OPERATIONS
    assert templates.SCULPT_BRUSHES
    assert templates.AXES


# ======================================================
# Running it
# ======================================================

def test_it_runs_and_reads_the_result(blender):
    result = actions.run_actions([{"action": "add_cube", "params": {"name": "Cube"}}])

    assert result["success"] is True
    assert result["ran"] is True
    assert result["result"]["created"] == ["Cube"]


def test_the_temp_script_is_cleaned_up(blender, tmp_path):
    before = set(Path(os.environ.get("TEMP", "/tmp")).glob("aria-blender-*.py"))

    result = actions.run_actions([{"action": "add_cube"}])
    after = set(Path(os.environ.get("TEMP", "/tmp")).glob("aria-blender-*.py"))

    assert result["script"] is None
    assert after <= before


def test_a_gui_is_never_opened(blender, monkeypatch):
    """Blender opens a window unless told otherwise, and a window
    nobody can see is a process that never exits."""
    seen = {}
    from backend.core import cli_runner
    real = cli_runner.run

    def watched(executable, arguments, **kwargs):
        seen["argv"] = list(arguments)
        return real(executable, arguments, **kwargs)

    monkeypatch.setattr(cli_runner, "run", watched)
    actions.run_actions([{"action": "add_cube"}])

    assert "--background" in seen["argv"]


def test_the_user_addons_cannot_change_the_result(blender, monkeypatch):
    """Two machines running the same actions should get the same
    answer, and somebody's auto-loading add-on is exactly the kind of
    difference that is impossible to debug later."""
    seen = {}
    from backend.core import cli_runner
    real = cli_runner.run

    def watched(executable, arguments, **kwargs):
        seen["argv"] = list(arguments)
        return real(executable, arguments, **kwargs)

    monkeypatch.setattr(cli_runner, "run", watched)
    actions.run_actions([{"action": "add_cube"}])

    assert "--factory-startup" in seen["argv"]


def test_a_script_that_did_not_finish_is_not_a_success(broken_blender):
    """Blender can exit 0 having printed a traceback, so the result
    block is the witness, not the exit code."""
    result = actions.run_actions([{"action": "add_cube"}])

    assert result["success"] is False
    assert result["ran"] is True
    assert "did not complete" in result["error"]


def test_an_unknown_action_never_starts_blender(blender, monkeypatch):
    started = []
    from backend.core import cli_runner
    monkeypatch.setattr(cli_runner, "run",
                        lambda *a, **k: started.append(a) or {})

    result = actions.run_actions([{"action": "teleport"}])

    assert started == []
    assert result["ran"] is False
    assert "teleport" in result["error"]


def test_no_blender_configured_says_so(no_blender):
    result = actions.run_actions([{"action": "add_cube"}])

    assert result["ran"] is False
    assert result["success"] is False
    assert result["error"]


def test_a_refusal_never_claims_to_have_run(no_blender):
    result = actions.run_actions([{"action": "add_cube"}])

    assert result["result"] is None


def test_a_configured_path_that_is_gone_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv(actions.ENV_BLENDER, str(tmp_path / "vanished.exe"))

    result = actions.run_actions([{"action": "add_cube"}])

    assert result["ran"] is False
    assert "not a file" in result["error"]


def test_the_single_action_helpers_reach_blender(blender):
    """The named functions are the surface the mapper and a person
    both use; they must not have drifted from the template names."""
    for call in (lambda: actions.add_cube(size=2),
                 lambda: actions.apply_bevel("Cube", amount=0.02),
                 lambda: actions.rotate("Cube", x=90),
                 lambda: actions.create_material("Paint"),
                 lambda: actions.export_fbx("C:/tmp/a.fbx")):
        result = call()
        assert result["ran"] is True, result
        assert result["success"] is True, result


def test_every_helper_names_a_real_template():
    """A helper calling a template that does not exist would fail only
    when somebody used that one feature."""
    import inspect

    helpers = [name for name in actions.__all__
               if callable(getattr(actions, name, None))
               and not name.startswith("_")]
    missing = []
    for name in helpers:
        source = inspect.getsource(getattr(actions, name))
        for line in source.splitlines():
            if "_one(" in line:
                called = line.split('_one("')[1].split('"')[0]
                if called not in templates.TEMPLATES:
                    missing.append((name, called))

    assert missing == []


# ======================================================
# Not emptying somebody's file
#
# A recipe opens with clear_scene, which is right when a run starts
# from an empty Blender -- the only things it removes are the default
# cube, camera and light. It is not right when a caller opened an
# existing .blend and the run saves back over it: Blender loads their
# file, the script empties it, and save_file writes the empty result
# on top. There is nothing to undo.
# ======================================================

@pytest.fixture
def existing_blend(tmp_path):
    """A .blend that already has somebody's work in it."""
    path = tmp_path / "MyWork.blend"
    path.write_bytes(b"BLENDER-v500" + b"\x00" * 512)
    return path


def _watch_runner(monkeypatch):
    """Record whether Blender was started at all."""
    started = []
    from backend.core import cli_runner
    monkeypatch.setattr(cli_runner, "run",
                        lambda *a, **k: started.append(a) or
                        {"success": True, "output": "", "error": None})
    return started


def test_clearing_and_saving_over_the_opened_file_is_refused(blender,
                                                             existing_blend):
    result = actions.run_actions(
        [{"action": "clear_scene"},
         {"action": "add_cube", "params": {"name": "Car"}},
         {"action": "save_file", "params": {"path": str(existing_blend)}}],
        blend_file=str(existing_blend))

    assert result["ran"] is False
    assert result["success"] is False
    assert "delete everything in it" in result["error"]


def test_the_refusal_happens_before_blender_starts(blender, existing_blend,
                                                   monkeypatch):
    """A guard that fires after the process has opened the file is not
    a guard."""
    started = _watch_runner(monkeypatch)

    actions.run_actions(
        [{"action": "clear_scene"},
         {"action": "save_file", "params": {"path": str(existing_blend)}}],
        blend_file=str(existing_blend))

    assert started == []


def test_the_same_file_under_another_name_is_still_caught(blender,
                                                          existing_blend):
    """"MyWork.blend" and "sub/../MyWork.blend" are the same file, and
    a guard that only compared strings would wave one through."""
    detour = str(existing_blend.parent / "sub" / ".." / existing_blend.name)

    result = actions.run_actions(
        [{"action": "clear_scene"},
         {"action": "save_file", "params": {"path": detour}}],
        blend_file=str(existing_blend))

    assert result["ran"] is False


def test_a_refusal_says_what_would_have_happened(blender, existing_blend):
    """A person who is told "no" needs to know what was about to be
    destroyed and how to proceed anyway."""
    result = actions.run_actions(
        [{"action": "clear_scene"},
         {"action": "save_file", "params": {"path": str(existing_blend)}}],
        blend_file=str(existing_blend))

    assert existing_blend.name in result["error"]
    assert "Nothing ran" in result["error"]
    assert "allow_clearing_saved_file" in result["error"]


def test_opening_and_clearing_without_saving_back_is_allowed(blender,
                                                             existing_blend,
                                                             tmp_path):
    """Nothing reaches the disk, so there is nothing to protect. A
    guard that refused this would be a false alarm."""
    result = actions.run_actions(
        [{"action": "clear_scene"},
         {"action": "add_cube", "params": {"name": "Car"}},
         {"action": "export_glb", "params": {"path": str(tmp_path / "car.glb")}}],
        blend_file=str(existing_blend))

    assert result["ran"] is True


def test_saving_to_a_different_file_is_allowed(blender, existing_blend, tmp_path):
    result = actions.run_actions(
        [{"action": "clear_scene"},
         {"action": "save_file", "params": {"path": str(tmp_path / "Other.blend")}}],
        blend_file=str(existing_blend))

    assert result["ran"] is True


def test_modifying_and_saving_without_clearing_is_allowed(blender,
                                                          existing_blend):
    """Open, add, save back is the ordinary way to edit a file."""
    result = actions.run_actions(
        [{"action": "add_cube", "params": {"name": "Extra"}},
         {"action": "save_file", "params": {"path": str(existing_blend)}}],
        blend_file=str(existing_blend))

    assert result["ran"] is True


def test_an_ordinary_run_is_never_guarded(blender, tmp_path):
    """Every recipe starts with clear_scene. Without a blend_file
    there is no file to lose, and the guard must not touch the path
    that is used for almost everything."""
    result = actions.run_actions(
        [{"action": "clear_scene"},
         {"action": "add_cube", "params": {"name": "Car"}},
         {"action": "save_file", "params": {"path": str(tmp_path / "new.blend")}}])

    assert result["ran"] is True


def test_a_blend_file_that_does_not_exist_yet_is_not_guarded(blender, tmp_path):
    """There is no work to destroy in a file that is not there."""
    result = actions.run_actions(
        [{"action": "clear_scene"},
         {"action": "save_file", "params": {"path": str(tmp_path / "new.blend")}}],
        blend_file=str(tmp_path / "new.blend"))

    assert result["ran"] is True


def test_saying_you_meant_it_lets_it_through(blender, existing_blend):
    result = actions.run_actions(
        [{"action": "clear_scene"},
         {"action": "save_file", "params": {"path": str(existing_blend)}}],
        blend_file=str(existing_blend), allow_clearing_saved_file=True)

    assert result["ran"] is True


def test_the_natural_language_path_cannot_reach_this(blender):
    """The guard is for programmatic callers. map_text never emits
    save_file, so no sentence can construct the destructive run --
    which is worth pinning, because it is the reason the hazard is
    narrow rather than urgent."""
    emitted = set()
    for sentence in ("make a car in Blender", "save it in Blender",
                     "in Blender save the file",
                     "make a house in Blender and save it",
                     "can you make a car in Blender and save it"):
        plan = mapping.map_text(sentence)
        if plan:
            emitted.update(step["action"] for step in plan["actions"])

    assert "save_file" not in emitted


# ======================================================
# Reading a sentence
# ======================================================

@pytest.mark.parametrize("sentence,expect", [
    ("Create a car in Blender", "recipe:car"),
    ("Make a character in Blender", "recipe:character"),
    ("Generate a low-poly tree in Blender", "recipe:tree"),
    ("Build a house in Blender", "recipe:house"),
    ("make a table in Blender", "recipe:table"),
    ("make a sword in Blender", "recipe:sword"),
    ("Rig the Character_Torso in Blender", "rig"),
    ("Animate a walk cycle in Blender", "animate"),
    ("UV unwrap the Cube in Blender", "unwrap"),
    ("Sculpt a creature head in Blender", "sculpt"),
])
def test_the_named_phrases_are_recognised(sentence, expect):
    result = mapping.map_text(sentence)

    assert result is not None, sentence
    assert expect in result["matched"]
    assert result["actions"]


# ======================================================
# The tool gate
#
# Blender acts only when Blender is named. Without this, "make a car"
# starts a subprocess and writes files because somebody used a common
# verb -- and "make a car" could as easily mean a game object, a
# drawing, or nothing in particular.
# ======================================================

@pytest.mark.parametrize("sentence,expect", [
    ("Make a car in Blender", "recipe:car"),
    ("Make a car with Blender", "recipe:car"),
    ("Make a car using Blender", "recipe:car"),
    ("use Blender to make a car", "recipe:car"),
    ("Blender, make a car", "recipe:car"),
    ("Blender: make a car", "recipe:car"),
    ("MAKE A CAR IN BLENDER", "recipe:car"),
    ("Create a character in Blender", "recipe:character"),
    ("Animate a walk cycle in Blender", "animate"),
])
def test_naming_blender_turns_the_layer_on(sentence, expect):
    result = mapping.map_text(sentence)

    assert result is not None, sentence
    assert expect in result["matched"]
    assert result["actions"]


@pytest.mark.parametrize("sentence", [
    "Make a car",
    "Create a character",
    "Animate a walk cycle",
    "Generate a low-poly tree",
    "Build a house",
    "make a sword called Excalibur",
    "rig this model",
    "UV unwrap this mesh",
])
def test_a_generic_request_does_nothing(sentence):
    """The whole point of the gate. Each of these is a sentence a
    person might say about a game engine, a drawing, or nothing in
    particular, and none of them asked for Blender."""
    assert mapping.map_text(sentence) is None


@pytest.mark.parametrize("sentence", [
    "Make a car in Ludo",
    "Make a car in Ludo.ai",
    "generate a character using Ludo",
    "Ludo, make a car",
    "build a house with Ludo.ai",
])
def test_another_tool_is_not_this_one(sentence):
    """Ludo.ai is a different asset tool with its own path. A sentence
    naming it must not start Blender."""
    assert mapping.map_text(sentence) is None


def test_naming_two_tools_is_a_reason_to_ask_not_to_pick():
    """An ambiguous sentence is answered, not guessed at."""
    assert mapping.map_text("make a car in Ludo and Blender") is None
    assert mapping.map_text("make a car in Blender or Ludo") is None


def test_unity_is_not_treated_as_a_competing_tool():
    """"Export it from Blender to Unity" is a Blender request with a
    destination -- blender_asset_pipeline exists for exactly that
    sentence, so naming Unity must not switch the layer off."""
    result = mapping.map_text("export it as fbx from Blender to Unity")

    assert result is not None
    assert "export" in result["matched"]


def test_talking_about_blender_is_not_asking_for_it():
    """The gate is about the tool being named, not the word appearing.
    None of these asked for anything to be built."""
    for sentence in ("Blender is a modelling tool",
                     "I have Blender installed",
                     "Blender 5.0.1 is the version I use"):
        assert mapping.map_text(sentence) is None, sentence


def test_the_gate_and_the_mapper_agree():
    """Routing will ask names_blender() and running will call
    map_text(). A sentence that passes one and fails the other is a
    bug waiting to happen, so the predicate is public and this pins
    the one direction that matters: nothing maps without it."""
    for sentence in ("Make a car", "Create a character in Blender",
                     "hello", "Make a car in Ludo",
                     "Animate a walk cycle in Blender"):
        if mapping.map_text(sentence) is not None:
            assert mapping.names_blender(sentence), sentence
            assert not mapping.names_another_tool(sentence), sentence


@pytest.mark.parametrize("sentence", [
    "what is blender?",
    "how do I model a car in Blender?",
    "what's the best way to make a tree in Blender",
    "explain how to make a sword in Blender",
    "why would I rig a character in Blender",
    "is making a house in Blender hard?",
    "the car in Blender is red",
    "hello",
    "",
    "   ",
])
def test_a_question_never_builds_anything(sentence):
    """Measured: "how do I model a car in Blender?" built a
    twenty-seven step car, because the verb list contains "model" and
    the question mark meant nothing. Somebody asking how a thing is
    done wants an answer, not four wheels.

    Every case here names Blender, so it is the interrogative doing
    the refusing rather than the tool gate.
    """
    assert mapping.map_text(sentence) is None


@pytest.mark.parametrize("sentence", [
    "can you make a car in Blender?",
    "could you build me a house in Blender?",
    "would you make a tree in Blender",
    "will you rig this in Blender",
    "can you make a red sword called Excalibur in Blender",
])
def test_a_polite_interrogative_is_still_an_instruction(sentence):
    """"Can you make a car in Blender?" is a request in a polite
    shape. Once the tool gate has done its job there is no ambiguity
    left for a question guard to protect against -- the person named
    Blender -- so refusing them for being polite would be friction
    with no safety behind it."""
    result = mapping.map_text(sentence)

    assert result is not None, sentence
    assert result["actions"]


@pytest.mark.parametrize("sentence", [
    "can you tell me how to make a car in Blender?",
    "could you explain rigging in Blender",
    "can you show me how to make a tree in Blender",
    "can you walk me through making a sword in Blender",
    "what's the best way to make a house in Blender",
    "is there a tutorial for making a car in Blender",
    "what are the steps to make a car in Blender",
    "teach me to rig a character in Blender",
])
def test_asking_to_be_taught_is_answered_not_run(sentence):
    """The half that survives letting interrogatives through.

    "Can you tell me how to make a car in Blender?" opens with "can",
    so the opening alone can no longer decide -- without this it would
    have gone on to build twenty-seven steps nobody asked for. These
    are separated by what they ask FOR, wherever the phrase sits.
    """
    assert mapping.map_text(sentence) is None


@pytest.mark.parametrize("sentence", [
    "please make a low-poly tree in Blender",
    "make a car for me in Blender",
    "I want a house in Blender",
])
def test_a_politely_worded_instruction_still_builds(sentence):
    """Politeness that is not an interrogative opening is still an
    instruction, and must not be swallowed."""
    result = mapping.map_text(sentence)

    assert result is not None, sentence
    assert result["actions"]


def test_every_mapped_action_is_a_real_template():
    """The strongest thing these two modules owe each other: the
    mapper can only ever emit actions the templates know."""
    unknown = set()
    for sentence in ("Create a car in Blender",
                     "Make a character and rig it in Blender",
                     "Generate a low-poly tree in Blender",
                     "Build a house in Blender", "make a red table in Blender",
                     "make a chair in Blender", "make a sword in Blender",
                     "make a barrel in Blender", "make a rock in Blender",
                     "Animate a walk cycle in Blender",
                     "Animate an idle in Blender",
                     "UV unwrap the Cube with seams in Blender",
                     "Sculpt a creature head in Blender",
                     "in Blender export it as fbx",
                     "in Blender export it as obj",
                     "mirror the Cube on the Y-axis in Blender",
                     "smooth the Cube in Blender"):
        result = mapping.map_text(sentence)
        if not result:
            continue
        for step in result["actions"]:
            if step["action"] not in templates.TEMPLATES:
                unknown.add((sentence, step["action"]))

    assert unknown == set()


def test_everything_the_mapper_emits_compiles():
    """End to end without Blender: a sentence becomes source that
    parses. This is the check that would have caught `def main:`."""
    for sentence in ("Create a red car in Blender",
                     "Make a character and rig it in Blender",
                     "Generate a low-poly tree in Blender",
                     "make a sword called Excalibur in Blender"):
        result = mapping.map_text(sentence)
        source = templates.build_script(result["actions"])

        compile(source, f"<{sentence}>", "exec")


def test_a_recipe_starts_from_an_empty_scene():
    """Blender's default scene has a cube, a camera and a light in it,
    and a car built around a stray cube is only noticed after the
    export."""
    result = mapping.map_text("Create a car in Blender")

    assert result["actions"][0]["action"] == "clear_scene"


def test_a_colour_in_the_sentence_reaches_the_material():
    red = mapping.map_text("make a red car in Blender")
    blue = mapping.map_text("make a blue car in Blender")

    def paint(plan):
        for step in plan["actions"]:
            if step["action"] == "create_material" and "Paint" in step["params"]["name"]:
                return step["params"]["color"]
        return None

    assert paint(red) != paint(blue)
    assert paint(red)[0] > paint(red)[2]     # more red than blue
    assert paint(blue)[2] > paint(blue)[0]   # and the other way round


def test_a_name_in_the_sentence_is_used():
    result = mapping.map_text("make a sword called Excalibur in Blender")

    names = [s["params"]["name"] for s in result["actions"]
             if s["action"].startswith("add_")]
    assert any(name.startswith("Excalibur") for name in names)


def test_a_name_cannot_carry_punctuation_into_the_script():
    result = mapping.map_text('in Blender make a sword called "); import os; ("')

    if result:  # it may simply not match, which is also fine
        source = templates.build_script(result["actions"])
        compile(source, "<named>", "exec")


def test_low_poly_actually_lowers_the_polygon_count():
    """A word that changes nothing is a word that lies."""
    plain = mapping.map_text("make a tree in Blender")
    low = mapping.map_text("make a low-poly tree in Blender")

    def has(plan, action):
        return any(s["action"] == action for s in plan["actions"])

    assert has(low, "apply_decimate")
    assert not has(plain, "apply_decimate")


def test_a_recipe_and_an_operation_in_one_sentence_both_happen():
    result = mapping.map_text("Create a character and rig it in Blender")

    assert "recipe:character" in result["matched"]
    assert "rig" in result["matched"]
    # And the rig must bind to the mesh the recipe just made, not to
    # some name nobody created.
    bound = [s for s in result["actions"] if s["action"] == "auto_weights"]
    created = {s["params"]["name"] for s in result["actions"]
               if s["action"].startswith("add_")}
    assert bound and bound[0]["params"]["mesh"] in created


def test_a_walk_cycle_is_a_loop():
    """First and last pose must match or the animation snaps."""
    result = mapping.map_text("animate a walk cycle in Blender")

    poses = [s["params"] for s in result["actions"] if s["action"] == "set_pose"]
    frames = sorted({p["frame"] for p in poses})
    first = {p["bone"]: tuple(p["rotation"]) for p in poses if p["frame"] == frames[0]}
    last = {p["bone"]: tuple(p["rotation"]) for p in poses if p["frame"] == frames[-1]}

    assert first == last


def test_an_export_never_invents_a_path():
    """ARIA is not allowed to hallucinate paths, so a sentence with no
    path in it produces a placeholder the caller must fill."""
    result = mapping.map_text("in Blender export it as fbx")

    step = [s for s in result["actions"] if s["action"].startswith("export_")][0]
    assert step["params"]["path"].startswith("<")


def test_the_summary_says_what_will_happen():
    result = mapping.map_text("Create a red car in Blender")

    assert "Car_Body" in result["summary"]
    assert result["summary"] != ""


# ======================================================
# Answering a request in someone's own words
#
# THE FAILURE THIS EXISTS FOR
# Measured, with the Blender plugin installed, enabled, and pointing
# at a working Blender 5.0.1. Typed into chat:
#
#     create a car for me in Blender
#
# ARIA routed it to phi-3-mini, which answered:
#
#     "I can guide you through creating a car in Blender. What
#      specific features would you like for your car model?"
#
# Nothing ran. Every piece needed to build that car existed and was
# tested; nothing called any of it. A model that cannot build
# something does not say so -- it offers to help, which reads like
# progress and is not.
# ======================================================

@pytest.fixture
def output_folder(tmp_path, monkeypatch):
    folder = tmp_path / "out"
    monkeypatch.setenv(actions.ENV_OUTPUT, str(folder))
    return folder


def test_the_sentence_from_the_log_builds_something(blender, output_folder):
    answer = actions.answer_request("create a car for me in Blender")

    assert answer is not None
    assert answer["ran"] is True
    assert "Built Car in Blender" in answer["text"]


def test_a_request_always_writes_something_to_disk(blender, output_folder,
                                                    monkeypatch):
    """A plan on its own builds in memory and Blender then exits,
    which leaves nothing. The run would 'succeed' and the person would
    have nothing to show for it."""
    seen = {}
    real = actions.run_actions

    def watched(acts, **kwargs):
        seen["actions"] = [a["action"] for a in acts]
        return real(acts, **kwargs)

    monkeypatch.setattr(actions, "run_actions", watched)
    actions.answer_request("create a car for me in Blender")

    assert "save_file" in seen["actions"]
    assert any(a.startswith("export_") for a in seen["actions"])


def test_the_format_the_sentence_asked_for_is_the_one_written(blender,
                                                              output_folder,
                                                              monkeypatch):
    seen = {}
    real = actions.run_actions
    monkeypatch.setattr(actions, "run_actions",
                        lambda acts, **kw: seen.update(actions=acts) or real(acts, **kw))

    actions.answer_request("in Blender make a car and export it as glb")

    exports = [a for a in seen["actions"] if str(a["action"]).startswith("export_")]
    assert exports
    assert all(a["action"] == "export_glb" for a in exports)


def test_a_placeholder_path_is_replaced_with_a_real_one(blender, output_folder,
                                                        monkeypatch):
    """The mapper leaves "<export>.fbx" rather than inventing a path,
    because ARIA is not allowed to hallucinate one. Something has to
    fill it in before the run."""
    seen = {}
    real = actions.run_actions
    monkeypatch.setattr(actions, "run_actions",
                        lambda acts, **kw: seen.update(actions=acts) or real(acts, **kw))

    actions.answer_request("in Blender make a car and export it as fbx")

    for step in seen["actions"]:
        path = (step.get("params") or {}).get("path")
        if path:
            assert not str(path).startswith("<"), step


def test_two_runs_do_not_overwrite_each_other(blender, output_folder,
                                              monkeypatch):
    """Silently replacing yesterday's Car is the sort of loss nobody
    notices until they go looking for it."""
    written = []
    real = actions.run_actions

    def watched(acts, **kwargs):
        written.append([(a.get("params") or {}).get("path")
                        for a in acts if a["action"] == "save_file"][0])
        return real(acts, **kwargs)

    monkeypatch.setattr(actions, "run_actions", watched)
    output_folder.mkdir(parents=True, exist_ok=True)
    (output_folder / "Car.blend").write_bytes(b"already here")

    actions.answer_request("create a car in Blender")

    assert not written[0].endswith("Car.blend")


def test_nothing_is_written_into_the_project_source(blender, output_folder):
    """ARIA never writes into project_root outside a commit, and never
    into somebody's Unity project without being asked."""
    answer = actions.answer_request("create a car in Blender")

    assert str(output_folder) in answer["text"]


def test_a_request_i_cannot_fill_says_so(blender, output_folder):
    """The important half. A model handed "make me a spaceship" would
    describe one and sound like it built it."""
    answer = actions.answer_request("make me a spaceship in Blender")

    assert answer is not None
    assert answer["ran"] is False
    assert "do not know how" in answer["text"]
    # And it says what it CAN do, so the person is not left guessing.
    assert "car" in answer["text"]


@pytest.mark.parametrize("sentence", [
    "in Blender, what makes good topology?",
    "can you tell me how to rig in Blender",
    "what is Blender?",
    "make a car",
    "make a car in Ludo",
    "what is the weather in Paris?",
    "hello",
    "",
])
def test_anything_else_is_handed_back(blender, output_folder, sentence):
    """None means "not mine". A question about Blender wants a model,
    and a model can answer it."""
    assert actions.answer_request(sentence) is None


def test_no_blender_configured_is_a_refusal_not_a_claim(no_blender,
                                                        output_folder):
    answer = actions.answer_request("create a car in Blender")

    assert answer["ran"] is False
    assert "nothing happened" in answer["text"]
    assert "Built" not in answer["text"]


def test_a_failed_run_is_reported_as_one(broken_blender, output_folder):
    answer = actions.answer_request("create a car in Blender")

    assert answer["ran"] is True
    assert "failed" in answer["text"]
    assert "Built" not in answer["text"]


# ======================================================
# The orchestrator
# ======================================================

def _turn(text):
    from backend.core.turn_types import SessionState, TurnRequest
    return TurnRequest(messages=[{"role": "user", "content": text}],
                       latest_user_text=text, conversation_id="test",
                       session=SessionState())


def test_the_turn_is_answered_without_a_model(blender, output_folder):
    from backend.core import turn_orchestrator

    result = turn_orchestrator._blender_reply(_turn("create a car in Blender"), [])

    assert result is not None
    assert result.model_id == turn_orchestrator.BLENDER_MODEL
    assert "Built Car in Blender" in result.text


@pytest.mark.parametrize("sentence", [
    "make a car",
    "in Blender, what makes good topology?",
    "what is the weather in Paris?",
    "write me a haiku",
    "make a car in Ludo",
    "",
])
def test_ordinary_messages_are_untouched(blender, output_folder, sentence):
    from backend.core import turn_orchestrator

    assert turn_orchestrator._blender_reply(_turn(sentence), []) is None


def test_a_broken_layer_still_says_nothing_happened(blender, output_folder,
                                                    monkeypatch):
    """An exception must not hand the turn back to the model, which is
    the one outcome this whole path exists to prevent."""
    from backend.core import turn_orchestrator

    def explode(_text, **_kwargs):
        raise RuntimeError("the layer fell over")

    monkeypatch.setattr(actions, "answer_request", explode)

    result = turn_orchestrator._blender_reply(_turn("create a car in Blender"), [])

    assert result is not None
    assert "nothing happened" in result.text


def test_the_short_circuit_runs_before_any_model_is_chosen(blender,
                                                           output_folder):
    """A floor works wherever the decision is made; a gate only works
    where it is placed. This one is placed ahead of intent detection
    and model routing, with the other two command short-circuits."""
    import inspect
    from backend.core import turn_orchestrator

    source = inspect.getsource(turn_orchestrator.orchestrate_turn)
    blender_at = source.index("_blender_reply")
    routing_at = source.index("INTENT_WORKSPACE_QUERY")

    assert blender_at < routing_at


# ======================================================
# Getting it into Unity
# ======================================================

@pytest.fixture
def unity_project(tmp_path, monkeypatch):
    root = tmp_path / "Project"
    (root / "Assets").mkdir(parents=True)
    from backend.unity import unity_cli_engine
    monkeypatch.setattr(unity_cli_engine, "_plugin_setting",
                        lambda field: str(root) if field == unity_cli_engine.FIELD_PROJECT else "")
    return root


@pytest.mark.parametrize("folder,expected", [
    ("Assets/ARIA", "Assets/ARIA/car.fbx"),
    ("ARIA/Models", "Assets/ARIA/Models/car.fbx"),
    ("Assets\\ARIA", "Assets/ARIA/car.fbx"),
    ("/Assets/ARIA/", "Assets/ARIA/car.fbx"),
])
def test_an_asset_path_is_always_unix_and_assets_relative(folder, expected):
    """Unity asset paths are not filesystem paths, and a backslash in
    one surfaces much later as a missing texture."""
    assert pipeline.asset_path_for(Path("C:/tmp/car.fbx"), folder) == expected


def test_a_copy_works_with_unity_closed(unity_project, tmp_path):
    export = tmp_path / "car.fbx"
    export.write_bytes(b"not really an fbx")

    result = pipeline.move_export_to_unity(str(export))

    assert result["success"] is True
    assert (unity_project / result["asset_path"]).is_file()
    assert result["imported"] is False


def test_a_copy_of_something_that_is_not_there_fails(unity_project, tmp_path):
    result = pipeline.move_export_to_unity(str(tmp_path / "ghost.fbx"))

    assert result["success"] is False
    assert "no file" in result["error"]


def test_no_project_set_says_so(monkeypatch, tmp_path):
    from backend.unity import unity_cli_engine
    monkeypatch.setattr(unity_cli_engine, "_plugin_setting", lambda field: "")
    export = tmp_path / "car.fbx"
    export.write_bytes(b"x")

    result = pipeline.move_export_to_unity(str(export))

    assert result["success"] is False
    assert "No Unity project" in result["error"]


def test_an_unreachable_editor_falls_back_to_a_copy(unity_project, tmp_path,
                                                    monkeypatch):
    """Measured against the real CLI with the Editor closed: "No
    Pipeline instance found for project". The file can still be put
    where Unity will find it."""
    from backend.unity import unity_cli_engine
    monkeypatch.setattr(unity_cli_engine, "run_invocation",
                        lambda *a, **k: {"success": False, "output": "",
                                         "json": None,
                                         "error": "No Pipeline instance found"})
    export = tmp_path / "car.fbx"
    export.write_bytes(b"x")

    result = pipeline.trigger_unity_import(str(export))

    assert result["success"] is True
    assert result["imported"] is False
    assert "not reachable" in result["note"]
    assert (unity_project / result["asset_path"]).is_file()


def test_a_fallback_copy_does_not_claim_a_prefab(unity_project, tmp_path,
                                                 monkeypatch):
    """The whole point of this layer: never report work that did not
    happen."""
    from backend.unity import unity_cli_engine
    monkeypatch.setattr(unity_cli_engine, "run_invocation",
                        lambda *a, **k: {"success": False, "output": "",
                                         "json": None, "error": "not reachable"})
    export = tmp_path / "car.fbx"
    export.write_bytes(b"x")

    result = pipeline.deliver_to_unity(str(export), place=True, prefab=True)

    assert "prefab" not in result
    assert "instance" not in result
    assert "Open the Unity Editor" in result["summary"]


def test_a_failing_command_is_read_from_the_envelope(unity_project, tmp_path,
                                                     monkeypatch):
    """The CLI exits 0 for a command that failed inside the Editor, so
    the JSON envelope is the thing to believe."""
    from backend.unity import unity_cli_engine
    monkeypatch.setattr(unity_cli_engine, "run_invocation",
                        lambda *a, **k: {"success": True, "output": "",
                                         "json": {"success": False, "data": None,
                                                  "errors": [{"message": "no scene"}]},
                                         "error": None})

    result = pipeline.place_in_scene("Assets/ARIA/car.fbx")

    assert result["success"] is False
    assert "no scene" in result["error"]


def test_arguments_are_separate_argv_entries(unity_project, tmp_path, monkeypatch):
    """A path with a space in it must stay one argument."""
    seen = {}
    from backend.unity import unity_cli_engine

    def watched(command, args=None, **kwargs):
        seen["command"] = command
        seen["args"] = list(args or [])
        return {"success": True, "output": "", "json": {"success": True, "data": {}},
                "error": None}

    monkeypatch.setattr(unity_cli_engine, "run_invocation", watched)
    export = tmp_path / "my car.fbx"
    export.write_bytes(b"x")

    pipeline.trigger_unity_import(str(export))

    assert seen["command"] == "cmd import_asset"
    assert "--source" in seen["args"]
    assert str(export.resolve()) in seen["args"]
    assert "--path" in seen["args"]
    assert "Assets/ARIA/my car.fbx" in seen["args"]


def test_the_parameter_names_match_the_pipeline_package(unity_project, tmp_path,
                                                        monkeypatch):
    """Read out of the package's own source rather than guessed:

        import_asset(source, path, confirm, dry_run)
        instantiate_prefab(prefab, scene_path, name)
        create_prefab(source, path)

    Four earlier guesses about this CLI were wrong, which is why this
    is pinned."""
    calls = []
    from backend.unity import unity_cli_engine
    monkeypatch.setattr(unity_cli_engine, "run_invocation",
                        lambda command, args=None, **k: calls.append((command, list(args or []))) or
                        {"success": True, "output": "",
                         "json": {"success": True, "data": {"globalId": "abc"}},
                         "error": None})
    export = tmp_path / "car.fbx"
    export.write_bytes(b"x")

    pipeline.deliver_to_unity(str(export), place=True, prefab=True)

    flags = {command: [a for a in args if a.startswith("--")]
             for command, args in calls}
    assert set(flags["cmd import_asset"]) <= {"--source", "--path", "--confirm",
                                              "--dry_run"}
    assert set(flags["cmd instantiate_prefab"]) <= {"--prefab", "--scene_path",
                                                    "--name"}
    assert set(flags["cmd create_prefab"]) <= {"--source", "--path"}


def test_a_prefab_is_made_from_the_scene_instance_not_the_asset(unity_project,
                                                                tmp_path,
                                                                monkeypatch):
    """create_prefab takes a GameObject in a scene, not an asset path
    -- so the model has to be placed first."""
    calls = []
    from backend.unity import unity_cli_engine

    def watched(command, args=None, **kwargs):
        args = list(args or [])
        calls.append((command, args))
        identity = "GlobalObjectId-instance" if "instantiate" in command else "abc"
        return {"success": True, "output": "",
                "json": {"success": True, "data": {"globalId": identity}},
                "error": None}

    monkeypatch.setattr(unity_cli_engine, "run_invocation", watched)
    export = tmp_path / "car.fbx"
    export.write_bytes(b"x")

    pipeline.deliver_to_unity(str(export), place=True, prefab=True)

    ordered = [command for command, _ in calls]
    assert ordered.index("cmd instantiate_prefab") < ordered.index("cmd create_prefab")
    prefab_args = dict(zip(*[iter(dict(calls)["cmd create_prefab"])] * 2))
    assert prefab_args["--source"] == "GlobalObjectId-instance"


def test_a_later_step_failing_does_not_erase_an_earlier_one(unity_project,
                                                            tmp_path, monkeypatch):
    """"The import worked and the placement did not" is a true and
    useful sentence; one success flag over four steps is not."""
    from backend.unity import unity_cli_engine

    def watched(command, args=None, **kwargs):
        if "instantiate" in command:
            return {"success": True, "output": "",
                    "json": {"success": False, "data": None,
                             "errors": [{"message": "no loaded scene"}]},
                    "error": None}
        return {"success": True, "output": "",
                "json": {"success": True, "data": {"globalId": "abc"}}, "error": None}

    monkeypatch.setattr(unity_cli_engine, "run_invocation", watched)
    export = tmp_path / "car.fbx"
    export.write_bytes(b"x")

    result = pipeline.deliver_to_unity(str(export), place=True)

    assert result["steps"]["import"]["success"] is True
    assert result["steps"]["place"]["success"] is False
    assert "no loaded scene" in result["summary"]


def test_a_non_model_is_not_placed_in_a_scene(unity_project, tmp_path, monkeypatch):
    """A texture is not a prefab, and asking Unity to instantiate one
    is an error nobody needed to see."""
    calls = []
    from backend.unity import unity_cli_engine
    monkeypatch.setattr(unity_cli_engine, "run_invocation",
                        lambda command, args=None, **k: calls.append(command) or
                        {"success": True, "output": "",
                         "json": {"success": True, "data": {"globalId": "abc"}},
                         "error": None})
    export = tmp_path / "colour.png"
    export.write_bytes(b"x")

    pipeline.deliver_to_unity(str(export), place=True, prefab=True)

    assert calls == ["cmd import_asset"]


def test_a_request_blender_cannot_fill_points_somewhere_it_can(monkeypatch,
                                                               tmp_path):
    """MEASURED. Typed into chat:

        Create a 3D Model in Blender of a Anime Swordsman holding a
        katana

    The answer was the recipe list and nothing else -- honest, and
    unhelpful. Blender's limit is real: it builds from cubes and
    cylinders, and no arrangement of primitives is an anime swordsman.
    Ludo has no such limit, and it was one word away.
    """
    monkeypatch.setenv("ARIA_LUDO_API_KEY", "a-key-so-ludo-looks-configured")

    answer = actions.answer_request(
        "Create a 3D Model in Blender of a Anime Swordsman holding a katana")

    assert answer["ran"] is False, "nothing was built, and nothing was spent"
    assert "do not know how" in answer["text"]
    assert "in Ludo" in answer["text"]
    # Taking the suggestion spends money, so the suggestion says so.
    assert "costs credits" in answer["text"]
    # The tool name is stripped out of the suggested phrasing, or the
    # suggestion would name two tools and route to neither.
    assert "in Blender" not in answer["text"].split("Try:")[1]


def test_no_suggestion_when_ludo_could_not_take_it(monkeypatch, tmp_path):
    """Suggesting a route that would fail is worse than suggesting
    nothing."""
    monkeypatch.delenv("ARIA_LUDO_API_KEY", raising=False)
    from backend.plugins import plugin_settings
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE,
                       str(tmp_path / "none.json"))

    answer = actions.answer_request(
        "Create a 3D Model in Blender of a Anime Swordsman")

    assert "in Ludo" not in answer["text"]


def test_the_suggested_phrase_would_actually_route_to_ludo(monkeypatch):
    """A suggestion the router would not honour is a dead end. This
    takes the sentence out of the reply and puts it through the real
    Ludo mapper."""
    monkeypatch.setenv("ARIA_LUDO_API_KEY", "a-key")
    from backend.ludo import ludo_nl_mapping

    answer = actions.answer_request(
        "Create a 3D Model in Blender of a Anime Swordsman holding a katana")
    suggested = answer["text"].split("Try:")[1].strip().splitlines()[0].strip()

    assert ludo_nl_mapping.names_ludo(suggested)
    assert not ludo_nl_mapping.names_another_tool(suggested)
    assert ludo_nl_mapping.map_text(suggested) is not None
