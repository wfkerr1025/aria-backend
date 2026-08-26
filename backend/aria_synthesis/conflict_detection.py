"""ARIA Lite Phase 6.2 - rule-based conflict detection.

Finds places where two sources say incompatible things about the same
subject, so synthesis can surface the disagreement instead of quietly
picking whichever scored higher.

The design is dominated by one asymmetry: a missed conflict costs the user a
caveat, a false conflict costs them their trust in the answer. Being told
"your sources disagree" about two statements that are both true is worse
than not being told anything, because it makes every future warning
worthless. So every rule here is built to be strict, and each one is
allowed to fire only when the two statements are talking about the same
thing in nearly the same words.

Two kinds are detected:

    NUMERIC   the same quantity given two values -- "3 steps" vs "4 steps",
              "02:00 UTC" vs "06:00 UTC"
    POLARITY  the same proposition asserted and denied -- "requires a
              rebuild" vs "does not require a rebuild", "deprecated" vs
              "active"

Both gates are deliberately conservative:

    NUMERIC needs the measured noun to match *and* at least two other
    content words in common. Without the second half, "the build takes 3
    minutes" and "the deploy takes 5 minutes" would be reported as a
    contradiction, because they share the word "minutes".

    POLARITY needs the two propositions to be otherwise identical -- at most
    SYMMETRIC_DIFFERENCE_MAX content words between them, and that word must
    be one that dates a claim rather than narrowing its subject. Without the
    count, "the pipeline runs nightly" and "the pipeline does not run on
    weekends" would conflict; without the restriction, "notes are indexed"
    and "empty notes are not indexed" would. Both pairs are entirely true.
    What survives this gate is the case where the only difference between two
    sentences is that one of them is negated, which is what a contradiction
    actually looks like.

Rule-based and deterministic throughout: no model is consulted about
whether two sentences disagree, because a model that is occasionally
creative about that is a model that occasionally invents a disagreement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

try:
    from backend.aria_synthesis.evidence_bundle import EvidenceBundle
    from backend.llm.query_expansion import STOPWORDS
except ImportError:  # running from inside the backend directory
    from aria_synthesis.evidence_bundle import EvidenceBundle
    from llm.query_expansion import STOPWORDS

__all__ = [
    "ANTONYMS",
    "AUXILIARIES",
    "Conflict",
    "ConflictStatement",
    "KIND_NUMERIC",
    "KIND_POLARITY",
    "MIN_SHARED_TERMS",
    "NEGATORS",
    "NON_RESTRICTIVE",
    "SYMMETRIC_DIFFERENCE_MAX",
    "detect_conflicts",
    "sentences",
]

KIND_NUMERIC = "numeric"
KIND_POLARITY = "polarity"

# How much two statements must have in common before they are even
# considered to be about the same thing.
MIN_SHARED_TERMS = 2

# For a polarity conflict, how many content words the two statements may
# differ by, and which words those are allowed to be.
#
# The count alone is not enough. "Notes are indexed on save" and "Empty
# notes are not indexed on save" differ by exactly one word and are both
# true: "empty" narrows who the second sentence is about, so it is an
# exception to the first rather than a denial of it. A restrictive qualifier
# is almost always a single word, which is precisely what a bare count lets
# through.
#
# So the differing word must also be one that modifies when a claim holds
# rather than who it is about. "The endpoint is no longer active" contradicts
# "the endpoint is active"; "empty notes are not indexed" does not contradict
# "notes are indexed". Anything not on this list means the two sentences have
# different subjects, and different subjects cannot contradict.
SYMMETRIC_DIFFERENCE_MAX = 1
NON_RESTRICTIVE = frozenset({
    "now", "currently", "still", "already", "again", "today", "recently",
    "anymore", "longer", "yet", "ever", "actually",
})

# Words that flip the sense of the clause they appear in. Contractions are
# listed in the form they survive tokenizing as.
NEGATORS = frozenset({
    "not", "no", "never", "cannot", "cant", "wont", "doesnt", "dont",
    "isnt", "arent", "wasnt", "werent", "without", "none", "neither",
    "nor", "nothing", "unable", "fails", "failed",
})

# Pairs where one word is the negation of the other. Written negative ->
# positive: a statement containing the negative is rewritten to the positive
# with its polarity flipped, so "the endpoint is deprecated" and "the
# endpoint is active" become the same proposition with opposite signs and
# the ordinary polarity rule catches them.
ANTONYMS = {
    "unsupported": "supported",
    "disabled": "enabled",
    "deprecated": "active",
    "inactive": "active",
    "optional": "required",
    "forbidden": "allowed",
    "prohibited": "allowed",
    "blocked": "allowed",
    "unstable": "stable",
    "missing": "present",
    "absent": "present",
    "unavailable": "available",
    "broken": "working",
    "off": "on",
    "invalid": "valid",
    "unsafe": "safe",
    "readonly": "writable",
}

# Skipped when looking backwards for the noun a number describes, so
# "the score floor is 0.6" is a claim about the floor and not about "is".
_COPULAS = frozenset({
    "is", "was", "are", "were", "be", "been", "at", "to", "of", "as",
    "about", "around", "now", "set", "equals", "then", "than",
})

# Grammatical scaffolding, excluded from the subject of a claim. These are
# listed in the form _normalize leaves them in, which is why "does" appears
# as "doe" and "has" as "ha" -- the trailing-s rule runs first.
#
# Excluding them matters most in the negated half of a contradiction. "The
# pipeline does not require a rebuild" carries an auxiliary its affirmative
# twin does not, and counted as a subject word that single "does" is enough
# to make the two sentences look like they are about different things.
AUXILIARIES = frozenset({
    "is", "wa", "was", "are", "were", "be", "been", "being", "am",
    "do", "doe", "does", "did", "ha", "has", "have", "had",
    "will", "would", "can", "could", "should", "shall", "must",
    "may", "might", "it", "its", "there", "that", "this",
})

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+")
_WORD = re.compile(r"[a-z0-9][a-z0-9:.\-']*")
# A number, optionally with a decimal point, colon (times) or percent sign.
_NUMBER = re.compile(r"^\d+(?:[.:]\d+)*%?$")


@dataclass(frozen=True)
class ConflictStatement:
    """One side of a disagreement, and where it came from."""

    provenance: str
    text: str
    score: float
    # The topic of the evidence this sentence came from; "" when unknown.
    topic: str = ""


@dataclass(frozen=True)
class Conflict:
    """Two or more sources saying incompatible things about one subject.

    topic is the shared subject in the words the evidence used, kept for
    display. kind says which rule fired, which is what lets summarization
    pick a sentence pattern without re-deriving why these statements were
    grouped.

    detail carries what the rule matched on -- the measured noun and its
    competing values for a numeric conflict -- so a summary can be written
    without parsing the statements a second time.
    """

    topic: str
    statements: list[ConflictStatement]
    kind: str = KIND_POLARITY
    detail: dict = None  # type: ignore[assignment]
    # True when the disagreeing sources are not about the same conversation
    # topic. Worth separating from an ordinary conflict: two notes on one
    # subject that disagree is a fact in dispute, while a Unity note and a
    # backend note that appear to disagree are quite often talking past each
    # other about different systems that share a word.
    cross_topic: bool = False
    # True when at least one side of the disagreement is evidence from
    # outside the topic the user's current goal belongs to.
    #
    # Named for goals but detected on topics, because that is what the
    # evidence actually carries: a note is filed under a subject, not under
    # a piece of work somebody was doing when they read it. So this means
    # "this disagreement pulls in material from outside what you are working
    # on" -- which is the useful warning -- rather than a claim that two
    # sources were written in pursuit of different goals.
    cross_goal: bool = False

    @property
    def provenances(self) -> list[str]:
        return [statement.provenance for statement in self.statements]

    @property
    def topics(self) -> list[str]:
        """The evidence topics taking part, deduplicated and sorted."""
        return sorted({statement.topic for statement in self.statements if statement.topic})


def sentences(text: str) -> list[str]:
    """Split a snippet into statements, dropping fragments.

    Sentence-level rather than snippet-level because a snippet is up to 250
    characters and usually says several things: comparing whole snippets
    would mean a single shared subject drags every unrelated clause around
    it into the comparison.

    A trailing fragment from a truncated snippet is dropped -- it ends in an
    ellipsis rather than a full stop, and half a sentence cannot be judged
    for what it asserts.
    """
    found = []
    for part in _SENTENCE_SPLIT.split(" ".join(str(text or "").split())):
        cleaned = part.strip()
        if cleaned.endswith("…"):
            continue
        if len(cleaned.split()) >= 3:
            found.append(cleaned)
    return found


def _words(sentence: str) -> list[str]:
    """Lowercased word tokens, with apostrophes folded out.

    "doesn't" has to become "doesnt" rather than "doesn" and "t", or the
    negator list would need an entry per contraction spelling.

    Trailing punctuation is stripped but interior punctuation is kept, so
    "0.6." reads as the number 0.6 and "02:00" as a time, while
    "self-service" stays one word. Leaving the full stop attached would be
    quietly expensive: "deprecated." is not in the antonym table and "0.6."
    is not a number, so a sentence-final claim would never be compared.
    """
    return [
        token.rstrip(".:-")
        for token in _WORD.findall(sentence.lower().replace("'", ""))
        if token.rstrip(".:-")
    ]


def _normalize(word: str) -> str:
    """Fold a word toward its stem, crudely and predictably.

    A trailing "s" comes off anything long enough to survive it, so
    "requires"/"require" and "steps"/"step" compare equal. This is not
    stemming and does not try to be: it is one rule, it is deterministic,
    and its failures ("bus" -> "bu") are harmless because both sides of a
    comparison fail the same way.
    """
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _content_terms(words: list[str]) -> set[str]:
    """The words that carry the subject, normalized.

    Stopwords, negators, auxiliaries and bare numbers are all excluded:
    negators are polarity rather than subject, auxiliaries are grammar, and
    a number is the thing being disagreed about rather than evidence the two
    statements are related.
    """
    terms = set()
    for word in words:
        if word in STOPWORDS or word in NEGATORS or _NUMBER.match(word):
            continue
        normalized = _normalize(word)
        if word in AUXILIARIES or normalized in AUXILIARIES:
            continue
        terms.add(normalized)
    return terms


def _polarity(words: list[str], terms: set[str]) -> tuple[int, set[str]]:
    """Whether this sentence asserts or denies, and its subject either way.

    An antonym is rewritten to its positive counterpart and flips the sign,
    so "deprecated" and "not active" and "active" all end up as claims about
    the same proposition, differing only in polarity.
    """
    sign = -1 if any(word in NEGATORS for word in words) else 1

    rewritten = set(terms)
    for term in terms:
        positive = ANTONYMS.get(term)
        if positive:
            rewritten.discard(term)
            rewritten.add(positive)
            sign = -sign
    return sign, rewritten


def _numbers(words: list[str]) -> dict[str, str]:
    """Measured noun -> value, for every number in the sentence.

    The noun is the word after the number ("3 steps"), or the nearest
    meaningful word before it when the number ends the clause ("the floor is
    0.6"). Copulas are skipped on the way back so the claim attaches to what
    was measured rather than to the verb.
    """
    found: dict[str, str] = {}
    for index, word in enumerate(words):
        if not _NUMBER.match(word):
            continue

        noun = None
        following = words[index + 1] if index + 1 < len(words) else None
        if following and not _NUMBER.match(following) and following not in STOPWORDS:
            noun = following
        else:
            for candidate in reversed(words[:index]):
                if (
                    candidate not in STOPWORDS
                    and candidate not in _COPULAS
                    and not _NUMBER.match(candidate)
                ):
                    noun = candidate
                    break
        if noun:
            # First value wins for a repeated noun, so the reading of a
            # sentence does not depend on how far it was scanned.
            found.setdefault(_normalize(noun), word)
    return found


@dataclass(frozen=True)
class _Claim:
    """A sentence, pre-chewed into everything the rules need."""

    statement: ConflictStatement
    terms: frozenset
    sign: int
    numbers: dict


def _claims(bundle: EvidenceBundle) -> list[_Claim]:
    """Every sentence in the bundle, in bundle order.

    Order matters only for reproducibility: conflicts are sorted before they
    are returned, but building them from a stable sequence means the same
    bundle produces the same grouping every time.
    """
    claims = []
    for item in (*bundle.notes, *bundle.files):
        for sentence in sentences(item.snippet):
            words = _words(sentence)
            sign, terms = _polarity(words, _content_terms(words))
            claims.append(
                _Claim(
                    statement=ConflictStatement(
                        provenance=item.provenance,
                        text=sentence,
                        score=item.score,
                        topic=getattr(item, "topic", "") or "",
                    ),
                    terms=frozenset(terms),
                    sign=sign,
                    numbers=_numbers(words),
                )
            )
    return claims


def _numeric_conflict(left: _Claim, right: _Claim):
    """A shared measured noun given two different values, or None.

    Requires MIN_SHARED_TERMS content words in common *beyond* the noun
    itself. That second condition is what separates "the pipeline has 3
    steps" / "the pipeline has 4 steps" from "the build takes 3 minutes" /
    "the deploy takes 5 minutes": both pairs share a measured noun, only the
    first pair is about the same thing.
    """
    for noun, value in sorted(left.numbers.items()):
        other = right.numbers.get(noun)
        if other is None or other == value:
            continue
        shared = (left.terms & right.terms) - {noun}
        # Either the two sentences say enough of the same things around the
        # number, or they are word-for-word identical apart from it. The
        # second case covers the short claim -- "the score floor is 0.6" --
        # which has almost nothing in it but the number and its noun, and
        # would otherwise never clear a shared-term count.
        if len(shared) < MIN_SHARED_TERMS and left.terms ^ right.terms:
            continue
        return noun, value, other, shared
    return None


def _polarity_conflict(left: _Claim, right: _Claim):
    """The same proposition asserted by one and denied by the other, or None.

    The strictness lives in the symmetric difference: the two sentences must
    be saying the same thing apart from the negation. Anything looser starts
    reporting a general claim and a specific exception to it as a
    contradiction.
    """
    if left.sign == right.sign:
        return None
    shared = left.terms & right.terms
    if len(shared) < MIN_SHARED_TERMS:
        return None
    differing = left.terms ^ right.terms
    if len(differing) > SYMMETRIC_DIFFERENCE_MAX:
        return None
    if differing - NON_RESTRICTIVE:
        # The extra word narrows the subject rather than dating the claim,
        # so these are two statements about different things.
        return None
    return shared


def _topic(shared: set, claims: list[_Claim]) -> str:
    """The shared subject, in the order and wording the evidence used.

    Reconstructed from the highest-scoring statement rather than printed as
    a sorted set, because "radar endpoint" reads as a subject and
    "endpoint, radar" reads as debug output.
    """
    best = max(claims, key=lambda claim: claim.statement.score)
    ordered = []
    for word in _words(best.statement.text):
        normalized = _normalize(word)
        if normalized in shared and normalized not in ordered:
            ordered.append(normalized)
    return " ".join(ordered) or " ".join(sorted(shared))


def detect_conflicts(bundle: EvidenceBundle, goal_topic: str = "") -> list[Conflict]:
    """Find every disagreement between sources in a bundle.

    Compares each sentence against every other sentence from a *different*
    source. Two sentences in one note are not a conflict between sources --
    a note that qualifies itself is doing its job, and reporting it as a
    disagreement would make every carefully written note look unreliable.

    goal_topic marks the disagreements that reach outside the work in
    hand. Defaulted to "" so a caller with no conversation gets exactly the
    behaviour it got before goals existed; the bundle carries it when there
    is one, and the engine passes it through.

    Returns conflicts sorted by kind then topic, each carrying every
    statement that took part, best-scoring first. Empty when nothing
    contradicts, which is the overwhelmingly common case.
    """
    goal_topic = goal_topic or bundle.meta.get("goal_topic") or ""
    claims = _claims(bundle)
    grouped: dict[tuple, dict] = {}

    for index, left in enumerate(claims):
        for right in claims[index + 1:]:
            if left.statement.provenance == right.statement.provenance:
                continue

            numeric = _numeric_conflict(left, right)
            if numeric:
                noun, left_value, right_value, shared = numeric
                key = (KIND_NUMERIC, noun, tuple(sorted(shared)))
                entry = grouped.setdefault(
                    key, {"claims": [], "values": {}, "shared": shared, "noun": noun}
                )
                entry["values"][left_value] = None
                entry["values"][right_value] = None
                for claim in (left, right):
                    if claim not in entry["claims"]:
                        entry["claims"].append(claim)
                continue

            shared = _polarity_conflict(left, right)
            if shared:
                key = (KIND_POLARITY, tuple(sorted(shared)))
                entry = grouped.setdefault(key, {"claims": [], "shared": shared})
                for claim in (left, right):
                    if claim not in entry["claims"]:
                        entry["claims"].append(claim)

    conflicts = []
    for key, entry in grouped.items():
        kind = key[0]
        statements = sorted(
            (claim.statement for claim in entry["claims"]),
            key=lambda statement: (-statement.score, statement.provenance, statement.text),
        )
        detail = {}
        if kind == KIND_NUMERIC:
            detail = {"noun": entry["noun"], "values": sorted(entry["values"])}
        else:
            # The affirmative side, which reads as a proposition a summary
            # can ask "whether" about.
            positive = [
                claim for claim in entry["claims"] if claim.sign > 0
            ] or entry["claims"]
            detail = {
                "affirmative": max(
                    positive, key=lambda claim: claim.statement.score
                ).statement.text
            }

        taking_part = {statement.topic for statement in statements if statement.topic}
        conflicts.append(
            Conflict(
                topic=_topic(entry["shared"], entry["claims"]),
                statements=statements,
                kind=kind,
                detail=detail,
                cross_topic=len(taking_part) > 1,
                cross_goal=bool(goal_topic and taking_part - {goal_topic}),
            )
        )

    conflicts.sort(key=lambda conflict: (conflict.kind, conflict.topic))
    return conflicts
