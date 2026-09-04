"""Typed calls for Blender: what maps, and what is refused rather than guessed."""

import pytest

from backend.blender import blender_typed_calls as tc


# ======================================================
# The vocabulary
# ======================================================

def test_every_action_is_callable_as_a_typed_name():
    """The table is derived, so it cannot drift from blender_actions."""
    import inspect

    from backend.blender import blender_actions

    expected = {
        name for name in blender_actions.__all__
        if name not in tc._NOT_OPERATIONS
        and inspect.isfunction(getattr(blender_actions, name, None))
    }

    derived = {operation.python_name for operation in tc.OPERATIONS.values()}
    assert derived == expected


def test_names_are_pascal_case():
    assert "AddCube" in tc.OPERATIONS
    assert "SmartUvProject" in tc.OPERATIONS
    assert "ExportGlb" in tc.OPERATIONS


# ======================================================
# Reading a message
# ======================================================

def test_a_sentence_is_not_this_layers_business():
    """A question about AddCube must reach a model, not be run."""
    assert tc.parse_blender_calls("how does AddCube work?") is None
    assert tc.parse_blender_calls("") is None


def test_a_build_maps_in_order():
    planned = tc.parse_blender_calls(
        'AddCube("BenchTop", size=1, location=[0, 0, 0.9])\n'
        'Scale("BenchTop", 1.2, 0.1, 0.7)\n'
        'ExportGlb("out.glb", "BenchTop")')

    assert [step["function"] for step in planned] == ["add_cube", "scale", "export_glb"]
    assert planned[0]["args"] == {"name": "BenchTop", "size": 1, "location": [0, 0, 0.9]}
    assert planned[1]["args"] == {"obj": "BenchTop", "x": 1.2, "y": 0.1, "z": 0.7}


def test_a_code_fence_is_stripped():
    planned = tc.parse_blender_calls('```\nAddCube("A")\nAddSphere("B")\n```')
    assert [step["operation"] for step in planned] == ["AddCube", "AddSphere"]


def test_a_call_may_span_lines():
    planned = tc.parse_blender_calls(
        'AddCylinder("Pillar",\n'
        '            radius=0.15,\n'
        '            depth=2.0)')
    assert planned[0]["args"] == {"name": "Pillar", "radius": 0.15, "depth": 2.0}


def test_camel_case_arguments_are_accepted():
    """Nobody typing calls should have to know which spelling Python used."""
    planned = tc.parse_blender_calls('AddTorus("Ring", majorRadius=2, minorRadius=0.3)')
    assert planned[0]["args"] == {"name": "Ring", "major_radius": 2, "minor_radius": 0.3}


# ======================================================
# Name first
# ======================================================

def test_add_cube_reads_the_name_first():
    """add_cube's signature is (size, location, name); the typed call is not.

    Every other Create* in ARIA takes the name first, and reading
    AddCube("BenchTop") as a size is the CreateLight trap again.
    """
    assert tc.OPERATIONS["AddCube"].order[0] == "name"

    planned = tc.parse_blender_calls('AddCube("BenchTop", 1.5)')
    assert planned[0]["args"] == {"name": "BenchTop", "size": 1.5}


@pytest.mark.parametrize("call, expected", [
    ('AddSphere("Boulder", 0.8)', {"name": "Boulder", "radius": 0.8}),
    ('AddPlane("Floor", 10)', {"name": "Floor", "size": 10}),
])
def test_every_primitive_reads_the_name_first(call, expected):
    assert tc.parse_blender_calls(call)[0]["args"] == expected


# ======================================================
# Being wrong out loud
# ======================================================

def test_prose_mixed_with_calls_is_refused_not_half_run():
    """The failure this exists to prevent: four good lines and one sentence."""
    with pytest.raises(tc.Unmappable, match="not both in one message"):
        tc.parse_blender_calls('AddCube("X")\nplease also bevel it')


def test_an_unknown_argument_names_what_is_taken():
    with pytest.raises(tc.Unmappable, match="no sizze argument"):
        tc.parse_blender_calls('AddCube("X", sizze=2)')


def test_an_argument_given_twice_is_refused():
    with pytest.raises(tc.Unmappable, match="given name twice"):
        tc.parse_blender_calls('AddCube("X", 2, name="Y")')


def test_an_expression_is_refused_rather_than_evaluated():
    with pytest.raises(tc.Unmappable, match="plain value"):
        tc.parse_blender_calls('AddCube("X", size=1+1)')


def test_too_many_unnamed_values_are_refused():
    with pytest.raises(tc.Unmappable, match="at most"):
        tc.parse_blender_calls('AddCube("X", 1, [0,0,0], "extra", "more")')


# ======================================================
# Running
# ======================================================

def test_a_build_is_one_blender_launch(monkeypatch):
    """Six operations must be one scene, not six default scenes.

    blender_actions' public functions each run Blender immediately from
    --factory-startup, so calling them in sequence loses everything the
    previous one made. Planning descriptors and running them together is
    the whole reason plan_blender_calls exists.
    """
    launches = []

    def once(actions, **kwargs):
        launches.append([action["action"] for action in actions])
        return {"ran": True, "success": True,
                "result": {"created": ["BenchTop"], "exported": ["out.glb"]}}

    from backend.blender import blender_actions
    monkeypatch.setattr(blender_actions, "run_actions", once)

    answer = tc.answer_typed(
        'AddCube("BenchTop")\n'
        'Scale("BenchTop", 2, 2, 2)\n'
        'ExportGlb("out.glb")')

    assert len(launches) == 1
    assert launches[0] == ["add_cube", "scale", "export_glb"]
    assert answer["ran"] is True


def test_a_plan_never_runs_anything(monkeypatch):
    """Planning is inspection; nothing should reach Blender."""
    from backend.blender import blender_actions
    monkeypatch.setattr(blender_actions, "run_actions",
                        lambda *a, **k: pytest.fail("planning ran Blender"))

    assert tc.plan_blender_calls('AddCube("X")') == [
        {"action": "add_cube", "params": {"name": "X"}}]


def test_answer_typed_reports_a_refusal_without_running(monkeypatch):
    from backend.blender import blender_actions
    monkeypatch.setattr(blender_actions, "run_actions",
                        lambda *a, **k: pytest.fail("a refused message ran Blender"))

    answer = tc.answer_typed('AddCube("X")\nplease also bevel it')

    assert answer["ran"] is False


def test_answer_typed_hands_a_question_back():
    assert tc.answer_typed("what does ExportGlb do?") is None
