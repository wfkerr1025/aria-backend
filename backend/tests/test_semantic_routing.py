# backend/tests/test_semantic_routing.py
#
# backend/llm/semantic_routing.py -- deciding which subsystems a turn should
# activate, and the conversation_manager wiring that acts on the decision.
#
# Classification is a pure function of the message, so most of this file is
# table-driven: a phrase in, an intent out. The integration tests at the
# bottom use the real database and prompt stack to check that a use_memory
# veto actually prevents retrieval rather than merely being recorded.

from __future__ import annotations

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.core import conversation_manager
from backend.llm import llm_engine, memory_prompting, semantic_routing as routing


def intent_of(message: str) -> str:
    return routing.route(message)["intent"]


# ======================================================
# memory.recall
# ======================================================
@pytest.mark.parametrize(
    "message",
    [
        "What did I tell you about my deployment schedule?",
        "what did I say about the release?",
        "remind me what the plan was",
        "you said the pipeline runs nightly",
        "earlier you mentioned a workaround",
        "do you remember my api key setup?",
        "we discussed this last time",
        "what are my preferences?",
        "check my settings please",
        "look through my notes for the invoice",
    ],
)
def test_memory_recall_is_detected(message):
    assert intent_of(message) == routing.MEMORY_RECALL


def test_memory_recall_uses_memory_only():
    result = routing.route("remind me what you said")
    assert result["use_memory"] is True
    assert result["use_tools"] is False
    assert result["use_files"] is False


def test_multiple_recall_phrases_raise_confidence():
    single = routing.route("remind me about that")["confidence"]
    double = routing.route("remind me what you said")["confidence"]
    assert double > single


def test_recall_beats_code_words():
    # A recall question that happens to mention an error is a recall
    # question, not a debugging request.
    result = routing.route("what did I tell you about that compile error?")
    assert result["intent"] == routing.MEMORY_RECALL
    assert result["use_memory"] is True


def test_recall_beats_weather_words():
    assert intent_of("remind me what you said about the weather app") == routing.MEMORY_RECALL


# ======================================================
# weather.query
# ======================================================
@pytest.mark.parametrize(
    "message",
    [
        "what's the weather in Berlin tomorrow?",
        "give me the forecast for London",
        "what's the temperature today?",
        "is it going to rain this weekend?",
        "will there be snow on Monday?",
        "how windy is it in Chicago?",
    ],
)
def test_weather_query_is_detected(message):
    assert intent_of(message) == routing.WEATHER_QUERY


def test_weather_query_uses_tools_not_memory():
    result = routing.route("what's the weather in Berlin tomorrow?")
    assert result["use_tools"] is True
    assert result["use_memory"] is False
    assert result["use_files"] is False


def test_weather_with_location_and_time_is_high_confidence():
    qualified = routing.route("what's the weather in Berlin tomorrow?")
    bare = routing.route("what's the weather?")
    assert qualified["confidence"] > bare["confidence"]


def test_a_bare_weather_question_still_routes_to_weather():
    # Sending the one unambiguous weather question to plain chat would be
    # worse than routing it with lower confidence.
    assert intent_of("what's the weather?") == routing.WEATHER_QUERY


def test_location_is_detected():
    assert routing.route("weather in Berlin")["raw_features"]["has_location"] is True
    assert routing.route("weather")["raw_features"]["has_location"] is False


def test_time_phrases_are_detected():
    assert "tomorrow" in routing.route("forecast tomorrow")["raw_features"]["time_phrases"]


# ======================================================
# code.help
# ======================================================
@pytest.mark.parametrize(
    "message",
    [
        "why does this throw a NullPointerException?",
        "I keep getting a runtime error",
        "here is the stack trace, what's wrong?",
        "this won't compile",
        "fix this: ```python\ndef broken(:\n```",
        "Traceback (most recent call last): ValueError",
        "def handler(request): return None  # why does this fail",
        "const x = 1; why is this undefined",
    ],
)
def test_code_help_is_detected(message):
    assert intent_of(message) == routing.CODE_HELP


def test_code_help_uses_tools_not_memory():
    result = routing.route("why does this throw an exception?")
    assert result["use_tools"] is True
    assert result["use_memory"] is False
    assert result["use_files"] is False


def test_a_code_fence_alone_is_enough():
    result = routing.route("```\nsome code here\n```")
    assert result["intent"] == routing.CODE_HELP
    assert result["raw_features"]["code_shapes"]


