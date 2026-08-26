"""ARIA Lite Phase 6.3 - choosing the shape of an answer.

The same evidence answers different questions differently. "How do I set up
the pipeline" wants a numbered sequence; "why is the pipeline failing" wants
causes to check; "should I use addressables or assetbundles" wants criteria
and a recommendation. A model left to decide for itself will usually write
prose, which is the worst of the three for two of the three questions.

So the structure is chosen here, deterministically, and stated in the prompt
as an instruction. Rule-based for the same reason everything else in this
package is: a query that classified as STEPS on Monday and EXPLANATION on
Tuesday would change the shape of ARIA's answer with nothing to point at.

Two signals, in a fixed relationship:

    The query decides when it says anything about form. "How do I", "which
    should I", "compare" are the user telling us what they want, and no
    amount of evidence outvotes that.

    The evidence decides when the query is silent. Most queries are -- "the
    build pipeline" is a topic, not a request for a shape -- and in that
    case what the sources look like is the only evidence available: numbered
    instructions suggest steps, "because" and "caused by" suggest causes.

The spec pairs each template with both a query cue and an evidence cue. They
are treated as alternatives rather than as an AND on purpose: somebody who
asks "how do I rebuild the index" has asked for steps whether or not the
notes happen to be written as a numbered list, and refusing to structure the
answer because the evidence is prose would punish them for how someone else
took notes.
"""

from __future__ import annotations

import re
from enum import Enum

try:
    from backend.aria_synthesis.evidence_bundle import EvidenceBundle
except ImportError:  # running from inside the backend directory
    from aria_synthesis.evidence_bundle import EvidenceBundle

__all__ = [
    "BROAD_ITEM_COUNT",
    "GOAL_VERB_TEMPLATES",
    "EVIDENCE_MARKERS",
    "QUERY_MARKERS",
    "TEMPLATE_PRIORITY",
    "TemplateType",
    "classify_template",
    "evidence_signals",
    "query_signals",
    "template_for_goal",
]


class TemplateType(Enum):
    """The shape an answer should take."""

    STEPS = "steps"
    DIAGNOSTIC = "diagnostic"
    COMPARISON = "comparison"
    SUMMARY = "summary"
    EXPLANATION = "explanation"
    CHECKLIST = "checklist"
    DECISION_GUIDE = "decision_guide"


# Phrases in the query that ask for a particular shape of answer.
QUERY_MARKERS: dict[TemplateType, tuple[str, ...]] = {
    TemplateType.DECISION_GUIDE: (
        "should i", "should we", "which should", "which one", "which is better",
        "choose", "pick", "recommend", "worth it", "better option",
        "or should",
    ),
    TemplateType.COMPARISON: (
        "compare", "comparison", "difference between", "differences between",
        "vs", "versus", "how does it differ", "same as",
    ),
    TemplateType.CHECKLIST: (
        "checklist", "prerequisite", "prerequisites", "requirements",
        "what do i need", "what is needed", "required for", "before i can",
    ),
    TemplateType.DIAGNOSTIC: (
        "not working", "doesnt work", "does not work", "isnt working",
        "is not working", "broken", "fails", "failing", "failure",
        "error", "errors", "crash", "crashes", "crashing", "debug",
        "troubleshoot", "wont", "will not", "cant", "cannot", "stuck",
        "why is it not", "what went wrong",
        # A negated "why" question is asking after a cause, whatever verb
        # carries the negation. Listed as pairs rather than as a bare
        # "doesnt", which also shows up in ordinary questions about
        # behaviour ("what doesnt get indexed") that want an explanation.
        "why doesnt", "why does not", "why isnt", "why is not",
        "why wont", "why will not", "why cant", "why cannot",
        "why didnt", "why did not",
    ),
    TemplateType.STEPS: (
        "how to", "how do i", "how can i", "how would i", "steps",
        "step by step", "procedure", "process for", "walk me through",
        "set up", "setup", "install", "configure", "guide to",
    ),
    TemplateType.SUMMARY: (
        "summarize", "summarise", "summary", "overview", "brief",
        "briefly", "recap", "tl dr", "tldr", "high level", "gist",
    ),
    TemplateType.EXPLANATION: (
        "what is", "what are", "why does", "why do", "how does", "how do",
        "explain", "what does", "meaning of",
    ),
}

