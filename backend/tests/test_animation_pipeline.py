"""Giving a rigged character something to play.

WHAT THE SPECIFICATION ASKED FOR AND WHAT THE PACKAGE HAS
---------------------------------------------------------
Six commands were named. One exists:

    create_animator_controller   exists
    add_state                    is add_animator_state
    add_transition               is add_animator_transition
    add_parameter                is add_animator_parameter
    assign_animator_controller   does not exist -- the Animator's
                                 m_Controller property is written
    validate_humanoid_avatar     does not exist -- isHuman and isValid
                                 are Avatar properties, read with eval

MOST OF THIS SUITE NEEDS NO UNITY AT ALL. The graph and its parameters
are ordinary Python, and every mistake worth catching about them -- a
transition to a state nobody declared, a Trigger compared against a
number, two default states, a state nothing leaves -- is decided
before the Editor is asked anything. That is deliberate: those same
mistakes reach Unity as parameter-validation failures with no
indication of which of eleven commands caused them.

The Unity half is faked, and the fakes were written from the shapes a
live Editor actually returned -- including that get_animator_controller
answers with parameters, layers, states and transitions nested under
data.result.
"""

from __future__ import annotations

import json

import pytest

from backend.unity import animation_parameters as params
from backend.unity import animation_pipeline as pipeline
from backend.unity import animation_state_graph as graphs
from backend.unity import animator_controller_builder as builder
from backend.unity import unity_delivery as delivery


# What the Editor reports for a Generic import with a real
# skeleton: no humanoid avatar, 48 transforms, no controller yet.
GENERIC_OK = ("hasAvatar=False isHuman=False isValid=False bones=48 "
              "controller=none")
HUMANOID_OK = ("hasAvatar=True isHuman=True isValid=True bones=48 "
               "controller=none")
AVATAR_OK = GENERIC_OK


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "Project"
    (root / "Assets").mkdir(parents=True)
    monkeypatch.setattr(delivery, "unity_project_root", lambda: root)
    return root


@pytest.fixture
def cli(monkeypatch):
    """The Unity CLI, answering the way a live Editor did."""
    calls = []
    state = {"avatar": AVATAR_OK}

    def fake(command, args=None, **kwargs):
        argv = list(args or [])
        named = {argv[i].lstrip("-"): argv[i + 1]
                 for i in range(0, len(argv) - 1, 2) if argv[i].startswith("--")}
        name = command.replace("cmd ", "")
        calls.append({"command": name, "args": named})

        def envelope(result):
            return {"success": True, "output": "", "error": None,
                    "json": {"success": True, "errors": [],
                             "data": {"command": name, "parameters": named,
                                      "result": result, "target": {},
                                      "success": True}}}

        if name == "eval":
            return envelope({"success": True, "result": state["avatar"]})
        if name == "set_component_properties":
            return envelope({"type": "Animator", "properties": {}})
        return envelope({"assetPath": named.get("path"), "name": named.get("name")})

    from backend.unity import unity_cli_engine
    monkeypatch.setattr(unity_cli_engine, "run_invocation", fake)
    return calls, state


def commands(calls):
    return [c["command"] for c in calls]


def every(calls, command):
    return [c["args"] for c in calls if c["command"] == command]


# ======================================================
# The graph, which needs no Unity
# ======================================================

def test_the_default_graph_is_the_specified_one():
    graph = graphs.default_graph()

    assert [s.name for s in graph.states] == ["Idle", "Walk", "Run", "Attack"]
    assert graph.default_state.name == "Idle"

    edges = {(t.source, t.target) for t in graph.transitions}
    assert ("Idle", "Walk") in edges
    assert ("Walk", "Run") in edges
    assert ("Run", "Idle") in edges
    assert (graphs.ANY_STATE, "Attack") in edges


def test_the_thresholds_are_the_specified_ones():
    graph = graphs.default_graph()
    by_edge = {(t.source, t.target): t for t in graph.transitions}

    assert by_edge[("Idle", "Walk")].conditions[0].threshold == 0.1
    assert by_edge[("Walk", "Run")].conditions[0].threshold == 2.0
    assert by_edge[("Run", "Idle")].conditions[0].mode == "Less"
    assert by_edge[(graphs.ANY_STATE, "Attack")].conditions[0].mode == "If"