def test_keyword_plus_code_shape_is_highest_confidence():
    both = routing.route("this raises an exception:\n```py\ndef f(): pass\n```")
    keyword_only = routing.route("I got an error")
    assert both["confidence"] > keyword_only["confidence"]


def test_a_file_and_line_reference_reads_as_code():
    assert intent_of("it fails at handlers.py:1033") == routing.CODE_HELP


# ======================================================
# files.query
# ======================================================
@pytest.mark.parametrize(
    "message",
    [
        "summarize this file for me",
        "search my documents about invoices",
        "search my files for the contract",
        "what's in that pdf?",
        "read the file and tell me the total",
        "check the spreadsheet",
    ],
)
def test_files_query_is_detected(message):
    assert intent_of(message) == routing.FILES_QUERY


def test_files_query_uses_files_only():
    result = routing.route("summarize this file")
    assert result["use_files"] is True
    assert result["use_memory"] is False
    assert result["use_tools"] is False


def test_a_file_phrase_outscores_a_bare_file_word():
    phrase = routing.route("search my documents about invoices")
    word = routing.route("check the folder")
    assert phrase["confidence"] > word["confidence"]


# ======================================================
# chat.general
# ======================================================
@pytest.mark.parametrize(
    "message",
    [
        "what is the capital of France?",
        "tell me a joke",
        "hello",
        "can you explain photosynthesis?",
        "what's 2 + 2?",
        "",
    ],
)
def test_chat_general_is_the_default(message):
    assert intent_of(message) == routing.CHAT_GENERAL


def test_chat_general_keeps_memory_on():
    # An unclassified message is exactly where recall might help and nothing
    # else can, so the default has to be memory-on.
    result = routing.route("tell me something interesting")
    assert result["use_memory"] is True
    assert result["use_tools"] is False
    assert result["use_files"] is False


def test_chat_general_has_the_lowest_confidence():
    default = routing.route("hello")["confidence"]
    classified = routing.route("what did I tell you about it?")["confidence"]
    assert default < classified


# ======================================================
# Result shape and flags
# ======================================================
def test_result_has_every_field():
    assert sorted(routing.route("hello")) == [
        "confidence", "intent", "raw_features", "use_files", "use_memory", "use_tools",
    ]


def test_confidence_is_a_probability():
    for message in ["hello", "weather in Berlin", "remind me", "```code```", "this file"]:
        assert 0.0 <= routing.route(message)["confidence"] <= 1.0


@pytest.mark.parametrize(
    "intent, use_memory, use_tools, use_files",
    [
        (routing.MEMORY_RECALL, True, False, False),
        (routing.WEATHER_QUERY, False, True, False),
        (routing.CODE_HELP, False, True, False),
        (routing.FILES_QUERY, False, False, True),
        (routing.CHAT_GENERAL, True, False, False),
    ],
)
def test_flags_match_the_documented_table(intent, use_memory, use_tools, use_files):
    flags = routing.INTENT_FLAGS[intent]
    assert flags["use_memory"] is use_memory
    assert flags["use_tools"] is use_tools
    assert flags["use_files"] is use_files


def test_exactly_one_subsystem_is_selected_per_intent():
    for message in ["remind me", "weather in Berlin", "```x```", "this file", "hello"]:
        result = routing.route(message)
        selected = sum([result["use_memory"], result["use_tools"], result["use_files"]])
        assert selected == 1


def test_raw_features_are_exposed_for_debugging():
    features = routing.route("what's the weather in Berlin tomorrow?")["raw_features"]
    assert "weather" in features["weather_words"]
    assert features["has_location"] is True
    assert "tomorrow" in features["time_phrases"]


# ======================================================
# Determinism and purity
# ======================================================
def test_classification_is_deterministic():
    message = "what's the weather in Berlin tomorrow?"
    assert routing.route(message) == routing.route(message)


def test_classification_is_stable_across_repeated_calls():
    message = "what did I tell you about the error in main.py:12?"
    results = {routing.route(message)["intent"] for _ in range(10)}
    assert len(results) == 1


def test_history_does_not_change_the_decision():
    # Documented behaviour: history is accepted for signature stability but
    # not yet consulted, which keeps classification purely a function of the
    # message.
    without = routing.route("hello")
    with_history = routing.route("hello", history=[{"role": "user", "content": "weather in Berlin"}])
    assert without == with_history


def test_classification_is_case_insensitive():
    assert intent_of("WHAT DID I TELL YOU ABOUT THIS?") == routing.MEMORY_RECALL
    assert intent_of("What's The WEATHER In Berlin?") == routing.WEATHER_QUERY


