"""Bridge call syntax reaches the open editor, and never a model.

THE FAILURE THIS EXISTS FOR
---------------------------
Measured on the developer's machine. Typed into chat:

    OpenScene("Assets/Scenes/SampleScene.unity")
    SaveScene("Assets/Scenes/WorkshopScene.unity")
    DeleteGameObject("Main Camera")
    DeleteGameObject("Directional Light")

aria/unity_editor_bridge.py had shipped the day before with every one of
those commands implemented, and nothing in the chat path imported it. So
the turn went to nemo-12b, which recognised the only shape it knew -- a
file path -- and started WRITING Assets/Scenes/SampleScene.unity as
generated scene YAML. The packet log shows

    progress {"label":"writing Assets/Scenes/SampleScene.unity"}

and then the turn ending in a truncation notice. It ran out of context
before it finished. Had it not, a real scene would have been overwritten
with invented text and the user told it had been edited.

So these tests assert the two halves that matter: a message that is
nothing but bridge calls is answered by the editor, and every answer that
did not reach the editor says so in its first sentence.
"""

from __future__ import annotations

import json

import pytest

from aria import unity_editor_bridge as ueb
from backend.core import turn_orchestrator
from backend.core.turn_types import SessionState, TurnRequest
from backend.unity import unity_editor_actions as actions


# The message from the packet log, verbatim.
THE_MESSAGE = (
    'OpenScene("Assets/Scenes/SampleScene.unity")\n'
    'SaveScene("Assets/Scenes/WorkshopScene.unity")\n'
    'DeleteGameObject("Main Camera")\n'
    'DeleteGameObject("Directional Light")'
)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "Proj"
    (root / "Assets").mkdir(parents=True)
    return root


@pytest.fixture
def bridge(project):
    made = ueb.UnityEditorBridge(project, timeout=3, wait=False, poll_interval=0.01)
    made.install_bridge()
    return made


class Answer:
    """One CommandResult, without the editor that would have made it."""

    def __init__(self, success=True, message="done"):
        self.success = success
        self.message = message
        self.data = {}


class Stub:
    """A bridge whose send() does whatever the test needs it to."""

    def __init__(self, tmp_path, outcome=None, installed=True):
        self.project_root = tmp_path
        self.commands_path = tmp_path / "ARIA" / "unity_commands.json"
        self.timeout = 60.0
        self.outcome = outcome
        self.installed = installed
        self.sent = None

    def is_installed(self):
        return self.installed

    def send(self, commands, **_kwargs):
        self.sent = list(commands)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def results_for(*answers):
    return ueb.BridgeResults(id="x", ok=True, message="", results=list(answers))


# ======================================================
# The tables stay level with the bridge
# ======================================================

def test_every_bridge_command_can_be_typed():
    """A command added to the bridge and forgotten here fails here."""
    assert set(actions.ARGUMENTS) == set(ueb.COMMANDS)
    assert set(actions.POSITIONAL) == set(ueb.COMMANDS)


def test_positional_names_are_all_real_arguments():
    for name, order in actions.POSITIONAL.items():
        assert set(order) <= set(actions.ARGUMENTS[name]), name


# ======================================================
# Reading the message
# ======================================================

def test_the_message_from_the_log_parses():
    assert actions.parse_bridge_calls(THE_MESSAGE) == [
        {"command": "OpenScene", "args": {"path": "Assets/Scenes/SampleScene.unity"}},
        {"command": "SaveScene", "args": {"path": "Assets/Scenes/WorkshopScene.unity"}},
        {"command": "DeleteGameObject", "args": {"target": "Main Camera"}},
        {"command": "DeleteGameObject", "args": {"target": "Directional Light"}},
    ]


@pytest.mark.parametrize("text", [
    "",
    "How do I open a scene in Unity?",
    "What does OpenScene do?",
    'Open the scene, like OpenScene("Assets/Scenes/Main.unity"), and tell me more.',
    "OpenScene",
    "print('hello')",
])
def test_anything_that_is_not_a_list_of_calls_is_handed_back(text):
    """None hands the turn to a model, which is right for all of these."""
    assert actions.parse_bridge_calls(text) is None


def test_a_message_that_is_half_prose_is_refused_rather_than_passed_on():
    """Ours, and unanswerable. Passing it on is what wrote a scene."""
    with pytest.raises(actions.Unmappable, match="not both"):
        actions.parse_bridge_calls(
            'OpenScene("Assets/Scenes/Main.unity")\nand then tell me what is in it')


def test_a_fenced_block_is_still_a_list_of_calls():
    fenced = '```\nDeleteGameObject("Main Camera")\n```'
    assert actions.parse_bridge_calls(fenced) == [
        {"command": "DeleteGameObject", "args": {"target": "Main Camera"}}]