def test_the_four_specified_transitions_leave_attack_a_trap():
    """Following the specification exactly produces a character that
    enters Attack and never leaves, and a Walk that can only reach
    Idle by first speeding up into Run. Both are invisible until
    somebody plays it."""
    graph = graphs.default_graph(complete=False)

    # Advice, not an error: Unity accepts it and the caller asked for
    # it. Refusing would substitute our judgement for theirs; saying
    # nothing would ship a character stuck in its attack.
    assert graphs.validate_graph(graph, params.DEFAULT_PARAMETERS) == []
    assert any("nothing leaves 'Attack'" in a
               for a in graphs.graph_advice(graph))


def test_the_completed_graph_is_clean():
    graph = graphs.default_graph()

    assert graphs.validate_graph(graph, params.DEFAULT_PARAMETERS) == []


def test_the_completed_graph_adds_the_way_back():
    graph = graphs.default_graph()
    edges = {(t.source, t.target) for t in graph.transitions}

    assert ("Walk", "Idle") in edges
    assert ("Run", "Walk") in edges
    assert ("Attack", "Idle") in edges


def test_attack_leaves_on_exit_time_not_a_condition():
    """An attack ends when its clip has played, not when a parameter
    says so."""
    graph = graphs.default_graph()
    leaving = [t for t in graph.transitions
               if t.source == "Attack" and t.target == "Idle"][0]

    assert leaving.has_exit_time is True
    assert leaving.conditions == ()


def test_a_transition_to_a_state_nobody_declared_is_caught():
    graph = graphs.Graph(
        states=[graphs.State("Idle", is_default=True)],
        transitions=[graphs.Transition("Idle", "Fly",
                                       (graphs.Condition("speed", "Greater", 1),))])

    problems = graphs.validate_graph(graph, params.DEFAULT_PARAMETERS)
    assert any("no state called 'Fly'" in p for p in problems)


def test_a_condition_on_an_undeclared_parameter_is_caught():
    graph = graphs.Graph(
        states=[graphs.State("Idle", is_default=True), graphs.State("Walk")],
        transitions=[graphs.Transition("Idle", "Walk",
                                       (graphs.Condition("velocity", "Greater", 1),)),
                     graphs.Transition("Walk", "Idle",
                                       (graphs.Condition("speed", "Less", 1),))])

    problems = graphs.validate_graph(graph, params.DEFAULT_PARAMETERS)
    assert any("no parameter called 'velocity'" in p for p in problems)


def test_comparing_a_trigger_against_a_number_is_caught():
    """Greater/Less compare against a threshold, and a Trigger has no
    value to compare."""
    graph = graphs.Graph(
        states=[graphs.State("Idle", is_default=True), graphs.State("Attack")],
        transitions=[graphs.Transition("Idle", "Attack",
                                       (graphs.Condition("attack", "Greater", 1),)),
                     graphs.Transition("Attack", "Idle", has_exit_time=True)])

    problems = graphs.validate_graph(graph, params.DEFAULT_PARAMETERS)
    assert any("is a Trigger and Greater compares" in p for p in problems)


def test_two_default_states_are_caught():
    graph = graphs.Graph(
        states=[graphs.State("Idle", is_default=True),
                graphs.State("Walk", is_default=True)],
        transitions=[graphs.Transition("Idle", "Walk",
                                       (graphs.Condition("speed", "Greater", 1),)),
                     graphs.Transition("Walk", "Idle",
                                       (graphs.Condition("speed", "Less", 1),))])

    problems = graphs.validate_graph(graph, params.DEFAULT_PARAMETERS)
    assert any("2 states are marked default" in p for p in problems)


def test_a_transition_with_no_condition_and_no_exit_time_is_caught():
    """It fires immediately, so the state it leaves is unreachable."""
    graph = graphs.Graph(
        states=[graphs.State("Idle", is_default=True), graphs.State("Walk")],
        transitions=[graphs.Transition("Idle", "Walk"),
                     graphs.Transition("Walk", "Idle", has_exit_time=True)])

    assert any("fires immediately" in a for a in graphs.graph_advice(graph))


# ======================================================
# Parameters
# ======================================================

def test_the_specified_parameters_are_a_float_and_a_trigger():
    assert params.SPEED.type == "Float"
    assert params.ATTACK.type == "Trigger"
    assert params.validate_parameters(params.DEFAULT_PARAMETERS) == []


def test_a_type_unity_does_not_have_is_caught():
    problems = params.validate_parameters([params.Parameter("speed", "Double")])

    assert any("not one of" in p for p in problems)


