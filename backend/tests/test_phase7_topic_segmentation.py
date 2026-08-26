# backend/tests/test_phase7_topic_segmentation.py
#
# Phase 7.1: topic segmentation.
#
# The behaviour under test is a scoping rule: when a conversation has been
# about three things, retrieval should see the one it is about now and not
# the other two. So the interesting cases are the interleaved histories --
# Unity, then weather, then back -- and the follow-up that names no subject
# at all, which is how most real turns are phrased.
#
# Nothing here loads a model. Classification and segmentation are keyword
# work, and the retrieval tests use the ordinary test database.

from __future__ import annotations

import pytest

from phase3_upgrade_helpers import db  # noqa: F401

from backend import aria_memory
from backend import aria_memory_notes as notes_store
from backend import aria_memory_semantic_index as semantic
from backend.aria_memory import memory_ranking
from backend.aria_synthesis.bundle_builder import build_evidence_bundle
from backend.aria_synthesis.conflict_detection import detect_conflicts
from backend.aria_synthesis.conflict_summary import (
    CROSS_TOPIC_LEAD,
    summarize_conflicts,
)
from backend.aria_synthesis.synthesis_engine import answer_with_evidence
from backend.context.current_topic import (
    current_topic_of,
    current_topic_thread,
    resolve_current_topic,
)
from backend.context.topic_classifier import (
    TOPIC_PRIORITY,
    TOPICS,
    classify_topic,
    topic_matches,
)
from backend.context.topic_segmentation import segment_history, thread_for
from backend.context.topic_segments import TopicSegment, TopicThread
from backend.files import file_ingestion as ingestion


def user(text: str, **extra) -> dict:
    return {"role": "user", "content": text, **extra}


def assistant(text: str, **extra) -> dict:
    return {"role": "assistant", "content": text, **extra}


# A conversation that changes subject twice and comes back.
INTERLEAVED = [
    user("my prefab keeps breaking"),
    assistant("try reimporting the asset"),
    user("what is the forecast tomorrow"),
    assistant("rain in the afternoon"),
    user("the shader still will not compile"),
    assistant("check the keywords"),
]


# ======================================================
# 1. Classification
# ======================================================
@pytest.mark.parametrize("text,topic", [
    ("how do I instance a prefab", "unity"),
    ("the shader will not compile", "unity"),
    ("the routing provider fell back", "backend"),
    ("the websocket keeps timing out", "backend"),
    ("what is the forecast tomorrow", "weather"),
    ("check the radar for the storm", "weather"),
    ("what does nl routing do", "aria_internal"),
    ("raise the score floor", "aria_internal"),
    ("my python traceback says KeyError", "coding"),
    ("help me debug this regex", "coding"),
    ("book a flight to Oslo", "travel"),
    ("which hotel is near the airport", "travel"),
    ("thanks, that worked", "general"),
    ("can you say more about that", "general"),
])
def test_each_topic_is_recognised(text, topic):
    assert classify_topic(text) == topic


def test_every_label_is_a_known_topic():
    assert set(TOPIC_PRIORITY) == TOPICS
    assert TOPIC_PRIORITY[-1] == "general"


def test_unmatched_text_is_general():
    assert classify_topic("the quick brown fox") == "general"
    assert classify_topic("") == "general"
    assert classify_topic(None) == "general"


# --- priority and specificity ---
def test_priority_breaks_a_tie_between_equally_specific_matches():
    # One Unity word and one backend word, both single words: unity wins
    # because it is higher in the stated order.
    assert classify_topic("the prefab talks to the provider") == "unity"


def test_a_more_specific_marker_beats_a_higher_priority_one():
    # "nl routing" is two words and belongs to aria_internal; "routing"
    # is one and belongs to backend. Priority alone would answer backend,
    # which is wrong -- and is the example the spec itself gives.
    assert classify_topic("what does nl routing do") == "aria_internal"
    assert classify_topic("what does routing do") == "backend"