def test_keywords_are_accepted_in_either_spelling():
    both = actions.parse_bridge_calls(
        'AddComponent("Crate", component_type="Rigidbody")\n'
        'AddComponent("Crate", componentType="Rigidbody")')
    assert both[0] == both[1]
    assert both[0]["args"]["componentType"] == "Rigidbody"


def test_a_semicolon_separates_calls_too():
    assert len(actions.parse_bridge_calls('Ping(); Ping()')) == 2


@pytest.mark.parametrize("text, says", [
    # The message is parsed, never executed.
    ('OpenScene(__import__("os").getcwd())', "would have to run"),
    ('DeleteGameObject(open("x").read())', "would have to run"),
    ('OpenScene(*paths)', "unpacks its arguments"),
    ('OpenScene(**options)', "unpacks its keywords"),
    ('OpenScene(path="a.unity", colour="red")', "has no colour argument"),
    ('OpenScene("a.unity", path="b.unity")', "given path twice"),
    ('DeleteGameObject("a", "b", "c")', "reads at most 1 unnamed value"),
])
def test_a_call_i_cannot_read_is_refused_by_name(text, says):
    """Refused, not returned as None: None would hand it to a model."""
    with pytest.raises(actions.Unmappable, match=says):
        actions.parse_bridge_calls(text)


def test_a_null_field_value_survives():
    """SetField(..., None) clears a reference; it does not omit the argument."""
    command = actions.parse_bridge_calls('SetField("Crate", "Rigidbody", "mass", None)')[0]
    assert command["args"] == {"target": "Crate", "componentType": "Rigidbody",
                               "field": "mass", "value": None}


# ======================================================
# The second one, which got past the first fix
#
# The short-circuit was in and working, and this still reached a model
# and started writing Assets/Scenes/WorkshopScene.unity. One statement
# would not map, the whole message was disowned, and disowning meant
# "let a model have it".
# ======================================================

THE_SECOND_MESSAGE = (
    'CreateCamera("MainCamera", position={"x":0,"y":1.6,"z":-3}, '
    'rotation={"x":10,"y":0,"z":0})\n'
    'CreateLight("DirectionalLight", type="Directional", intensity=1.2)'
)


def test_the_second_message_maps_now_that_the_light_takes_a_name_first():
    """Refusing this was the right answer to the wrong question.

    The message is not wrong. Every Create* line in it puts the name
    first, which is what CreateGameObject and CreateCamera do; only
    CreateLight read that value as the light type. The refusal explained
    itself well and the person deleted the light -- twice -- and then
    said they could not add any lights at all.
    """
    assert actions.parse_bridge_calls(THE_SECOND_MESSAGE) == [
        {"command": "CreateCamera",
         "args": {"name": "MainCamera",
                  "position": {"x": 0, "y": 1.6, "z": -3},
                  "rotation": {"x": 10, "y": 0, "z": 0}}},
        {"command": "CreateLight",
         "args": {"name": "DirectionalLight", "type": "Directional",
                  "intensity": 1.2}},
    ]


@pytest.mark.parametrize("text, expected", [
    # The two lines that were actually deleted from a real session.
    ('CreateLight("WorkshopLamp", type="Point", position={"x":0,"y":3,"z":0}, '
     'intensity=3.0, range=8)',
     {"name": "WorkshopLamp", "type": "Point", "position": {"x": 0, "y": 3, "z": 0},
      "intensity": 3.0, "range": 8}),
    ('CreateLight("PortalGlow", type="Point", position={"x":1.5,"y":2.5,"z":2}, '
     'intensity=2.5, range=4)',
     {"name": "PortalGlow", "type": "Point",
      "position": {"x": 1.5, "y": 2.5, "z": 2}, "intensity": 2.5, "range": 4}),
    # And the fully named form, which never stopped working.
    ('CreateLight(type="Directional", name="DirectionalLight", intensity=1.2)',
     {"type": "Directional", "name": "DirectionalLight", "intensity": 1.2}),
])
def test_the_lights_from_that_session(text, expected):
    assert actions.parse_bridge_calls(text) == [
        {"command": "CreateLight", "args": expected}]


def test_every_create_reads_its_first_unnamed_value_as_the_name():
    """The rule, stated once, so a fifth Create cannot quietly differ."""
    for name in ("CreateGameObject", "CreateCamera", "CreateLight"):
        assert actions.POSITIONAL[name][0] == "name", name
    # InstantiatePrefab is the deliberate exception: what it makes is
    # identified by the asset it comes from, and the name is optional.
    assert actions.POSITIONAL["InstantiatePrefab"][0] == "prefabPath"


