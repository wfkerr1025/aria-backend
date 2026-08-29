# backend/tests/test_directory_query_behavior.py
#
# "What is your working directory?" -- answered from the registry, never
# generated.
#
# The failure this replaces is specific and worth naming: asked where it
# was working, a small model produced a confident, well-formed, wrong
# path. Of every question in this application that is the worst one to
# hallucinate, because the answer is the one the user then acts on. A
# fact with an authority behind it should not be routed through a model
# at all -- the same reasoning that already sends "what model are you"
# and "what is the weather" to a lookup instead of a prompt.
#
# So the assertions come in two halves: the question is recognised, and
# the answer comes from workspace_manager rather than from anything that
# could invent one.

from __future__ import annotations

import pytest

from backend.core import turn_orchestrator, workspace_manager
from backend.core.conversation_manager import (
    INTENT_ENVIRONMENT_QUERY,
    INTENT_MODEL_QUERY,
    INTENT_WORKSPACE_QUERY,
    SELF_QUERY_INTENTS,
    detect_intent,
)
from backend.core.turn_orchestrator import orchestrate_turn
from backend.core.turn_types import KIND_INFERENCE, KIND_TEXT, SessionState, TurnRequest


def turn(text: str, **kw) -> TurnRequest:
    return TurnRequest(
        messages=[{"role": "user", "content": text}],
        latest_user_text=text,
        conversation_id="c1",
        session=kw.pop("session", SessionState(mode="local")),
        **kw,
    )


# ======================================================
# The question is recognised
# ======================================================
@pytest.mark.parametrize("phrase", [
    "what is your working directory?",
    "What is your working directory",
    "tell me the directory",
    "where are you working?",
    "which workspace are you in",
    "what workspace is active",
    "what is your project root",
    "where is your ghost workspace",
])
def test_a_workspace_question_is_recognised(phrase):
    assert detect_intent(phrase) == INTENT_WORKSPACE_QUERY


@pytest.mark.parametrize("phrase,expected", [
    # One word apart from "where are you working", and a different
    # question: this one is about local-vs-cloud, not about a directory.
    ("where are you running", INTENT_ENVIRONMENT_QUERY),
    ("what model are you using", INTENT_MODEL_QUERY),
])
def test_it_does_not_swallow_the_neighbouring_questions(phrase, expected):
    assert detect_intent(phrase) == expected


@pytest.mark.parametrize("phrase", [
    "what directory should I put this file in?",
    "create a directory for the tests",
    "how do I change the working directory in python",
])
def test_a_question_about_the_users_code_is_not_a_status_request(phrase):
    # The cost of being greedy here is worse than the hallucination it
    # replaces: answering a question about the user's own project with a
    # status report is both wrong and confusing.
    assert detect_intent(phrase) != INTENT_WORKSPACE_QUERY


def test_it_is_not_one_of_the_self_knowledge_intents():
    # Those all resolve through self_knowledge.answer_self_query(), which
    # knows about models, modes and providers and nothing about
    # workspaces. Adding this to that set would route it to a resolver
    # with no answer for it.
    assert INTENT_WORKSPACE_QUERY not in SELF_QUERY_INTENTS


# ======================================================
# The answer is read, not generated
# ======================================================
@pytest.fixture
def workspace(tmp_path, monkeypatch):
    from backend.core import file_tools

    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))
    return tmp_path


def test_no_model_is_invoked(workspace):
    result = orchestrate_turn(turn("what is your working directory?"),
                              default_local_model=lambda: "nemo-12b-q5")

    assert result.kind == KIND_TEXT
    assert result.inference_request is None
    # modelId marks the text as read rather than generated, the same way
    # "skr" and "search" already do, so the UI never attributes a fact to
    # whichever model happens to be loaded.
    assert result.model_id == turn_orchestrator.WORKSPACE_MODEL


def test_the_answer_carries_the_real_paths(workspace):
    described = workspace_manager.describe_workspace()

    result = orchestrate_turn(turn("what is your working directory?"),
                              default_local_model=lambda: "nemo-12b-q5")

    assert described["project_root"] in result.text
    assert described["ghost_root"] in result.text
    assert "My active workspace is:" in result.text
    assert "Ghost workspace:" in result.text
    assert "Staged files:" in result.text


def test_the_path_is_the_one_the_file_tools_enforce(workspace):
    from backend.core import file_tools

    result = orchestrate_turn(turn("where are you working?"),
                              default_local_model=lambda: "nemo-12b-q5")

    # One authority. A second copy is how a reported directory and an
    # enforced boundary come to disagree, which would make this answer
    # confidently wrong in exactly the way it exists to prevent.
    assert str(file_tools.workspace_root()) in result.text


def test_the_staged_count_is_real(workspace):
    from backend.core import file_tools, ghost_workspace

    (workspace / "notes.txt").write_text("one\n", encoding="utf-8", newline="")
    file_tools.edit_file(ghost_workspace.stage_path("notes.txt"), "two\n", confirm=True)

    result = orchestrate_turn(turn("what is your working directory?"),
                              default_local_model=lambda: "nemo-12b-q5")

    assert "Staged files: 1" in result.text
    # And says where they are, because "1" on its own invites the wrong
    # conclusion -- that the project has already been changed.
    assert "commit" in result.text


def test_it_is_answered_directly_in_the_telemetry(workspace):
    result = orchestrate_turn(turn("what is your working directory?"),
                              default_local_model=lambda: "nemo-12b-q5")

    events = [record["event"] for record in result.telemetry]
    assert "workspace_query_answered_directly" in events


def test_a_read_failure_says_so_rather_than_guessing(workspace, monkeypatch):
    monkeypatch.setattr(
        workspace_manager, "describe_workspace",
        lambda: (_ for _ in ()).throw(RuntimeError("registry unreadable")),
    )

    result = orchestrate_turn(turn("what is your working directory?"),
                              default_local_model=lambda: "nemo-12b-q5")

    # Not a fall-through to the model. The whole point is that this
    # question never gets a generated answer, and "the lookup failed" is
    # the one moment the temptation to allow one is strongest.
    assert result.kind == KIND_TEXT
    assert result.inference_request is None
    assert "could not read" in result.text


def test_an_ordinary_question_still_reaches_a_model(workspace):
    result = orchestrate_turn(turn("explain the build pipeline"),
                              default_local_model=lambda: "nemo-12b-q5")

    assert result.kind == KIND_INFERENCE