def test_a_duplicate_parameter_is_caught():
    problems = params.validate_parameters(
        [params.Parameter("speed"), params.Parameter("speed")])

    assert any("declared twice" in p for p in problems)


def test_a_trigger_carries_no_default_value():
    """The command documents defaultValue as ignored for a Trigger, so
    sending one is a small lie about what was configured."""
    assert "defaultValue" not in params.ATTACK.payload()
    assert params.SPEED.payload()["defaultValue"] == 0.0


# ======================================================
# 1-4. Building the controller
# ======================================================

def test_it_creates_animator_controller(project, cli):
    calls, _ = cli
    result = pipeline.animate_character("Assets/ARIA/Prefabs/Hero.prefab",
                                        instance="Hero", name="Hero")

    assert result.success is True
    assert "create_animator_controller" in commands(calls)
    assert result.controller_path == "Assets/ARIA/Animators/Hero.controller"


def test_it_adds_states(project, cli):
    calls, _ = cli
    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    added = [a["name"] for a in every(calls, "add_animator_state")]
    assert added == ["Idle", "Walk", "Run", "Attack"]
    assert result.states == ["Idle", "Walk", "Run", "Attack"]


def test_the_first_state_is_the_default_one(project, cli):
    calls, _ = cli
    pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    idle = [a for a in every(calls, "add_animator_state")
            if a["name"] == "Idle"][0]
    assert idle.get("isDefault") == "true"


def test_every_state_gets_a_clip(project, cli):
    """A state with no motion is legal and plays nothing, so a
    controller full of them looks right in the inspector and does
    nothing in play mode."""
    calls, _ = cli
    pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    for state in every(calls, "add_animator_state"):
        assert state.get("motion", "").endswith(".anim"), state["name"]


def test_locomotion_clips_loop_and_an_attack_does_not(project, cli):
    calls, _ = cli
    pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    made = {a["path"].rsplit("_", 1)[-1]: a for a in
            every(calls, "create_animation_clip")}
    assert made["Idle.anim"]["loop"] == "true"
    assert made["Attack.anim"]["loop"] == "false"


def test_it_adds_transitions(project, cli):
    calls, _ = cli
    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    edges = {(a["fromState"], a["toState"])
             for a in every(calls, "add_animator_transition")}
    assert ("Idle", "Walk") in edges
    assert ("AnyState", "Attack") in edges
    assert len(result.transitions) == 7


def test_the_conditions_go_over_as_json(project, cli):
    calls, _ = cli
    pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    walk = [a for a in every(calls, "add_animator_transition")
            if a["fromState"] == "Idle"][0]
    conditions = json.loads(walk["conditions"])
    assert conditions == [{"parameter": "speed", "mode": "Greater",
                           "threshold": 0.1}]


def test_it_adds_parameters(project, cli):
    calls, _ = cli
    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    added = {a["name"]: a["type"] for a in every(calls, "add_animator_parameter")}
    assert added == {"speed": "Float", "attack": "Trigger"}
    assert result.parameters == ["speed", "attack"]


def test_parameters_are_added_before_transitions(project, cli):
    """A condition names a parameter and the command checks it exists.
    The other order builds a controller and then refuses every
    transition."""
    calls, _ = cli
    pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    order = commands(calls)
    assert order.index("add_animator_parameter") < \
        order.index("add_animator_transition")


def test_states_are_added_before_transitions(project, cli):
    calls, _ = cli
    pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    order = commands(calls)
    assert order.index("add_animator_state") < \
        order.index("add_animator_transition")


def test_clips_are_made_before_the_states_that_reference_them(project, cli):
    calls, _ = cli
    pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    order = commands(calls)
    assert order.index("create_animation_clip") < \
        order.index("add_animator_state")


# ======================================================
# 5. Attaching it
# ======================================================

def test_it_attaches_controller_to_prefab(project, cli):
    calls, _ = cli
    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    attached = every(calls, "set_component_properties")
    assert attached, "the controller was never attached"
    assert attached[0]["type"] == "Animator"
    properties = json.loads(attached[0]["properties"])
    assert properties["m_Controller"] == {
        "path": "Assets/ARIA/Animators/Hero.controller"}
    assert "attach_controller" in result.steps


