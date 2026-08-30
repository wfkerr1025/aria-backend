# backend/tests/test_routing_corpus.py
#
# The routing table, stated as behaviour rather than as design.
#
# This file exists because a phrase list looked right and was not. Probed
# against 27 ordinary ways of asking for a file operation, the first
# classifier matched six -- "create a file" routed correctly and "create
# a new file called notes.md" did not, because one extra word broke a
# substring. Twenty-one requests reached the chat model, which cannot
# emit tool actions, producing the exact "ARIA talks about the change
# instead of making it" failure the routing layer exists to prevent.
#
# A phrase list cannot be fixed by adding phrases; there is always
# another way to say it. So the corpus is the specification: any future
# simplification of the classifier has to beat these numbers rather than
# a handful of examples someone happened to remember.

from __future__ import annotations

import pytest

from backend.chat.model_router import (
    TURN_CHAT, TURN_CLASSIFICATION, TURN_HEAVY, TURN_TOOLS,
    select_model_for_turn,
)
from backend.config.model_roles import installed_model_for
from backend.core.turn_types import SessionState, TurnRequest

ROUTER = installed_model_for("qwen2.5-0.5b")
CHAT = installed_model_for("phi-3-mini-4k-instruct-q4")
TOOL = installed_model_for("mistral-7b")
HEAVY = installed_model_for("mistral-nemo-12b")


def turn(text: str, **kw) -> TurnRequest:
    return TurnRequest(
        messages=kw.pop("messages", [{"role": "user", "content": text}]),
        latest_user_text=text,
        conversation_id="c1",
        session=kw.pop("session", SessionState(mode="local")),
        **kw,
    )


# ------------------------------------------------------
# Asking for file or workspace work
# ------------------------------------------------------
TOOL_PHRASINGS = [
    "create a file",
    "create a new file called notes.md",
    "make a new file",
    "add a file to the project",
    "write a new file src/util.py",
    "create a folder called docs",
    "make a directory for tests",
    "delete notes.md",
    "remove the file a.py",
    "rename foo.py to bar.py",
    "move the file into src/",
    "copy config.json to backup",
    "read the file src/main.py",
    "show me what's in config.json",
    "open README.md",
    "list the files in this project",
    "what files are in src/",
    "edit main.py",
    "update the config file",
    "fix the bug in parser.py",
    "add logging to the handler",
    "commit the staged changes",
    "discard my changes",
    "stage everything",
    "rollback that change",
    "run the tests",
    "run pytest",
    "search the web for python 3.13 news",
]

# ------------------------------------------------------
# Conversation. Every one of these was a real false positive at some
# point while the classifier was being probed.
# ------------------------------------------------------
CHAT_PHRASINGS = [
    "hello",
    "hi there",
    "thanks!",
    "what did you mean by that",
    "can you explain that again",
    "who are you",
    "tell me a joke",
    "I like Node.js",
    "how do I create a file in python",
    "what is a directory",
    "can you show me an example",
    "list three ideas for a name",
    "and/or is fine",
    "he/she said hello",
    "it was a win/loss record",
    "python 3.13 is out",
    "e.g. something like that",
    "version 2.0 shipped yesterday",
]


@pytest.mark.parametrize("text", TOOL_PHRASINGS)
def test_file_work_reaches_a_tool_capable_model(text):
    choice = select_model_for_turn(turn(text))

    assert choice.turn_kind in (TURN_TOOLS, TURN_HEAVY), (
        f"{text!r} routed as {choice.turn_kind} and would reach "
        f"{choice.model_id}, which cannot emit tool actions")
    assert choice.model_id in (TOOL, HEAVY)
    assert choice.model_id != ROUTER


@pytest.mark.parametrize("text", CHAT_PHRASINGS)
def test_conversation_is_not_mistaken_for_file_work(text):
    choice = select_model_for_turn(turn(text))

    assert choice.turn_kind == TURN_CHAT
    assert choice.model_id == CHAT


def test_the_corpus_is_large_enough_to_mean_something():
    # Named counts, so a future edit that quietly deletes the awkward
    # cases has to delete the assertion too.
    assert len(TOOL_PHRASINGS) >= 27
    assert len(CHAT_PHRASINGS) >= 16