def test_specificity_is_word_count_before_length():
    assert classify_topic("the score floor for the endpoint") == "aria_internal"


@pytest.mark.parametrize("earlier,later", [
    ("unity", "backend"),
    ("backend", "weather"),
    ("weather", "aria_internal"),
    ("aria_internal", "coding"),
    ("coding", "travel"),
])
def test_the_stated_priority_order_holds(earlier, later):
    assert TOPIC_PRIORITY.index(earlier) < TOPIC_PRIORITY.index(later)


# --- whole-word matching ---
@pytest.mark.parametrize("text", [
    "the area of the room",          # "aria" inside "area"
    "training the new hire",         # "rain" inside "training"
    "a cantilever bracket",          # "cant" inside "cantilever"
])
def test_a_marker_does_not_fire_inside_a_longer_word(text):
    assert classify_topic(text) == "general"


def test_matching_is_case_insensitive():
    assert classify_topic("My PREFAB Broke") == "unity"


def test_punctuation_does_not_hide_a_marker():
    assert classify_topic("is the prefab, broken?") == "unity"


def test_the_matches_are_inspectable():
    matches = topic_matches("the prefab talks to the provider")
    assert matches["unity"] == "prefab"
    assert matches["backend"] == "provider"


def test_classification_is_deterministic():
    text = "the prefab talks to the provider"
    assert all(classify_topic(text) == "unity" for _ in range(5))


def test_classification_makes_no_model_calls(monkeypatch):
    from backend.llm import semantic_embeddings

    def explode(*args, **kwargs):
        raise AssertionError("topic classification must not call a model")

    monkeypatch.setattr(semantic_embeddings, "embed_texts_semantic", explode)
    monkeypatch.setattr(semantic_embeddings, "embed_text_semantic", explode)
    assert classify_topic("how do I instance a prefab") == "unity"


# ======================================================
# 2. Segmentation
# ======================================================
def test_messages_are_grouped_by_topic():
    threads = {thread.topic: thread for thread in segment_history(INTERLEAVED)}
    assert set(threads) == {"unity", "weather"}
    # Each answer is filed with the question it answered, so a thread holds
    # both halves of its exchange rather than only the user's.
    assert [segment.text for segment in threads["unity"].segments] == [
        "my prefab keeps breaking",
        "try reimporting the asset",
        "the shader still will not compile",
        "check the keywords",
    ]
    assert [segment.text for segment in threads["weather"].segments] == [
        "what is the forecast tomorrow",
        "rain in the afternoon",
    ]


def test_an_answer_is_filed_with_its_question():
    # "try reimporting the asset" names no topic of its own.
    assert thread_for(segment_history(INTERLEAVED), "unity").segments[1].role == "assistant"


def test_answers_can_be_classified_on_their_own_text_instead():
    threads = {t.topic: t for t in segment_history(INTERLEAVED, attach_replies=False)}
    assert "general" in threads
    assert [s.text for s in threads["unity"].segments] == [
        "my prefab keeps breaking",
        "the shader still will not compile",
    ]


def test_an_answer_that_names_its_own_topic_keeps_it():
    history = [user("my prefab keeps breaking"),
               assistant("that is a radar forecast problem, not a build one")]
    threads = {thread.topic: thread for thread in segment_history(history)}
    assert "weather" in threads


def test_segments_keep_their_original_order():
    thread = thread_for(segment_history(INTERLEAVED), "unity")
    assert thread.segments[0].text == "my prefab keeps breaking"
    assert thread.segments[-1].text == "check the keywords"
    assert thread.user_segments[-1].text == "the shader still will not compile"


def test_threads_are_sorted_by_last_updated():
    threads = segment_history(INTERLEAVED)
    stamps = [thread.last_updated for thread in threads]
    assert stamps == sorted(stamps, reverse=True)