def test_a_value_really_given_twice_is_still_refused():
    """The collision check did not go away with the signature that caused it."""
    with pytest.raises(actions.Unmappable) as refused:
        actions.parse_bridge_calls('CreateLight("Lamp", name="Other")')

    said = str(refused.value)
    assert "CreateLight was given name twice" in said
    assert "name, type, position, rotation, intensity, color" in said


def test_the_refusal_shows_both_values_and_guesses_nothing():
    """It used to end by writing out the call the person probably meant.

    That was right while CreateLight's signature was the trap: the
    displaced value could only have been the name, so moving it there was
    safe. With the signature fixed, a collision is a real mistake and the
    next free slot is not a safe guess -- for CreateGameObject the free
    slot is `parent`, and suggesting parent="Crate" produces a call that
    RUNS and quietly parents the object to something nobody named.
    """
    with pytest.raises(actions.Unmappable) as refused:
        actions.parse_bridge_calls(
            'CreateGameObject("Crate", name="Box", primitive="Cube")')

    said = str(refused.value)
    assert '"Crate"' in said and '"Box"' in said, "show both, so the conflict is visible"
    assert "Give name once." in said
    assert "parent=" not in said, "a guessed slot is a call that runs and is wrong"


def test_a_camera_with_dict_vectors_maps():
    """The half of that message that was always fine."""
    assert actions.parse_bridge_calls(
        'CreateCamera("MainCamera", position={"x":0,"y":1.6,"z":-3})'
    ) == [{"command": "CreateCamera",
           "args": {"name": "MainCamera", "position": {"x": 0, "y": 1.6, "z": -3}}}]


# ======================================================
# The third one: a call that does not parse is still a call
#
# The refusal above ends "CreateLight(type=..., name=..., position=...)".
# A person read that as the shape to type and pasted it, ellipsis and
# all -- and a positional argument after keyword arguments is a Python
# SyntaxError. Ownership was decided by ast.parse succeeding, so the line
# was disowned, and phi-3-mini answered "Light created at specified
# position and rotation." Nothing was created.
# ======================================================

THE_PASTED_TEMPLATE = 'CreateLight(type="Directional", name="DirectionalLight", ...)'


def test_a_call_that_does_not_parse_is_still_this_layers_business():
    with pytest.raises(actions.Unmappable, match="placeholder"):
        actions.parse_bridge_calls(THE_PASTED_TEMPLATE)


def test_the_pasted_template_never_reaches_a_model(tmp_path):
    answer = actions.answer_request(THE_PASTED_TEMPLATE, bridge=Stub(tmp_path))

    assert answer is not None, "None is what let phi-3 claim it made a light"
    assert answer["ran"] is False


def test_an_unfinished_call_says_it_could_not_be_read():
    with pytest.raises(actions.Unmappable, match="could not read"):
        actions.parse_bridge_calls('CreateLight("x", type="y"')


def test_a_call_written_across_lines_is_one_call():
    """Paste formatted code and the formatting comes with it."""
    assert actions.parse_bridge_calls(
        'CreateCamera("MainCamera",\n'
        '             position={"x":0,"y":1.6,"z":-3},\n'
        '             rotation={"x":10,"y":0,"z":0})'
    ) == [{"command": "CreateCamera",
           "args": {"name": "MainCamera",
                    "position": {"x": 0, "y": 1.6, "z": -3},
                    "rotation": {"x": 10, "y": 0, "z": 0}}}]


def test_calls_separated_by_commas_are_still_separate():
    """The other thing a trailing comma means, and both have to work."""
    assert actions.parse_bridge_calls('Ping(),\nPing()') == [
        {"command": "Ping", "args": {}}, {"command": "Ping", "args": {}}]


def test_prose_is_never_swallowed_into_the_line_above_it():
    """Continuation is only attempted for a piece that names a command."""
    with pytest.raises(actions.Unmappable, match="not both"):
        actions.parse_bridge_calls('Ping()\nnow tell me what happened')


# ======================================================
# What reaches the editor
# ======================================================

def test_the_commands_file_holds_what_was_typed(bridge):
    """End to end through the real bridge, with nobody there to answer."""
    answer = actions.answer_request(THE_MESSAGE, bridge=bridge)

    assert answer["ran"] is False, "wait is off, so nothing has run yet"
    written = json.loads(bridge.commands_path.read_text(encoding="utf-8"))
    assert [entry["command"] for entry in written["commands"]] == [
        "OpenScene", "SaveScene", "DeleteGameObject", "DeleteGameObject"]
    assert written["commands"][0]["args"]["path"] == "Assets/Scenes/SampleScene.unity"


def test_no_scene_file_is_ever_written(bridge):
    """The failure in the log, asserted directly."""
    actions.answer_request(THE_MESSAGE, bridge=bridge)

    scene = bridge.project_root / "Assets" / "Scenes" / "SampleScene.unity"
    assert not scene.exists()


