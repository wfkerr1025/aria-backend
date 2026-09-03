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


def test_one_line_of_prose_disqualifies_the_whole_message():
    assert actions.parse_bridge_calls(
        'OpenScene("Assets/Scenes/Main.unity")\nand then tell me what is in it') is None


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


def test_arguments_that_are_not_literals_are_refused():
    """The message is parsed, never executed."""
    assert actions.parse_bridge_calls('OpenScene(__import__("os").getcwd())') is None
    assert actions.parse_bridge_calls('DeleteGameObject(open("x").read())') is None


def test_an_unknown_keyword_is_refused_rather_than_dropped():
    assert actions.parse_bridge_calls('OpenScene(path="a.unity", colour="red")') is None


def test_an_argument_given_twice_is_refused():
    assert actions.parse_bridge_calls('OpenScene("a.unity", path="b.unity")') is None


def test_too_many_positional_arguments_are_refused():
    assert actions.parse_bridge_calls('DeleteGameObject("a", "b", "c")') is None


def test_a_null_field_value_survives():
    """SetField(..., None) clears a reference; it does not omit the argument."""
    command = actions.parse_bridge_calls('SetField("Crate", "Rigidbody", "mass", None)')[0]
    assert command["args"] == {"target": "Crate", "componentType": "Rigidbody",
                               "field": "mass", "value": None}


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