# Signals in the evidence itself, used only when the query asks for nothing.
EVIDENCE_MARKERS: dict[TemplateType, tuple[str, ...]] = {
    TemplateType.STEPS: (
        "first", "then", "next", "finally", "afterwards", "step",
        "before you", "once you",
    ),
    TemplateType.DIAGNOSTIC: (
        "because", "caused by", "due to", "the reason", "fails when",
        "results in", "leads to", "symptom",
    ),
    TemplateType.CHECKLIST: (
        "must", "required", "requires", "prerequisite", "mandatory",
        "depends on", "you need",
    ),
    TemplateType.COMPARISON: (
        "instead of", "alternative", "either", "whereas", "unlike",
        "compared to", "versus",
    ),
    TemplateType.DECISION_GUIDE: (
        "however", "trade-off", "tradeoff", "downside", "drawback",
        "at the cost", "on the other hand", "prefer",
    ),
}

# A numbered or bulleted list in the evidence is the strongest hint that the
# source itself is a procedure.
_ORDERED_LIST = re.compile(r"(?:^|\s)(?:\d+[.)]|[-*])\s+\S")

# How many items make a bundle "broad" enough that a summary is the useful
# shape. Five is the per-kind retrieval cap, so this is reached only when
# both halves contributed or one of them filled up.
BROAD_ITEM_COUNT = 6

# The order signals are tested in, and the whole of how ties are broken.
#
# Sorted by how specifically each marker set states what the user wants,
# most specific first, so that a query carrying two cues gets the answer
# shaped by the narrower one:
#
#   DECISION_GUIDE beats COMPARISON  "which should I use, A or B" is a
#       request for a recommendation; a bare comparison leaves the user to
#       make the call they just asked us to make.
#   CHECKLIST beats DIAGNOSTIC       "what do I need before deploying" is
#       about readiness, even when the reason for asking is a failure.
#   DIAGNOSTIC beats STEPS           "how do I fix the build error" cannot
#       be answered as a procedure until the cause is known, so causes and
#       checks come first. A "how to" with no failure named is still STEPS.
#   SUMMARY last but one             "summarize the deployment steps" is
#       still best served as steps; a numbered list is already a summary.
#
# EXPLANATION is last and is the default: prose is the right answer to a
# question that has not asked for anything else.
TEMPLATE_PRIORITY = (
    TemplateType.DECISION_GUIDE,
    TemplateType.COMPARISON,
    TemplateType.CHECKLIST,
    TemplateType.DIAGNOSTIC,
    TemplateType.STEPS,
    TemplateType.SUMMARY,
    TemplateType.EXPLANATION,
)


def _normalized(text: str) -> str:
    """Lowercase, punctuation-flattened and padded for whole-phrase matching.

    Punctuation becomes whitespace so "A vs. B" and "A vs B" match the same
    marker, and apostrophes are dropped so "doesn't" matches "doesnt". The
    padding is what keeps "vs" from firing inside "versus" handling or
    "cant" inside "cantilever".
    """
    lowered = str(text or "").lower().replace("'", "")
    flattened = re.sub(r"[^a-z0-9]+", " ", lowered)
    return f" {' '.join(flattened.split())} "


def query_signals(query: str) -> list[TemplateType]:
    """Every template the query itself asks for, in priority order."""
    normalized = _normalized(query)
    return [
        template
        for template in TEMPLATE_PRIORITY
        if any(f" {marker} " in normalized for marker in QUERY_MARKERS.get(template, ()))
    ]