def test_routing_touches_no_database(monkeypatch):
    # Routing runs in front of every turn; it must not become the reason a
    # turn is slow or a reason a turn can fail.
    def explode(*_args, **_kwargs):
        raise AssertionError("routing must not retrieve")

    monkeypatch.setattr(memory_prompting, "build_memory_context", explode)
    assert routing.route("what did I tell you?")["intent"] == routing.MEMORY_RECALL


def test_route_matches_classify_intent():
    message = "summarize this file"
    assert routing.route(message) == routing.classify_intent(message)


# ======================================================
# llm_engine veto
# ======================================================
def test_use_memory_false_skips_retrieval(db, monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("retrieval should have been skipped")

    monkeypatch.setattr(memory_prompting, "build_memory_context", explode)
    assert llm_engine.build_system_prompt("base", query="anything", use_memory=False) == "base"
    assert llm_engine.build_user_prompt("hello", use_memory=False) == "hello"


def test_use_memory_true_still_retrieves(db):
    note_id = notes_store.save_note("the deployment pipeline runs nightly")
    semantic.index_note(note_id)
    prompt = llm_engine.build_system_prompt("base", query="deployment pipeline", use_memory=True)
    assert "### Memory Context" in prompt


# ======================================================
# conversation_manager integration
# ======================================================
def seed(db_path=None):
    note_id = notes_store.save_note("the deployment pipeline runs nightly")
    semantic.index_note(note_id)
    return note_id


def policy_for(message: str):
    final, policy = conversation_manager.apply_history_policy(
        [{"role": "user", "content": message}]
    )
    return final, policy


def test_policy_info_carries_every_routing_field(db):
    _final, policy = policy_for("hello")
    for key in [
        "routing_intent", "routing_confidence",
        "routing_use_memory", "routing_use_tools", "routing_use_files",
    ]:
        assert key in policy


def test_policy_info_reports_the_classified_intent(db):
    _final, policy = policy_for("what's the weather in Berlin tomorrow?")
    assert policy["routing_intent"] == routing.WEATHER_QUERY
    assert policy["routing_use_tools"] is True
    assert policy["routing_use_memory"] is False


def test_a_memory_turn_gets_the_memory_context(db):
    seed()
    final, policy = policy_for("what did I tell you about the deployment pipeline?")
    assert policy["routing_use_memory"] is True
    assert "### Memory Context" in final[0]["content"]
    assert final[0]["content"] != conversation_manager.SYSTEM_PROMPT


def test_a_weather_turn_gets_the_plain_system_prompt(db):
    seed()
    final, policy = policy_for("what's the weather in Berlin tomorrow?")
    assert policy["routing_use_memory"] is False
    assert final[0]["content"] == conversation_manager.SYSTEM_PROMPT
    assert "### Memory Context" not in final[0]["content"]


def test_a_code_turn_gets_the_plain_system_prompt(db):
    seed()
    final, policy = policy_for("why does this throw an exception?")
    assert policy["routing_intent"] == routing.CODE_HELP
    assert final[0]["content"] == conversation_manager.SYSTEM_PROMPT


def test_a_files_turn_gets_the_plain_system_prompt(db):
    seed()
    final, policy = policy_for("summarize this file")
    assert policy["routing_use_files"] is True
    assert final[0]["content"] == conversation_manager.SYSTEM_PROMPT


def test_a_general_turn_keeps_memory(db):
    seed()
    final, policy = policy_for("tell me about the deployment pipeline")
    assert policy["routing_intent"] == routing.CHAT_GENERAL
    assert policy["routing_use_memory"] is True
    assert "### Memory Context" in final[0]["content"]


def test_the_conversation_messages_are_untouched_by_routing(db):
    messages = [{"role": "user", "content": "what's the weather in Berlin?"}]
    final, _policy = conversation_manager.apply_history_policy(messages)
    assert final[1:] == messages


def test_routing_failure_falls_back_to_memory(db, monkeypatch):
    # A router that raises must not take chat down, and must not silently
    # disable memory either.
    def explode(*_args, **_kwargs):
        raise RuntimeError("router is down")

    monkeypatch.setattr(conversation_manager.semantic_routing, "route", explode)
    seed()
    final, policy = policy_for("what did I tell you about the pipeline?")
    assert policy["routing_use_memory"] is True
    assert "### Memory Context" in final[0]["content"]


def test_existing_policy_fields_are_still_reported(db):
    _final, policy = policy_for("hello")
    for key in ["mode", "incoming_count", "kept_count", "dropped_count", "max_messages"]:
        assert key in policy
