# backend/tests/test_planner_actions.py
#
# The planner names what to act on. It never says what to write.
#
# synthesis_engine.build_prompt() plans, routes, runs its tools and only
# then assembles the prompt -- so the planner runs BEFORE the model has
# generated anything. At plan time the new contents of a file do not
# exist and cannot. That is why an action step carries content=None and
# why backend/core/action_plan.py reads the content back out of the
# answer instead.
#
# The other half is the paths. A filename extracted from prose is a
# filename nobody has checked exists, so a target has to be a document
# the evidence bundle actually produced. "Edit the docs" names nothing
# that exists and gets no action step, which leaves the turn answering in
# prose exactly as it did before.

from __future__ import annotations

import pytest

from backend.planning.plan import KIND_ACTION, KIND_ANSWER, CONVERSATION_TARGET
from backend.planning.plan_builder import PlanBuilder


class _File:
    def __init__(self, path):
        self.path = path
        self.provenance = path


class _Bundle:
    query = ""
    cleaned_query = ""
    current_topic = ""
    domain = None

    def __init__(self, *paths):
        self.files = [_File(p) for p in paths]
        self.notes = []
        self.turns = []
        self.meta = {}


TWO_DOCS = ("README.md", "setup.py")


def plan_for(query, *paths):
    return PlanBuilder().build(query, _Bundle(*(paths or TWO_DOCS)), "", None)


def actions(plan):
    return [s for s in plan.steps if s.kind == KIND_ACTION]


# ------------------------------------------------------
# Actionable intent
# ------------------------------------------------------
def test_an_edit_naming_a_known_document_produces_an_action():
    step = actions(plan_for("edit README.md to add an install section"))[0]

    assert step.target == "README.md"
    assert step.action["tool"] == "edit_file"
    assert step.action["args"] == {"path": "README.md"}


def test_a_request_to_run_the_tests_produces_an_action():
    step = actions(plan_for("run the tests"))[0]

    assert step.action["tool"] == "run_tests"
    assert step.action["args"] == {"scope": ""}


def test_the_test_scope_is_not_improvised_from_the_sentence():
    step = actions(plan_for("run the tests for the shader module"))[0]

    # file_tools validates scope against a character allowlist precisely
    # because passing a fragment of a user's sentence to a test runner is
    # not safe to guess at.
    assert step.action["args"]["scope"] == ""


def test_an_action_depends_on_the_step_that_describes_the_change():
    plan = plan_for("edit setup.py to pin the version")
    step = actions(plan)[0]

    # The terminal step is where the model says what the change is; the
    # action is the request to apply it. An action that did not wait for
    # it would be an action whose content nothing produced.
    assert step.depends_on
    assert step.depends_on[0] in [s.id for s in plan.steps]


# ------------------------------------------------------
# What the planner will not do
# ------------------------------------------------------
def test_the_planner_never_supplies_the_content():
    """It runs before the model generates. There is nothing to supply."""
    for query in ("edit README.md to add an install section", "run the tests"):
        for step in actions(plan_for(query)):
            assert step.action["content"] is None


def test_a_path_that_is_not_in_evidence_produces_no_action():
    plan = plan_for("edit the docs to add an install section")

    # "the docs" names no document the bundle produced. Guessing -- say,
    # picking the first file in the bundle -- would write to a file the
    # user did not name.
    assert actions(plan) == []


def test_a_file_named_but_never_retrieved_produces_no_action():
    plan = plan_for("edit CHANGELOG.md to add a release note")

    assert actions(plan) == []


def test_a_turn_with_no_evidence_produces_no_action():
    plan = PlanBuilder().build("edit README.md", _Bundle(), "", None)

    assert actions(plan) == []
    assert [s.kind for s in plan.steps] == [KIND_ANSWER]


@pytest.mark.parametrize("query", [
    "explain how the build pipeline works",
    "what does setup.py do",
    "summarize README.md",
    "who founded Microsoft",
])
def test_an_explanation_is_not_an_action(query):
    assert actions(plan_for(query)) == []


def test_only_registered_action_tools_are_ever_named():
    from backend.core.action_plan import ACTION_TOOLS

    for query in ("edit README.md to add a section", "run the tests",
                  "edit setup.py and run the tests"):
        for step in actions(plan_for(query)):
            assert step.action["tool"] in ACTION_TOOLS


def test_the_planner_emits_no_commands():
    import inspect

    source = inspect.getsource(PlanBuilder.action_step)
    for forbidden in ("subprocess", "shell", "os.system", "command", "exec("):
        assert forbidden not in source


# ------------------------------------------------------
# Still deterministic
# ------------------------------------------------------
def test_action_generation_consults_no_model():
    """The property the planner's own suite already pins, extended.

    A planner that asked a model what to do would make every plan a
    sampling result, and every test in this directory probabilistic.
    """
    import inspect

    source = inspect.getsource(PlanBuilder.action_step).lower()

    # Code only. This method's comments explain that the MODEL supplies
    # the content, and a plain substring check reads that sentence and
    # asserts the opposite of what it says.
    code = " ".join(
        line for line in source.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )

    for forbidden in ("prompt", "generate(", "llm", "completion", "model"):
        assert forbidden not in code


def test_the_same_query_plans_the_same_action_every_time():
    first = plan_for("edit README.md to add an install section")
    second = plan_for("edit README.md to add an install section")

    assert [(s.kind, s.target, s.action) for s in first.steps] == \
           [(s.kind, s.target, s.action) for s in second.steps]


# ------------------------------------------------------
# Nothing that read plans before has to change
# ------------------------------------------------------
def test_the_action_field_defaults_to_none():
    for step in plan_for("summarize README.md and setup.py").steps:
        if step.kind != KIND_ACTION:
            assert step.action is None


def test_the_router_ignores_action_steps():
    from backend.tools.tool_router import route_plan

    plan = plan_for("edit README.md to add an install section")
    routed = [i.tool_name for i in route_plan(plan, None)]

    # tool_router maps step KINDS to tools and knows nothing about
    # KIND_ACTION, so an action step routes to nothing. That is correct
    # for now: an action with no content is not executable, and the
    # orchestrator is fed by action_plan reading the model's answer.
    assert routed == ["read_file", "read_file"]


def test_an_action_step_is_shaped_like_the_block_action_plan_reads():
    step = actions(plan_for("edit README.md to add an install section"))[0]

    assert set(step.action) == {
        "tool", "target", "content", "args", "preconditions", "postconditions",
    }
    assert isinstance(step.action["preconditions"], list)
    assert isinstance(step.action["postconditions"], list)