def evidence_signals(bundle: EvidenceBundle) -> list[TemplateType]:
    """Every template the evidence suggests, in priority order.

    Read from the snippets rather than the whole note: the snippet is what
    the model will see, so shaping the answer around something outside it
    would promise a structure the evidence in front of the model cannot
    support.
    """
    snippets = [item.snippet for item in (*bundle.notes, *bundle.files)]
    if not snippets:
        return []

    haystack = _normalized(" ".join(snippets))
    signals = [
        template
        for template in TEMPLATE_PRIORITY
        if any(f" {marker} " in haystack for marker in EVIDENCE_MARKERS.get(template, ()))
    ]

    if any(_ORDERED_LIST.search(snippet) for snippet in snippets):
        if TemplateType.STEPS not in signals:
            signals.append(TemplateType.STEPS)

    # Breadth is a property of the bundle rather than of any one snippet:
    # several sources, or more than one domain, and no single thread running
    # through them.
    if len(snippets) >= BROAD_ITEM_COUNT or len(bundle.domains) > 1:
        if TemplateType.SUMMARY not in signals:
            signals.append(TemplateType.SUMMARY)

    return [template for template in TEMPLATE_PRIORITY if template in signals]


# What shape of answer each goal verb asks for.
#
# Separate from QUERY_MARKERS on purpose. A goal is known to begin with an
# action -- goal_classifier only returns strings that do -- so its first word
# is a reliable signal. The same words are not reliable inside a question:
# "what is the build pipeline" opens with a question and merely contains
# "build", and adding "build" to the query markers to catch goals would turn
# that into a numbered procedure.
GOAL_VERB_TEMPLATES: dict[str, TemplateType] = {
    "fix": TemplateType.DIAGNOSTIC,
    "debug": TemplateType.DIAGNOSTIC,
    "investigate": TemplateType.DIAGNOSTIC,
    "trace": TemplateType.DIAGNOSTIC,
    "profile": TemplateType.DIAGNOSTIC,
    "implement": TemplateType.STEPS,
    "build": TemplateType.STEPS,
    "create": TemplateType.STEPS,
    "write": TemplateType.STEPS,
    "add": TemplateType.STEPS,
    "set up": TemplateType.STEPS,
    "setup": TemplateType.STEPS,
    "configure": TemplateType.STEPS,
    "install": TemplateType.STEPS,
    "migrate": TemplateType.STEPS,
    "deploy": TemplateType.STEPS,
    "release": TemplateType.STEPS,
    "refactor": TemplateType.STEPS,
    "rename": TemplateType.STEPS,
    "move": TemplateType.STEPS,
    "remove": TemplateType.STEPS,
    "delete": TemplateType.STEPS,
    "update": TemplateType.STEPS,
    "upgrade": TemplateType.STEPS,
    "clean up": TemplateType.STEPS,
    "finish": TemplateType.STEPS,
    "optimise": TemplateType.STEPS,
    "optimize": TemplateType.STEPS,
    "review": TemplateType.CHECKLIST,
    "test": TemplateType.CHECKLIST,
    "check": TemplateType.CHECKLIST,
    "compare": TemplateType.COMPARISON,
    "summarize": TemplateType.SUMMARY,
    "summarise": TemplateType.SUMMARY,
    "explain": TemplateType.EXPLANATION,
    "document": TemplateType.EXPLANATION,
    "show": TemplateType.EXPLANATION,
    "find": TemplateType.EXPLANATION,
}

_GOAL_VERBS_LONGEST_FIRST = tuple(
    sorted(GOAL_VERB_TEMPLATES, key=lambda verb: len(verb.split()), reverse=True)
)


def template_for_goal(goal: str) -> TemplateType | None:
    """The shape a stated goal asks for, or None if its verb is unknown.

    Read from the goal's first word only. Everything after it is the object
    of the work -- "conflict detection", "the shader error" -- and says what
    the answer is about rather than how it should be arranged.
    """
    normalized = _normalized(goal)
    for verb in _GOAL_VERBS_LONGEST_FIRST:
        if normalized.startswith(f" {verb} "):
            return GOAL_VERB_TEMPLATES[verb]
    return None


def classify_template(query: str, bundle: EvidenceBundle) -> TemplateType:
    """Decide what shape this answer should take.

    The query wins whenever it says anything about form, because it is the
    user stating what they want. Only when it is silent -- a bare topic, a
    noun phrase -- does the evidence get to suggest a shape.

    Falls back to EXPLANATION, which is not a failure: most questions are
    best answered in prose, and imposing a numbered list on one that is not
    makes the answer worse.
    """
    for template in query_signals(query):
        return template

    for template in evidence_signals(bundle):
        return template

    return TemplateType.EXPLANATION