# ------------------------------------------------------
# The rest of the table
# ------------------------------------------------------
def test_intent_classification_uses_the_smallest_model():
    assert select_model_for_turn(
        turn("anything"), classification_only=True).model_id == ROUTER


@pytest.mark.parametrize("text", [
    "think carefully about why this fails",
    "walk me through the architecture in depth",
    "what is the root cause of the shader mismatch",
    "trace through the request lifecycle",
])
def test_depth_reaches_the_heaviest_model(text):
    assert select_model_for_turn(turn(text)).model_id == HEAVY


def test_a_long_conversation_escalates():
    from backend.chat.model_router import HEAVY_CONTEXT_CHARS

    messages = [{"role": "user", "content": "x" * (HEAVY_CONTEXT_CHARS + 1)},
                {"role": "user", "content": "ok"}]

    assert select_model_for_turn(turn("ok", messages=messages)).model_id == HEAVY


def test_a_named_file_beats_the_instructional_veto():
    # "how do I fix parser.py" is about THIS project however it is
    # phrased. The veto is for "how do I create a file in python".
    assert select_model_for_turn(turn("how do I fix parser.py")).turn_kind == TURN_TOOLS


def test_the_smallest_model_answers_nothing_a_user_reads():
    for text in TOOL_PHRASINGS + CHAT_PHRASINGS:
        assert select_model_for_turn(turn(text)).model_id != ROUTER


# ======================================================
# Automatic mode
#
# Everything above routes through model_router, which only runs when the
# orchestrator resolves a concrete model -- Local mode. A live session in
# AUTOMATIC mode showed what that missed:
#
#     the router deferred            (mode is not local)
#     _resolve_model_id deferred     (same reason)
#     the capability gate saw None   and passed it
#     complexity_router then chose,  AFTER the orchestrator, by prompt
#                                    length -- and picked the 0.5B
#
# The 0.5B answered "Hello Aria" and leaked its own system prompt. Three
# layers built to prevent exactly that each deferred to the next.
#
# The fix is a floor rather than another gate, because a floor works
# wherever the decision is made and a gate only works where it is placed.
# These assert the routing table at THAT layer.
# ======================================================
from backend.core.complexity_router import select_local_model_for_prompt as ladder


@pytest.mark.parametrize("text", ["Hello Aria", "hello there", "thanks!", "who are you"])
def test_the_ladder_never_hands_chat_to_the_smallest_model(text):
    assert ladder(text) != ROUTER
    assert ladder(text) == CHAT


@pytest.mark.parametrize("text", [
    "create a new file called hello_world.py", "delete notes.md",
    "edit main.py", "commit the staged changes",
])
def test_the_ladder_floors_tool_work_at_the_tool_model(text):
    assert ladder(text) == TOOL


@pytest.mark.parametrize("text", [
    "think carefully about the architecture of this module",
    "what is the root cause of this crash",
])
def test_the_ladder_floors_deep_work_at_the_heaviest_model(text):
    assert ladder(text) == HEAVY


def test_the_heuristic_underneath_is_unchanged():
    # The floors are floors, not a rewrite. classify_task_complexity
    # still rates "think carefully about the architecture" as low and
    # still wants the 0.5B -- which is why the floors are needed and why
    # removing them would silently restore the bug.
    assert ladder("Hello Aria", allow_below_chat_floor=True) == ROUTER
    assert ladder("think carefully about the architecture",
                  allow_below_chat_floor=True) == ROUTER


def test_classification_can_still_reach_the_smallest_model():
    # The one legitimate use, and it must survive the floors: nobody
    # reads a classifier's output.
    assert ladder("Hello Aria", allow_below_chat_floor=True) == ROUTER


def test_the_floor_uses_the_same_number_the_gate_does():
    from backend.core.chat_capability_gate import CHAT_PARAM_FLOOR, too_weak_for_chat

    # One authority on "too small to chat". A second threshold here would
    # drift from the gate's, and the drift would show up as a model the
    # gate rejects being chosen by the ladder.
    assert CHAT_PARAM_FLOOR == 3_000_000_000
    for text in ["Hello Aria", "edit main.py", "think carefully about this"]:
        assert too_weak_for_chat(ladder(text)) is False