def test_the_most_recently_spoken_thread_comes_first():
    assert segment_history(INTERLEAVED)[0].topic == "unity"


def test_roles_are_preserved():
    thread = thread_for(segment_history(INTERLEAVED), "weather")
    assert [segment.role for segment in thread.segments] == ["user", "assistant"]


def test_timestamps_are_used_when_present():
    history = [
        user("my prefab broke", timestamp=100.0),
        user("what is the forecast", timestamp=50.0),
    ]
    threads = segment_history(history)
    assert threads[0].topic == "unity"


def test_a_history_with_no_timestamps_still_orders_by_position():
    threads = segment_history([user("what is the forecast"), user("my prefab broke")])
    assert [thread.topic for thread in threads] == ["unity", "weather"]


def test_segmentation_does_not_mutate_the_messages():
    history = [dict(message) for message in INTERLEAVED]
    before = [dict(message) for message in history]
    segment_history(history)
    assert history == before


def test_alternative_field_names_are_accepted():
    threads = segment_history([{"role": "user", "text": "my prefab broke", "id": "m1"}])
    assert threads[0].topic == "unity"
    assert threads[0].segments[0].message_id == "m1"


def test_an_empty_history_has_no_threads():
    assert segment_history([]) == []
    assert segment_history(None) == []


def test_a_missing_thread_comes_back_empty_rather_than_none():
    thread = thread_for(segment_history(INTERLEAVED), "travel")
    assert isinstance(thread, TopicThread)
    assert thread.segments == []


def test_segmentation_is_deterministic():
    runs = [
        [(thread.topic, len(thread)) for thread in segment_history(INTERLEAVED)]
        for _ in range(5)
    ]
    assert all(run == runs[0] for run in runs)


def test_a_segment_reports_its_own_role():
    segment = TopicSegment("m1", "user", "hello", 0.0, "general")
    assert segment.is_user
    assert not TopicSegment("m2", "assistant", "hi", 1.0, "general").is_user


# ======================================================
# 3. Current topic
# ======================================================
def test_the_last_user_message_decides():
    assert current_topic_of(INTERLEAVED) == "unity"


def test_a_trailing_assistant_message_does_not_change_the_topic():
    history = INTERLEAVED + [assistant("the radar shows a storm coming")]
    assert current_topic_of(history) == "unity"


def test_an_assistant_only_history_is_general():
    assert current_topic_of([assistant("my prefab broke")]) == "general"


def test_an_empty_history_is_general():
    assert current_topic_of([]) == "general"
    assert resolve_current_topic([]) == "general"
    assert resolve_current_topic(None) == "general"


def test_a_topic_change_takes_effect_immediately():
    assert current_topic_of(INTERLEAVED + [user("book me a flight")]) == "travel"


# --- carrying a topic across an unmarked follow-up ---
def test_an_unmarked_follow_up_inherits_the_last_named_topic():
    # "why is it still failing" names nothing. Read literally it is a
    # general message, and scoping retrieval to general is exactly the lost
    # thread this phase exists to prevent.
    history = [user("my prefab keeps breaking"), assistant("try reimporting"),
               user("why is it still failing")]
    assert current_topic_of(history) == "unity"


def test_the_literal_rule_is_available_and_does_not_inherit():
    history = [user("my prefab keeps breaking"), assistant("try reimporting"),
               user("why is it still failing")]
    assert current_topic_of(history, carry_forward=False) == "general"


def test_inheritance_takes_the_most_recent_named_topic():
    # Not the one the user probably meant -- a rule cannot tell that "anyway"
    # reaches back over the weather detour -- but a defined one.
    history = INTERLEAVED + [user("anyway why is it still failing")]
    assert current_topic_of(history) == "unity"


def test_a_conversation_that_named_nothing_stays_general():
    assert current_topic_of([user("hello"), user("thanks")]) == "general"


