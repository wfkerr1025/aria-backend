"""ARIA Lite - memory retrieval entry point.

This is the module named `backend.aria_memory`. It exists as a package
directory rather than a flat aria_memory.py because memory_ranking lives
beside it -- Python cannot have both backend/aria_memory.py and
backend/aria_memory/, and the package form keeps the import path the same
for callers.

Two layers, deliberately separate:

    search_hybrid   retrieval -- WHICH items are candidates
    search_ranked   ordering  -- which of them ARIA should read first

Keeping them apart means ranking can be changed, weighted differently or
tested without touching retrieval, and a caller that wants raw candidates
is not forced to pay for scoring.

Internal for now: no tool wrapper, no dispatcher registration.
"""

from __future__ import annotations

import re

try:
    from backend.aria_memory import memory_ranking
    from backend.aria_memory_notes import get_note, search_notes_keyword
    from backend.aria_memory_semantic_index import search_semantic
    from backend.llm.query_deframing import deframe_query
    from backend.context.current_topic import current_topic_of
    from backend.context.evidence_selection import select_turns
    from backend.context.goal_tracking import current_goal_of
    from backend.llm.query_expansion import (
        STOPWORDS,
        detect_domain,
        expand_query,
        tokenize,
    )
except ImportError:  # running from inside the backend directory
    from aria_memory import memory_ranking
    from aria_memory_notes import get_note, search_notes_keyword
    from aria_memory_semantic_index import search_semantic
    from llm.query_deframing import deframe_query
    from context.current_topic import current_topic_of
    from context.evidence_selection import select_turns
    from context.goal_tracking import current_goal_of
    from llm.query_expansion import (
        STOPWORDS,
        detect_domain,
        expand_query,
        tokenize,
    )

__all__ = ["memory_ranking", "search_hybrid", "search_ranked", "tokenize"]

_ = STOPWORDS  # re-exported for callers that import it from here

# tokenize and STOPWORDS are re-exported from query_expansion, which owns
# them: expansion has to split a query into the same terms coverage scores it
# on, and two tokenizers disagreeing about one stopword would mean expansion
# widening a term that coverage never counts.


def _contains_term(haystack: str, term: str) -> bool:
    """Whether a term appears as a whole word.

    Whole-word rather than substring: "build" should not be satisfied by
    "rebuild", and "log" should not match "login". Word boundaries also do
    the right thing across the commas in a tag list.
    """
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", haystack) is not None


def _keyword_relevance(query: str, item: dict, expansion: dict | None = None) -> float:
    """How much of the query this item covers, synonyms included.

    Each term counts independently, so a note holding "login" in one
    sentence and "credentials" in another scores the same as one holding
    "login credentials" adjacently -- adjacency is an accident of phrasing,
    not evidence of relevance.

    A term the item does not contain can still be covered by one of its
    expansions, worth that expansion's weight instead of a full 1.0: a note
    saying "password" is evidence for a query about "login", but weaker
    evidence than a note that says "login".

    Credit is capped at 1.0 per term and taken from the single best synonym
    rather than summed, which is what keeps expansion from inflating
    anything. A term the user typed is already worth its full share, so an
    item containing every query term scores exactly 1.0 as it did before
    expansion existed, and three synonyms for one missing term are worth the
    best of the three, not their total.

    Expects a de-framed query; pass the matching expand_query result to
    avoid rebuilding it once per item. Scoring "tell me about the pipeline"
    as five terms would cap a perfect match at 0.2, because the framing
    words a note will never contain count against it.
    """
    expansion = expand_query(query) if expansion is None else expansion
    terms = expansion["terms"]
    if not terms:
        return 0.0

    haystack = f"{item.get('text') or item.get('chunk') or ''} {item.get('tags') or ''}".lower()

    # Each expansion is looked for once, not once per term it stands in for.
    present = {
        entry["term"]
        for entry in expansion["expansions"]
        if _contains_term(haystack, entry["term"])
    }

    covered = 0.0
    for term in terms:
        if _contains_term(haystack, term):
            covered += 1.0
            continue
        # entry["sources"][term] rather than entry["weight"]: a synonym is
        # credited at what it is worth for *this* term, not at its best rate
        # for some other term in the query.
        covered += max(
            (
                entry["sources"][term]
                for entry in expansion["expansions"]
                if entry["term"] in present and term in entry["sources"]
            ),
            default=0.0,
        )
    return covered / len(terms)


def _topic_hint(conversation, query: str = "") -> str | None:
    """The conversation's current topic, usable as a domain hint.

    Only the current topic thread contributes. Messages on every other topic
    are dropped by segmentation before this sees them, which is what keeps a
    detour about the weather from steering a Unity search.
    """
    if conversation is None:
        return None
    # The goal's topic, not the conversation's. They are the same except
    # when a continuity marker has held a goal on its original topic across
    # a turn that named nothing -- which is precisely the case Phase 7.2
    # exists to get right, so the goal's answer is the one to trust.
    topic = current_topic_of(conversation)
    goal = current_goal_of(conversation)
    if goal is not None and goal.topic:
        return goal.topic

    # No goal yet. Rather than read the whole history a second time, take
    # the topic of the best-scoring selected turn: selection has already
    # weighed topic above everything else, so its top turn is the most
    # on-topic thing the conversation contains.
    selected = select_turns(conversation, topic, goal, query=query)
    if selected and selected[0].topic:
        return selected[0].topic
    return topic or None