def test_a_good_exchange_reports_every_command(tmp_path):
    stub = Stub(tmp_path, outcome=results_for(Answer(), Answer(message="saved")))
    answer = actions.answer_request(
        'OpenScene("Assets/Scenes/Main.unity")\nSaveScene()', bridge=stub)

    assert answer["ran"] is True
    assert "Ran 2 commands" in answer["text"]
    assert "OpenScene Assets/Scenes/Main.unity" in answer["text"]
    assert "saved" in answer["text"]


# ======================================================
# Every answer that did not reach the editor says so
# ======================================================

def test_an_uninstalled_bridge_says_nothing_changed(project):
    plain = ueb.UnityEditorBridge(project, timeout=1, poll_interval=0.01)
    answer = actions.answer_request(THE_MESSAGE, bridge=plain)

    assert answer["ran"] is False
    assert answer["text"].startswith("I did not send anything to Unity")
    assert "--install" in answer["text"]


def test_silence_from_unity_is_not_reported_as_success(tmp_path):
    stub = Stub(tmp_path, outcome=ueb.UnityBridgeTimeout("nothing answered"))
    answer = actions.answer_request('DeleteGameObject("Main Camera")', bridge=stub)

    assert answer["ran"] is False
    assert "did not answer" in answer["text"]
    # The file is still there, so the next step is not "ask again".
    assert "ARIA > Run Bridge Commands" in answer["text"]


def test_a_refusal_names_what_landed_and_what_did_not(tmp_path):
    stopped = ueb.UnityBridgeError(
        "DeleteGameObject (command 2 of 3) failed: no such object",
        command="DeleteGameObject", index=1,
        results=[Answer(), Answer(success=False, message="no such object")])
    stub = Stub(tmp_path, outcome=stopped)

    answer = actions.answer_request(
        'OpenScene("Assets/Scenes/Main.unity")\n'
        'DeleteGameObject("Ghost")\n'
        'SaveScene()', bridge=stub)

    assert answer["ran"] is True
    assert "ok  OpenScene Assets/Scenes/Main.unity" in answer["text"]
    assert "FAILED  DeleteGameObject Ghost: no such object" in answer["text"]
    assert "not reached  SaveScene" in answer["text"]


def test_no_unity_project_is_an_answer_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setenv(ueb.ENV_PROJECT, str(tmp_path / "nowhere"))
    monkeypatch.setattr(ueb, "_default", None, raising=False)

    answer = actions.answer_request(THE_MESSAGE)

    assert answer["ran"] is False
    assert answer["text"].startswith("I did not send anything to Unity")


# ======================================================
# The plain-language door, for the registered tool
# ======================================================

def test_a_sentence_the_router_cannot_map_changes_nothing(tmp_path):
    stub = Stub(tmp_path)
    answer = actions.run_description("paint the walls a nicer colour", bridge=stub)

    assert answer["ran"] is False
    assert answer["text"].startswith("I did not send anything to Unity")
    assert stub.sent is None


def test_a_sentence_the_router_maps_reaches_the_editor(tmp_path):
    stub = Stub(tmp_path, outcome=results_for(Answer()))
    answer = actions.run_description("delete the Main Camera", bridge=stub)

    assert answer["ran"] is True
    assert stub.sent == [{"command": "DeleteGameObject", "args": {"target": "Main Camera"}}]


# ======================================================
# The orchestrator, which is where the log went wrong
# ======================================================

def _turn(text: str) -> TurnRequest:
    return TurnRequest(
        messages=[{"role": "user", "content": text}],
        latest_user_text=text,
        conversation_id="test-conversation",
        session=SessionState(),
    )


def test_the_message_from_the_log_never_reaches_a_model(monkeypatch):
    monkeypatch.setattr(actions, "answer_request",
                        lambda text, **kwargs: {"ran": True, "text": "Ran 4 commands."})

    result = turn_orchestrator._unity_editor_reply(_turn(THE_MESSAGE), [])

    assert result is not None, "this is the whole bug: it used to be None"
    assert result.model_id == turn_orchestrator.UNITY_EDITOR_MODEL
    assert result.text == "Ran 4 commands."


def test_a_question_about_unity_still_reaches_a_model():
    assert turn_orchestrator._unity_editor_reply(
        _turn("how do I delete the main camera in Unity?"), []) is None


def test_a_broken_bridge_answers_rather_than_falling_through(monkeypatch):
    def boom(text, **kwargs):
        raise RuntimeError("the bridge module is broken")

    monkeypatch.setattr(actions, "answer_request", boom)
    result = turn_orchestrator._unity_editor_reply(_turn(THE_MESSAGE), [])

    assert result is not None, "silence would hand it back to the model"
    assert "nothing changed" in result.text