def test_the_scoped_thread_holds_only_the_current_topic():
    texts = [segment.text for segment in current_topic_thread(INTERLEAVED).segments]
    assert texts == [
        "my prefab keeps breaking",
        "try reimporting the asset",
        "the shader still will not compile",
        "check the keywords",
    ]
    # The whole point: the weather detour is not in the scoped context.
    assert not any("forecast" in text or "rain" in text for text in texts)


def test_the_scoped_thread_exposes_the_last_user_message():
    assert current_topic_thread(INTERLEAVED).latest_user_text() == (
        "the shader still will not compile"
    )


def test_current_topic_is_deterministic():
    assert all(current_topic_of(INTERLEAVED) == "unity" for _ in range(5))


# ======================================================
# 4. Retrieval routing
# ======================================================
def test_hybrid_accepts_a_conversation(db):
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    items = {item["id"]: item for item in aria_memory.search_hybrid(
        "prefab", min_score=0.0, conversation=[user("my prefab keeps breaking")]
    )}
    assert note_id in items


def test_the_conversation_topic_drives_expansion(db):
    # "prefab" only expands toward "gameobject" in the Unity domain, so a
    # note using the synonym earns keyword credit only when the conversation
    # puts the query in that domain.
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    unity = {i["id"]: i for i in aria_memory.search_hybrid(
        "prefab", min_score=0.0, conversation=[user("my prefab keeps breaking")]
    )}
    weather = {i["id"]: i for i in aria_memory.search_hybrid(
        "prefab", min_score=0.0, conversation=[user("what is the forecast tomorrow")]
    )}
    assert unity[note_id]["keyword_score"] > 0.0
    assert weather.get(note_id, {}).get("keyword_score", 0.0) == 0.0


def test_cross_topic_messages_are_ignored(db):
    # The weather turns are in the history but not in the current thread,
    # so they must not pull the search away from Unity.
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    scoped = {i["id"]: i for i in aria_memory.search_hybrid(
        "prefab", min_score=0.0, conversation=INTERLEAVED
    )}
    assert scoped[note_id]["keyword_score"] > 0.0


def test_an_explicit_routing_intent_beats_the_conversation(db):
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    items = {i["id"]: i for i in aria_memory.search_hybrid(
        "prefab", min_score=0.0,
        conversation=[user("my prefab keeps breaking")],
        routing_intent="weather.query",
    )}
    assert items.get(note_id, {}).get("keyword_score", 0.0) == 0.0


def test_hybrid_output_is_unchanged_for_a_single_topic_history(db):
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    without = aria_memory.search_hybrid("prefab", min_score=0.0)
    with_history = aria_memory.search_hybrid(
        "prefab", min_score=0.0, conversation=[user("my prefab keeps breaking")]
    )
    assert [i["id"] for i in without] == [i["id"] for i in with_history]
    assert [i["keyword_score"] for i in without] == [i["keyword_score"] for i in with_history]
    assert [i["semantic_score"] for i in without] == [i["semantic_score"] for i in with_history]


def test_no_conversation_leaves_retrieval_exactly_as_it_was(db):
    note_id = notes_store.save_note("the release pipeline runs nightly")
    semantic.index_note(note_id)

    plain = aria_memory.search_hybrid("release pipeline", min_score=0.0)
    explicit_none = aria_memory.search_hybrid(
        "release pipeline", min_score=0.0, conversation=None
    )
    assert [i["keyword_score"] for i in plain] == [i["keyword_score"] for i in explicit_none]
    assert [i["semantic_score"] for i in plain] == [i["semantic_score"] for i in explicit_none]


def test_search_ranked_passes_the_conversation_through(db):
    note_id = notes_store.save_note("the gameobject was saved as a reusable asset")
    semantic.index_note(note_id)

    ranked = {i["id"]: i for i in aria_memory.search_ranked(
        "prefab", conversation=[user("my prefab keeps breaking")]
    )}
    assert ranked[note_id]["keyword_score"] > 0.0