def search_hybrid(
    query: str,
    limit: int = 20,
    min_score: float = 0.0,
    routing_intent: str | None = None,
    conversation=None,
) -> list[dict]:
    """Merge keyword and semantic hits into one candidate list.

    An item found by both searches appears once, carrying both scores --
    that agreement is exactly what ranking should be able to reward, and
    would be lost by concatenating the two result sets.

    Notes are keyed by note id; chunks stay separate rows because a long
    note can match in one passage and not another, and which passage
    matched is worth keeping.

    The keyword half runs on the de-framed query. A LIKE against "tell me
    about the pipeline" is a search for that literal phrase, which no note
    contains, so framed questions previously returned nothing from the
    keyword side and scored 0.0 coverage on the semantic side's hits. The
    semantic half is passed the caller's original string because
    search_semantic de-frames internally, and the raw query is what the
    caller keeps for display.

    Every candidate is scored on per-term coverage, however it was found: a
    note the semantic half surfaced is scored the same way as one the
    keyword half returned, so the two signals stay comparable. A note found
    only by keyword carries semantic_score 0.0 and vice versa, which is
    honest -- the other search genuinely had nothing to say about it.

    Coverage counts the query's terms and, at a lower weight, the synonyms
    query_expansion offers for the ones an item is missing, so a note written
    as "password reset" is not invisible to a query about "login". Which
    synonyms those are depends on the domain the query is speaking, detected
    once here and used by both halves -- passing routing_intent to one and
    not the other would let the keyword and semantic sides disagree about
    what the query means.

    Coverage is always measured against the whole note, never the chunk the
    semantic half happened to return. A chunk is a 40-word window, so a note
    saying "login" in its opening line and "credentials" three windows later
    would otherwise score half coverage on a query it fully contains, and
    tags -- which chunks do not carry at all -- would count for a keyword hit
    and not for a semantic one. The chunk stays on the row for display and
    keeps its own semantic score; only the keyword denominator widens.
    """
    cleaned = deframe_query(query)
    # A conversation, when supplied, is segmented by topic and only its
    # current thread informs the search. An explicit routing_intent wins:
    # the router had the message and the history, this is an inference from
    # the history alone.
    routing_intent = routing_intent or _topic_hint(conversation, query)
    # The domain comes from the raw query: framing is not a topic, but it is
    # evidence of one, and it is stripped from `cleaned` before this point.
    domain = detect_domain(query, routing_intent)
    # Built once: expansion is a pure dictionary lookup, but it is the same
    # answer for every candidate and there can be hundreds of them.
    expansion = expand_query(cleaned, domain)
    merged: dict[tuple[str, int], dict] = {}
    notes: dict[int, dict] = {}

    def note_row(note_id: int) -> dict | None:
        """The full note behind a hit, fetched once per search.

        Several chunks of one note can come back from a single semantic
        search; they all need the same note text, and it should not be read
        from the database once per chunk.
        """
        if note_id not in notes:
            notes[note_id] = get_note(note_id)
        return notes[note_id]

    for row in search_notes_keyword(cleaned, limit=limit):
        notes[row["id"]] = row
        merged[("note", row["id"])] = {
            **row,
            "type": "note",
            "keyword_score": _keyword_relevance(cleaned, row, expansion),
            "semantic_score": None,  # filled in below if the semantic half agrees
        }

    for hit in search_semantic(
        query, limit=limit, min_score=min_score, routing_intent=routing_intent
    ):
        key = ("note", hit["note_id"])
        if key in merged:
            # Same note, found both ways: keep the note row, add the
            # semantic evidence.
            existing = merged[key]
            existing["semantic_score"] = max(
                existing.get("semantic_score") or 0.0, hit["score"]
            )
            existing["chunk"] = hit["chunk"]
            continue

        merged[("chunk", hit.get("chunk_index") or 0, hit["note_id"])] = {
            **hit,
            "id": hit["note_id"],
            "type": "chunk",
            "semantic_score": hit["score"],
            # Scored against the note, falling back to the chunk itself if
            # the note has been deleted since it was indexed.
            "keyword_score": _keyword_relevance(
                cleaned, note_row(hit["note_id"]) or hit, expansion
            ),
        }

    # A keyword-only note keeps semantic_score 0.0 rather than None, so a
    # caller reading the field never has to distinguish "no semantic match"
    # from "never scored".
    for item in merged.values():
        if item.get("semantic_score") is None:
            item["semantic_score"] = 0.0

    return list(merged.values())


def search_ranked(
    query: str,
    limit: int = 20,
    routing_intent: str | None = None,
    conversation=None,
) -> list[dict]:
    """Retrieve candidates and return them best-first.

    min_score=0.0 on the retrieval pass on purpose: the ranking layer, not
    the semantic floor, decides what is worth surfacing, and a weak semantic
    match that is recent and keyword-exact may still deserve a place.
    """
    items = search_hybrid(
        query,
        limit=limit,
        min_score=0.0,
        routing_intent=routing_intent,
        conversation=conversation,
    )
    return memory_ranking.rank_items(items, query, limit=limit)