def test_the_controller_is_attached_by_object_reference_not_a_string():
    """set_component_properties assigns an object reference from a
    handle-shaped value. A bare string is not one."""
    import inspect
    source = inspect.getsource(pipeline.attach_controller)

    assert '{"path": controller_path}' in source or \
        "{CONTROLLER_PROPERTY: {\"path\": controller_path}}" in source


def test_it_attaches_only_after_the_controller_exists(project, cli):
    calls, _ = cli
    pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    order = commands(calls)
    assert order.index("create_animator_controller") < \
        order.index("set_component_properties")


# ======================================================
# 6. The humanoid gate
# ======================================================

def test_it_accepts_generic(project, cli):
    """The correction. A clip of bone-local transform curves sampled
    against a Humanoid import moved NOTHING; the same clip against the
    same model imported Generic drives it. Demanding Humanoid would
    refuse exactly the setup that works."""
    calls, _ = cli
    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    assert result.success is True
    assert result.import_type == pipeline.GENERIC
    assert "validate_import_type" in result.steps


def test_it_accepts_humanoid_too(project, cli):
    calls, state = cli
    state["avatar"] = HUMANOID_OK

    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    assert result.success is True
    assert result.import_type == pipeline.HUMANOID
    assert result.avatar_valid is True


def test_generic_is_warned_about_because_it_will_not_retarget(project, cli):
    """The warning that used to point the other way. Mixamo clips
    retarget through the Avatar system, which needs Humanoid on both
    sides -- so Generic is now the setting that quietly produces a
    character standing perfectly still.

    Accepted rather than refused: the controller, states, transitions
    and parameters are all still correct, and a re-import is a
    one-line fix.
    """
    calls, _ = cli
    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    assert result.success is True
    assert result.import_type == pipeline.GENERIC
    assert any("Avatar system" in w for w in result.warnings)


def test_humanoid_is_not_warned_about(project, cli):
    calls, state = cli
    state["avatar"] = HUMANOID_OK

    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    assert not any("Re-import" in w for w in result.warnings)


def test_the_gate_runs_before_anything_is_built(project, cli):
    calls, _ = cli
    pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    assert commands(calls)[0] == "eval", "it built before it checked"


def test_it_rejects_a_humanoid_with_an_invalid_avatar(project, cli):
    """Measured during the rigging work: a rigged FBX imports Generic
    with zero avatars unless told otherwise, and everything downstream
    then succeeds while nothing moves."""
    calls, state = cli
    state["avatar"] = ("hasAvatar=True isHuman=True isValid=False bones=48 "
                       "controller=none")

    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    assert result.success is False
    assert "Avatar is invalid" in result.error
    assert "create_animator_controller" not in commands(calls)


def test_it_rejects_a_model_with_no_skeleton(project, cli):
    calls, state = cli
    state["avatar"] = ("hasAvatar=False isHuman=False isValid=False bones=0 "
                       "controller=none")

    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    assert result.success is False
    assert "no skeleton" in result.error
    assert "create_animator_controller" not in commands(calls)


def test_a_handful_of_transforms_is_not_a_skeleton(project, cli):
    calls, state = cli
    state["avatar"] = ("hasAvatar=False isHuman=False isValid=False bones=2 "
                       "controller=none")

    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    assert result.success is False
    assert "not a skeleton" in result.error


def test_the_bone_floor_is_low_enough_for_a_simple_character():
    """Tuned to the 48-transform rig measured here, it would refuse
    every simpler character somebody rigs later."""
    assert pipeline.MINIMUM_BONES <= 8


def test_a_missing_animator_is_still_refused(project, cli):
    calls, state = cli
    state["avatar"] = "NO_ANIMATOR"

    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    assert result.success is False
    assert "no Animator component" in result.error


def test_an_object_that_is_not_in_the_scene_is_refused(project, cli):
    calls, state = cli
    state["avatar"] = "NO_OBJECT"

    result = pipeline.animate_character("p.prefab", instance="Ghost", name="Ghost")

    assert result.success is False
    assert "no object called 'Ghost'" in result.error


# ======================================================
# 7. Reuse
# ======================================================

def test_it_reuses_animation_pipeline(project, cli):
    calls, _ = cli
    controller = project / "Assets" / "ARIA" / "Animators"
    controller.mkdir(parents=True)
    (controller / "Hero.controller").write_text("already built")

    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    assert result.success is True
    assert result.reused is True
    assert "create_animator_controller" not in commands(calls)