def test_the_file_searches_accept_a_conversation(db, tmp_path):
    path = tmp_path / "unity.md"
    path.write_text("the gameobject was saved as a reusable asset", encoding="utf-8")
    ingestion.ingest_and_index(str(path))

    hits = ingestion.search_files_semantic(
        "prefab", min_score=0.0, conversation=[user("my prefab keeps breaking")]
    )
    assert hits is not None


def test_retrieval_is_deterministic_under_a_conversation(db):
    semantic.index_note(notes_store.save_note("the gameobject was saved as a reusable asset"))
    runs = [
        [i["keyword_score"] for i in aria_memory.search_hybrid(
            "prefab", min_score=0.0, conversation=INTERLEAVED
        )]
        for _ in range(3)
    ]
    assert all(run == runs[0] for run in runs)


# ======================================================
# 5. Synthesis routing
# ======================================================
def note_item(note_id: int, text: str, score: float = 0.8) -> dict:
    return {"type": "note", "id": note_id, "text": text, "combined_score": score}


def test_the_bundle_records_the_current_topic():
    bundle = build_evidence_bundle(
        "why is it failing", [note_item(1, "The prefab lost its reference.")],
        conversation=INTERLEAVED, now="FIXED",
    )
    assert bundle.meta["current_topic"] == "unity"
    assert bundle.current_topic == "unity"


def test_no_conversation_records_no_topic():
    bundle = build_evidence_bundle("q", [note_item(1, "x")], now="FIXED")
    assert bundle.meta["current_topic"] is None
    assert bundle.current_topic == ""


def test_evidence_items_carry_their_own_topic():
    bundle = build_evidence_bundle(
        "q",
        [note_item(1, "The prefab lost its monobehaviour."),
         note_item(2, "The radar station is offline.", 0.5)],
        now="FIXED",
    )
    assert bundle.notes[0].topic == "unity"
    assert bundle.notes[1].topic in {"weather", "backend"}


def test_the_bundle_reports_the_topics_it_spans():
    bundle = build_evidence_bundle(
        "q",
        [note_item(1, "The prefab lost its monobehaviour."),
         note_item(2, "Book a flight to Oslo.", 0.5)],
        conversation=INTERLEAVED, now="FIXED",
    )
    assert set(bundle.topics) == {"unity", "travel"}
    assert [item.topic for item in bundle.off_topic_items] == ["travel"]


def test_off_topic_evidence_is_kept_not_filtered():
    # Recorded, never dropped: a bundle that silently discarded cross-topic
    # evidence would hide the material a reader needs when an answer is odd.
    bundle = build_evidence_bundle(
        "q",
        [note_item(1, "The prefab lost its monobehaviour."),
         note_item(2, "Book a flight to Oslo.", 0.5)],
        conversation=INTERLEAVED, now="FIXED",
    )
    assert bundle.item_count == 2


