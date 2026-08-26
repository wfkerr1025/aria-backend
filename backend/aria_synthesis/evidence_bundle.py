"""ARIA Lite Phase 6 - the evidence bundle.

One normalized shape for everything retrieval found, whatever found it.

A note row, a note chunk and a file chunk arrive from three different
searches with three different field names -- text vs chunk, id vs note_id vs
file_id, semantic_score vs score. A prompt builder that reads those
directly ends up carrying the union of three schemas, and every retrieval
change becomes a prompt change. The bundle absorbs that: it is built once,
from whatever retrieval produced, and everything downstream reads only the
five fields it exposes.

The structures are frozen dataclasses. A bundle is a record of what was
retrieved for one query at one moment -- something that gets logged,
compared against a later run and asserted on in tests -- and none of that
survives a downstream step quietly rewriting a snippet.

Snippets are the load-bearing detail. They must be stable: the same note
must produce the same snippet on every call, on every machine, or two runs
of the same query produce two different prompts and the model's answer
changes for no reason anyone can see. So snippets are pure string work --
whitespace collapsed, cut at a word boundary, no model in the path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

try:
    from backend.llm.query_expansion import ExpansionDomain
except ImportError:  # running from inside the backend directory
    from llm.query_expansion import ExpansionDomain

__all__ = [
    "EvidenceBundle",
    "EvidenceFile",
    "EvidenceNote",
    "SNIPPET_CHARS",
    "SNIPPET_FLOOR",
    "TITLE_CHARS",
    "shorten",
    "snippet",
]

# Target snippet length. The band the spec asks for is 200-300 characters:
# long enough to carry a claim with its qualifier ("the floor is 0.6, raised
# from 0.55 in March"), short enough that five notes and five chunks still
# leave the model room to think.
SNIPPET_CHARS = 250

# A truncated snippet is never cut back past this looking for a word
# boundary. Without a floor, a 250-character run with no space in it would
# collapse to nothing; with one, the worst case is a snippet that ends
# mid-word, which is a much smaller problem than a snippet that ends empty.
SNIPPET_FLOOR = 200

# Notes have no title column, so one is derived from the text. Short enough
# to scan in a list, long enough to tell two notes apart.
TITLE_CHARS = 60

ELLIPSIS = "…"


def shorten(text, limit: int, floor: int = 0) -> str:
    """Collapse whitespace and cut to `limit`, preferring a word boundary.

    Whitespace is collapsed first so that re-wrapping a note -- or storing
    it with Windows line endings -- cannot change its snippet. That is the
    difference between "deterministic" (same input, same output) and
    "stable" (same *content*, same output); a bundle needs both.
    """
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed

    cut = collapsed[:limit]
    boundary = cut.rfind(" ")
    if boundary >= floor:
        cut = cut[:boundary]
    return cut.rstrip(" ,;:.-") + ELLIPSIS


def snippet(text) -> str:
    """The 200-300 character extract shown to the model for one item."""
    return shorten(text, SNIPPET_CHARS, SNIPPET_FLOOR)


@dataclass(frozen=True)
class EvidenceNote:
    """One note ARIA wrote down, as the synthesis prompt sees it."""

    id: int
    title: str
    snippet: str
    score: float
    domain: ExpansionDomain
    provenance: str

    # What this piece of evidence is itself about, from the same classifier
    # that segments the conversation. Defaulted so an EvidenceNote can still
    # be built without one, and set by the bundle builder for everything
    # that comes out of retrieval.
    #
    # Distinct from `domain`, which is a property of the *query*: every item
    # in a bundle shares the query's domain, while topics vary item by item.
    # That difference is exactly what makes "this evidence spans two topics"
    # a question worth asking.
    topic: str = ""

    @property
    def label(self) -> str:
        """How this is cited in prose: "note 12"."""
        return f"note {self.id}"


@dataclass(frozen=True)
class EvidenceFile:
    """One chunk of a document the user ingested."""

    path: str
    section_heading: str
    snippet: str
    score: float
    domain: ExpansionDomain
    provenance: str
    topic: str = ""

    @property
    def label(self) -> str:
        """How this is cited in prose: the provenance string itself.

        A file has no short name a reader would recognise the way "note 12"
        is recognisable, so the citation and the provenance are the same
        string rather than two things to keep in step.
        """
        return self.provenance


@dataclass(frozen=True)
class EvidenceBundle:
    """Everything retrieval found for one query, normalized and capped.

    meta carries the conditions the bundle was built under -- embedding
    backend, retrieval floors, how many candidates were dropped by the caps,
    when it was built. None of it is evidence, so none of it reaches the
    prompt; it exists so a bundle that produced a strange answer can be read
    back later and explained.
    """

    query: str
    cleaned_query: str
    domain: ExpansionDomain
    notes: list[EvidenceNote] = field(default_factory=list)
    files: list[EvidenceFile] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    # The past conversation turns worth carrying into this answer, best
    # first. Empty when no conversation was supplied, which is what keeps a
    # caller that never had one seeing exactly the bundle it always saw.
    turns: list = field(default_factory=list)

    def __bool__(self) -> bool:
        """True when there is anything at all to synthesize from."""
        return bool(self.notes or self.files)

    @property
    def is_empty(self) -> bool:
        return not self

    @property
    def item_count(self) -> int:
        return len(self.notes) + len(self.files)

    @property
    def topics(self) -> list[str]:
        """Every topic represented in the evidence, first-appearance order.

        More than one means retrieval reached across the conversation's
        topics. That is not wrong -- a note about a build error can be the
        right answer to a Unity question -- but it is worth the model
        knowing, because evidence from another topic is the kind that looks
        relevant and is not.
        """
        seen: list[str] = []
        for item in (*self.notes, *self.files):
            if item.topic and item.topic not in seen:
                seen.append(item.topic)
        return seen

    @property
    def current_topic(self) -> str:
        """The conversation topic this bundle was retrieved for, or ""."""
        return str(self.meta.get("current_topic") or "")

    @property
    def turn_topics(self) -> list[str]:
        """Every topic the carried turns come from, first-appearance order."""
        seen: list[str] = []
        for turn in self.turns:
            if turn.topic and turn.topic not in seen:
                seen.append(turn.topic)
        return seen

    @property
    def turn_goals(self) -> list[str]:
        """Every goal the carried turns belong to, first-appearance order."""
        seen: list[str] = []
        for turn in self.turns:
            if turn.goal and turn.goal not in seen:
                seen.append(turn.goal)
        return seen

    @property
    def current_goal(self) -> str:
        """What the user is working on, or "" when unknown."""
        return str(self.meta.get("current_goal") or "")

    @property
    def goal_topic(self) -> str:
        """The topic that goal belongs to, or "".

        Usually the conversation's current topic. The two differ when a
        continuity marker has held a goal on its original topic across a
        turn that named none -- and when they differ, this is the one
        evidence should be measured against, because it is the topic of the
        work rather than of the last sentence.
        """
        return str(self.meta.get("goal_topic") or "")

    @property
    def off_goal_items(self) -> list:
        """Evidence from outside the topic the current goal belongs to."""
        goal_topic = self.goal_topic
        if not goal_topic:
            return []
        return [
            item
            for item in (*self.notes, *self.files)
            if item.topic and item.topic != goal_topic
        ]

    @property
    def off_topic_items(self) -> list:
        """Evidence whose own topic is not the conversation's."""
        current = self.current_topic
        if not current:
            return []
        return [
            item
            for item in (*self.notes, *self.files)
            if item.topic and item.topic != current
        ]

    @property
    def domains(self) -> list[ExpansionDomain]:
        """Every domain represented, in first-appearance order.

        More than one means the evidence is speaking two vocabularies, which
        the synthesis rules ask the model to reconcile rather than blend.
        """
        seen: list[ExpansionDomain] = []
        for item in (*self.notes, *self.files):
            if item.domain not in seen:
                seen.append(item.domain)
        return seen