def test_a_reused_controller_is_still_attached(project, cli):
    """The controller existing does not mean this character has it."""
    calls, _ = cli
    controller = project / "Assets" / "ARIA" / "Animators"
    controller.mkdir(parents=True)
    (controller / "Hero.controller").write_text("already built")

    pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    assert "set_component_properties" in commands(calls)


def test_reuse_can_be_switched_off(project, cli):
    calls, _ = cli
    controller = project / "Assets" / "ARIA" / "Animators"
    controller.mkdir(parents=True)
    (controller / "Hero.controller").write_text("already built")

    pipeline.animate_character("p.prefab", instance="Hero", name="Hero",
                               reuse=False)

    assert "create_animator_controller" in commands(calls)


def test_replacing_an_existing_controller_is_reported(project, cli):
    calls, state = cli
    state["avatar"] = ("hasAvatar=False isHuman=False isValid=False "
                       "bones=48 controller=OldController")

    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    assert any("OldController" in w for w in result.warnings)


# ======================================================
# 8. Ceilings
# ======================================================

def test_it_respects_animation_ceilings(project, cli):
    """Nothing here spends money -- the clips come from Unity, not
    from Ludo -- so the ceiling that matters is the graph's own size.
    A loop that adds states adds them to somebody's project."""
    calls, _ = cli
    too_many = [f"State{index}" for index in range(graphs.MAX_STATES + 1)]

    result = pipeline.animate_character("p.prefab", instance="Hero",
                                        name="Hero", required_states=too_many)

    assert result.success is False
    assert "more than the" in result.error
    assert "create_animator_controller" not in commands(calls)


def test_the_transition_ceiling_is_checked_too():
    graph = graphs.Graph(
        states=[graphs.State("Idle", is_default=True)],
        transitions=[graphs.Transition("Idle", "Idle", has_exit_time=True)]
        * (graphs.MAX_TRANSITIONS + 1))

    problems = graphs.validate_graph(graph, params.DEFAULT_PARAMETERS)
    assert any("transitions is more than" in p for p in problems)


def test_the_ceiling_is_checked_before_unity_is_asked_anything(project, cli):
    calls, _ = cli
    pipeline.animate_character("p.prefab", instance="Hero", name="Hero",
                               required_states=[f"S{i}" for i in range(40)])

    assert "add_animator_state" not in commands(calls)


# ======================================================
# 9. Failure surfaces
# ======================================================

def _fails(monkeypatch, failing, message, avatar=AVATAR_OK):
    from backend.unity import unity_cli_engine
    calls = []

    def fake(command, args=None, **kwargs):
        argv = list(args or [])
        named = {argv[i].lstrip("-"): argv[i + 1]
                 for i in range(0, len(argv) - 1, 2) if argv[i].startswith("--")}
        name = command.replace("cmd ", "")
        calls.append({"command": name, "args": named})
        if name == failing:
            return {"success": False, "output": "", "json": None,
                    "error": message}
        result = {"success": True, "result": avatar} if name == "eval" else \
            {"assetPath": named.get("path")}
        return {"success": True, "output": "", "error": None,
                "json": {"success": True, "errors": [],
                         "data": {"command": name, "parameters": named,
                                  "result": result, "target": {},
                                  "success": True}}}

    monkeypatch.setattr(unity_cli_engine, "run_invocation", fake)
    return calls


def test_failure_surface_animation(project, monkeypatch):
    calls = _fails(monkeypatch, "create_animator_controller", "disk full")

    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    assert result.success is False
    assert "could not create the controller" in result.error
    assert "disk full" in result.error
    assert "set_component_properties" not in commands(calls)


def test_a_refused_parameter_stops_before_the_transitions(project, monkeypatch):
    calls = _fails(monkeypatch, "add_animator_parameter", "bad type")

    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    assert result.success is False
    assert "refused the parameter" in result.error
    assert "add_animator_transition" not in commands(calls)


def test_a_refused_state_says_which_state(project, monkeypatch):
    _fails(monkeypatch, "add_animator_state", "name already used")

    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    assert "refused the state 'Idle'" in result.error


def test_a_refused_transition_says_which_edge(project, monkeypatch):
    _fails(monkeypatch, "add_animator_transition", "no such parameter")

    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    assert "refused the transition Idle->Walk" in result.error


def test_a_controller_that_cannot_be_attached_is_not_reported_as_working(
        project, monkeypatch):
    """It was built. Saying the character is animated when nothing
    points at the controller is the one thing this must not do."""
    _fails(monkeypatch, "set_component_properties", "component is missing")

    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    assert result.success is False
    assert "could not be attached" in result.error
    assert result.controller_path