def test_the_prompt_names_the_current_topic_when_evidence_strays():
    seen = []
    answer_with_evidence(
        "why is it failing",
        [note_item(1, "The prefab lost its monobehaviour."),
         note_item(2, "Book a flight to Oslo.", 0.5)],
        conversation=INTERLEAVED,
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    # Phase 7.4 moved this into the Long-Context Analysis section, which is
    # where every scope caveat now lives.
    assert "Long-Context Analysis:" in seen[0]
    assert "Most evidence relates to unity; some comes from other topics" in seen[0]


def test_the_prompt_stays_quiet_when_all_evidence_is_on_topic():
    seen = []
    answer_with_evidence(
        "why is it failing",
        [note_item(1, "The prefab lost its monobehaviour.")],
        conversation=INTERLEAVED,
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert "some comes from other topics" not in seen[0]


def test_the_prompt_stays_quiet_without_a_conversation():
    seen = []
    answer_with_evidence(
        "why is it failing",
        [note_item(1, "The prefab lost its monobehaviour.")],
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert "Topic Focus" not in seen[0]


def test_a_cross_topic_conflict_is_marked_and_worded_differently():
    # The disputed sentence is identical on both sides -- which is what the
    # conflict detector requires -- while the notes around it belong to
    # different topics, which is what makes the disagreement cross-topic.
    bundle = build_evidence_bundle(
        "the cache",
        [{"type": "note", "id": 1,
          "text": "Book a flight to Oslo. The cache is enabled.", "combined_score": 0.9},
         {"type": "note", "id": 2,
          "text": "The shader compiles fine. The cache is disabled.", "combined_score": 0.8}],
        now="FIXED",
    )
    conflicts = detect_conflicts(bundle)
    assert conflicts and conflicts[0].cross_topic
    assert len(conflicts[0].topics) > 1
    assert summarize_conflicts(conflicts)[0].startswith(CROSS_TOPIC_LEAD)


def test_a_same_topic_conflict_keeps_the_plain_wording():
    bundle = build_evidence_bundle(
        "the cache",
        [{"type": "note", "id": 1,
          "text": "The shader is fine. The cache is enabled.", "combined_score": 0.9},
         {"type": "note", "id": 2,
          "text": "The shader is fine. The cache is disabled.", "combined_score": 0.8}],
        now="FIXED",
    )
    conflicts = detect_conflicts(bundle)
    assert conflicts and not conflicts[0].cross_topic
    assert not summarize_conflicts(conflicts)[0].startswith(CROSS_TOPIC_LEAD)


def test_the_prompt_never_invents_a_topic_switch():
    # The only topic named in the prompt is the one resolved from the
    # history; nothing else may appear as the current subject.
    seen = []
    answer_with_evidence(
        "why is it failing",
        [note_item(1, "The prefab lost its monobehaviour."),
         note_item(2, "Book a flight to Oslo.", 0.5)],
        conversation=INTERLEAVED,
        generate=lambda prompt: (seen.append(prompt), "x")[1],
    )
    assert "Most evidence relates to unity" in seen[0]
    for other in ("relates to travel", "relates to weather"):
        assert other not in seen[0]


# ======================================================
# 6. Determinism and no drift
# ======================================================
def test_the_same_history_gives_the_same_bundle_topic():
    first = build_evidence_bundle("q", [note_item(1, "x")], conversation=INTERLEAVED, now="F")
    second = build_evidence_bundle("q", [note_item(1, "x")], conversation=INTERLEAVED, now="F")
    assert first == second


def test_the_same_history_gives_the_same_prompt():
    seen = []
    for _ in range(3):
        answer_with_evidence(
            "why is it failing",
            [note_item(1, "The prefab lost its monobehaviour."),
             note_item(2, "Book a flight to Oslo.", 0.5)],
            conversation=INTERLEAVED,
            generate=lambda prompt: (seen.append(prompt), "x")[1],
        )
    assert all(prompt == seen[0] for prompt in seen)


def test_ranking_weights_are_unchanged():
    assert memory_ranking.WEIGHT_SEMANTIC == 0.45
    assert memory_ranking.WEIGHT_KEYWORD == 0.25
    assert memory_ranking.WEIGHT_RECENCY == 0.20
    assert memory_ranking.WEIGHT_TYPE == 0.10


def test_score_floors_are_unchanged():
    assert semantic.DEFAULT_MIN_SCORE == 0.6
    assert ingestion.DEFAULT_MIN_SCORE == 0.55


def test_segmentation_makes_no_model_calls(monkeypatch):
    from backend.llm import semantic_embeddings

    def explode(*args, **kwargs):
        raise AssertionError("segmentation must not call a model")

    monkeypatch.setattr(semantic_embeddings, "embed_texts_semantic", explode)
    monkeypatch.setattr(semantic_embeddings, "embed_text_semantic", explode)
    assert current_topic_of(INTERLEAVED) == "unity"
    assert len(segment_history(INTERLEAVED)) == 2