def test_a_clip_that_will_not_be_made_is_a_warning_not_a_failure(
        project, monkeypatch):
    """The controller and its graph are still worth having; the state
    simply plays nothing, and that is said rather than hidden."""
    _fails(monkeypatch, "create_animation_clip", "folder is read-only")

    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    assert result.success is True
    assert any("could not make a clip" in w for w in result.warnings)


# ======================================================
# The answer
# ======================================================

def test_the_result_has_every_field_the_specification_asks_for(project, cli):
    result = pipeline.animate_character("p.prefab", instance="Hero", name="Hero")

    for field in ("prefab_path", "controller_path", "states", "transitions",
                  "parameters", "avatar_valid", "reused", "steps", "warnings"):
        assert hasattr(result, field), field


def test_an_incomplete_graph_is_asked_for_explicitly_and_warned_about(
        project, cli):
    result = pipeline.animate_character("p.prefab", instance="Hero",
                                        name="Hero", complete_graph=False)

    assert result.success is True
    assert any("nothing leaves 'Attack'" in w for w in result.warnings)


def test_no_prefab_is_refused_before_anything_runs(project, cli):
    calls, _ = cli
    result = pipeline.animate_character("")

    assert result.success is False
    assert result.ran is False
    assert calls == []


# ======================================================
# Where the clips come from
#
# A generated-motion path used to live here and was removed. Ludo's
# walk had both thighs swinging the same way at every frame -- a
# shuffle, not a walk -- and the Mixamo clip measured the same way
# gives a 44.3 degree leg split against the generated 6.1. The clips
# come from mixamo_library now, and this pipeline builds the machine
# that plays them.
# ======================================================

def test_states_are_empty_unless_clips_are_supplied(project, cli):
    """A controller with empty states is still a correct controller --
    the states, transitions and parameters are all real. What an empty
    state means is that it plays nothing, and that is said."""
    result = pipeline.animate_character("p.prefab", instance="Hero",
                                        name="Hero")

    assert result.success is True
    assert any("plays nothing" in w for w in result.warnings)
    assert any("mixamo_library" in w for w in result.warnings)


def test_supplied_clips_reach_the_states(project, cli):
    calls, _ = cli
    pipeline.animate_character(
        "p.prefab", instance="Hero", name="Hero",
        clips={"Idle": "Assets/ARIA/Idle.anim",
               "Walk": "Assets/ARIA/Walking.anim"})

    motions = {a["name"]: a.get("motion")
               for a in every(calls, "add_animator_state")}
    assert motions["Idle"] == "Assets/ARIA/Idle.anim"
    assert motions["Walk"] == "Assets/ARIA/Walking.anim"


def test_supplying_clips_removes_the_empty_warning(project, cli):
    result = pipeline.animate_character(
        "p.prefab", instance="Hero", name="Hero",
        clips={"Idle": "Assets/Mixamo/Idle.fbx"})

    assert not any("plays nothing" in w for w in result.warnings)


def test_nothing_here_spends_a_credit(project, cli):
    """The pipeline used to buy a clip per state. It does not any more.

    Checked on the IMPORTS rather than the text: the module docstring
    explains at length why the Ludo path was removed, and a search for
    the word would match the explanation.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(pipeline))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)

    assert not [m for m in imported if "ludo" in m], sorted(imported)


def test_the_result_carries_the_clips_and_the_import_type(project, cli):
    result = pipeline.animate_character("p.prefab", instance="Hero",
                                        name="Hero")

    for field in ("prefab_path", "controller_path", "clips", "states",
                  "transitions", "parameters", "import_type", "reused",
                  "steps", "warnings"):
        assert hasattr(result, field), field
    assert result.import_type in (pipeline.GENERIC, pipeline.HUMANOID)


# ======================================================
# The states beyond the first four
#
# Eleven in all, built live and verified in the Editor: default Idle,
# 21 transitions, 5 of them from AnyState, every state carrying a
# humanoid clip and none without an exit.
# ======================================================

def test_every_known_state_is_wired():
    graph = graphs.default_graph(graphs.ALL_STATES)
    parameters = params.parameters_for(graphs.ALL_STATES)

    assert len(graph.states) == 11
    assert graphs.validate_graph(graph, parameters) == []
    assert graphs.graph_advice(graph) == [], "a state with no way out"


def test_a_subset_builds_only_the_edges_it_can():
    """Passing fewer states is normal. An edge whose endpoints are not
    both present is simply not built, so the four-state graph is
    exactly what it always was."""
    four = graphs.default_graph(("Idle", "Walk", "Run", "Attack"))

    assert len(four.states) == 4
    assert len(four.transitions) == 7
    assert graphs.validate_graph(four, params.DEFAULT_PARAMETERS) == []


def test_parameters_follow_the_states():
    """A graph is refused for naming a parameter nobody declared, so
    asking a caller to keep a second list in step is asking them to
    get it wrong."""
    assert [p.name for p in params.parameters_for(("Idle", "Walk"))] == ["speed"]
    assert [p.name for p in params.parameters_for(graphs.ALL_STATES)] == [
        "speed", "attack", "crouch", "jump", "grounded", "landForce",
        "dodgeLeft", "dodgeRight"]


def test_grounded_starts_true():
    """A Bool starts false, so a character whose graph has a Fall state
    would begin the game falling -- from Idle, on the first frame,
    before anything set it."""
    assert params.GROUNDED.type == "Bool"
    assert params.GROUNDED.default is True


def test_falling_is_entered_from_the_grounded_states_not_from_anystate():
    """A character can leave the ground without jumping, so Fall is
    driven by `grounded` rather than a trigger. But NOT from AnyState:

        AnyState -> Fall on !grounded fires the frame after a jump
        leaves the ground and overrides the Jump state immediately.
        Reported as "while running, if I jump, he does the falling
        animation" -- the jump played for about one frame.

    So it is entered from the grounded states, which is where walking
    off a ledge actually happens, and a jump reaches it through
    Jump -> Fall on exit time.
    """
    graph = graphs.default_graph(graphs.ALL_STATES)
    into_fall = {t.source for t in graph.transitions if t.target == "Fall"}

    assert graphs.ANY_STATE not in into_fall,         "AnyState -> Fall interrupts the jump on its first airborne frame"
    assert {"Idle", "Walk", "Run", "Crouch", "CrouchWalk"} <= into_fall
    assert "Jump" in into_fall, "a jump has to reach the fall somehow"

    grounded_edge = [t for t in graph.transitions
                     if t.source == "Walk" and t.target == "Fall"][0]
    assert grounded_edge.conditions[0].parameter == "grounded"
    assert grounded_edge.conditions[0].mode == "IfNot"


def test_anystate_transitions_do_not_interrupt_themselves():
    """Unity's default is true, which restarts the destination clip on
    every frame the condition holds."""
    graph = graphs.default_graph(graphs.ALL_STATES)

    for transition in graph.transitions:
        if transition.source == graphs.ANY_STATE:
            assert transition.can_transition_to_self is False, transition.target


def test_standing_up_while_moving_goes_to_walk_not_idle():
    """A character that stops dead because it stood up is a bug
    somebody spends an afternoon on."""
    graph = graphs.default_graph(graphs.ALL_STATES)
    out = {(t.source, t.target) for t in graph.transitions}

    assert ("CrouchWalk", "Walk") in out


def test_a_jump_still_ends_when_there_is_no_fall_state():
    """Jump leaves on exit time into Fall. Without a Fall state that
    edge cannot be built, and a jump with nowhere to go is a character
    stuck in the air."""
    graph = graphs.default_graph(("Idle", "Jump"))
    out = {(t.source, t.target) for t in graph.transitions}

    assert ("Jump", "Idle") in out
    # Idle has nowhere to go in a two-state graph, which is true and
    # not what this test is about.
    assert not any("Jump" in a for a in graphs.graph_advice(graph))


def test_falling_still_ends_when_there_is_no_land_state():
    graph = graphs.default_graph(("Idle", "Fall"))

    assert ("Fall", "Idle") in {(t.source, t.target) for t in graph.transitions}
    assert not any("Fall" in a for a in graphs.graph_advice(graph))


def test_a_model_file_is_not_offered_as_a_motion(project, cli):
    """add_animator_state resolves an FBX path to the model, not to the
    clip inside it, and fails the whole build on the first state:
    "resolved to a GameObject, not an AnimationClip". The state is
    created bare and assign_clips attaches the sub-asset."""
    calls, _ = cli
    result = pipeline.animate_character(
        "p.prefab", instance="Hero", name="Hero",
        clips={"Idle": "Assets/Mixamo/X Bot@Standing Idle.fbx"})

    idle = [a for a in every(calls, "add_animator_state")
            if a["name"] == "Idle"][0]
    assert "motion" not in idle
    assert any("assign_clips" in w for w in result.warnings)


def test_a_real_anim_asset_is_offered_as_a_motion(project, cli):
    calls, _ = cli
    pipeline.animate_character("p.prefab", instance="Hero", name="Hero",
                               clips={"Idle": "Assets/ARIA/Idle.anim"})

    idle = [a for a in every(calls, "add_animator_state")
            if a["name"] == "Idle"][0]
    assert idle["motion"] == "Assets/ARIA/Idle.anim"


def test_no_placeholder_clips_are_made_when_real_ones_are_supplied(
        project, cli):
    """Otherwise eleven empty .anim files land in somebody's project
    and are never played."""
    calls, _ = cli
    pipeline.animate_character(
        "p.prefab", instance="Hero", name="Hero",
        clips={"Idle": "Assets/Mixamo/X Bot@Standing Idle.fbx"})

    assert "create_animation_clip" not in commands(calls)


# ======================================================
# Leaving the jump
# ======================================================

def test_a_finished_jump_goes_back_to_standing_not_to_a_landing():
    """MEASURED, as root height sampled across each jump clip:

        Jump          0.00 .41 .78 1.01 1.02 .76 .35 .06 .02 .01 0.00
        Running Jump  0.00 .23 .37 0.46 0.47 .40 .27 .10 .02 .00 0.00
        Jumping      0.00 -.15 -.28 0.26 0.66 .48 -.11 -.31 -.11 .00 0.00

    Each one rises, comes down and settles inside its own length: the
    landing is already in the clip. Sending a finished jump on to Land
    played a second one, from clips authored for falling from a height
    -- Landing descends 2.30m, Falling To Roll 2.01m.
    """
    graph = graphs.default_graph(graphs.ALL_STATES)
    leaving = [t for t in graph.transitions if t.source == "Jump"]

    assert "Land" not in {t.target for t in leaving}, (
        "the jump clip lands by itself; a second landing is the bug")

    home = [t for t in leaving if t.target == "Idle"]
    assert home, "a finished jump has to hand control back"
    assert [(c.parameter, c.mode) for c in home[0].conditions] == [
        ("grounded", "If")]
    assert home[0].exit_time >= 0.8, (
        "leaving early cuts off the landing the clip contains")


def test_a_jump_that_is_still_in_the_air_falls():
    """Jumping off a ledge: the clip runs out with nothing under him."""
    graph = graphs.default_graph(graphs.ALL_STATES)
    to_fall = [t for t in graph.transitions
               if t.source == "Jump" and t.target == "Fall"]

    assert to_fall
    assert [(c.parameter, c.mode) for c in to_fall[0].conditions] == [
        ("grounded", "IfNot")]


def test_only_a_fall_reaches_the_landing():
    """Land exists for a drop the character did not jump into. Every
    other way in plays a from-height landing on flat ground."""
    graph = graphs.default_graph(graphs.ALL_STATES)
    into_land = {t.source for t in graph.transitions if t.target == "Land"}

    assert into_land == {"Fall"}, f"something else reaches Land: {into_land}"


def test_no_edge_out_of_the_jump_runs_on_exit_time_alone():
    graph = graphs.default_graph(graphs.ALL_STATES)

    unconditional = [t for t in graph.transitions
                     if t.source == "Jump" and not t.conditions]
    assert not unconditional, (
        "an edge out of Jump with no condition fires whether or not the "
        f"character has landed: {[t.target for t in unconditional]}")


def test_a_jump_ends_even_with_nowhere_to_stand():
    """Without an Idle to return to there is no hand-back edge, and a
    jump with nowhere to go is a character stuck in the air."""
    graph = graphs.default_graph(("Jump", "Fall"))
    out = {(t.source, t.target) for t in graph.transitions}

    assert ("Jump", "Fall") in out
    assert not any("Jump" in a for a in graphs.graph_advice(graph))


def test_a_graph_with_a_landing_but_no_fall_does_not_strand_the_jump():
    graph = graphs.default_graph(("Idle", "Jump", "Land"))
    out = {(t.source, t.target) for t in graph.transitions}

    assert ("Jump", "Idle") in out
    assert ("Jump", "Land") not in out
    assert not any("Jump" in a for a in graphs.graph_advice(graph))
