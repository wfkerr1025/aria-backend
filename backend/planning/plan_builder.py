"""ARIA Lite Phase 8 - deciding whether an answer needs a plan.

Most questions do not. One document, one thing asked, one answer -- planning
that is overhead with a heading on it. So the builder's first and most
important decision is to produce nothing: a single "answer" step, which the
prompt renders as no section at all.

A plan appears when the evidence spans more than one document. That is the
case where an unplanned answer goes wrong in a specific and recognisable
way: the model reads the highest-scoring chunk, answers from it, and never
returns to the other three files -- so the answer is about `build.md` and
silently ignores `deploy.md`, which said the opposite. Naming a read step
per document is what makes the omission visible.

Files, not notes. A file chunk is an excerpt -- the model is shown 250
characters of a document that may run to forty pages, so "work through
build.md" is a real instruction with real content behind it. A note is small
and already quoted whole; a read step for something wholly present in the
prompt would be theatre.

What the final step is comes from what was asked, in this order:

    An edit word wins. "Fix the pipeline and explain why" is a request to
    change something that also wants an explanation, and an answer shaped as
    an explanation would not contain the change.

    Otherwise it summarises. That is the shape of every question that is not
    asking for a change, which is most of them.

    The goal breaks a tie when the query says nothing. "Continue" asks for
    no shape at all, and the work in hand is the only remaining evidence of
    what kind of answer is wanted -- the same fallback the answer templates
    use, for the same reason.

A test step is added only behind an edit. "Why are the tests failing" names
tests and asks for a diagnosis, not a test run; adding a verification step
to an explanation would be a step the answer cannot take.

No model, no clock, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import replace

try:
    from backend.core import search_intent as _search_intent
    from backend.llm.query_expansion import DOMAIN_MARKERS, ExpansionDomain
    from backend.planning.plan import (
        CONVERSATION_TARGET,
        KIND_ANALYZE,
        KIND_ANSWER,
        KIND_ACTION,
    KIND_EDIT,
        KIND_READ,
        KIND_SEARCH,
        KIND_SUMMARIZE,
        KIND_TEST,
        KIND_WEATHER,
        Plan,
        PlanStep,
        step_id,
    )
except ImportError:  # running from inside the backend directory
    from core import search_intent as _search_intent
    from llm.query_expansion import DOMAIN_MARKERS, ExpansionDomain
    from planning.plan import (
        CONVERSATION_TARGET,
        KIND_ANALYZE,
        KIND_ANSWER,
        KIND_EDIT,
        KIND_READ,
        KIND_SEARCH,
        KIND_SUMMARIZE,
        KIND_TEST,
        KIND_WEATHER,
        Plan,
        PlanStep,
        step_id,
    )

__all__ = [
    "EDIT_WORDS",
    "LOCAL_SCOPE_WORDS",
    "MAX_READ_STEPS",
    "SEARCH_WORDS",
    "SUMMARIZE_WORDS",
    "TEST_WORDS",
    "WEATHER_WORDS",
    "PlanBuilder",
    "build_plan",
]

# A request to change something.
EDIT_WORDS = (
    "refactor", "fix", "change", "update", "modify", "rewrite", "patch",
    "implement", "add", "remove", "delete", "rename", "migrate", "port",
    "clean up", "correct", "repair", "adjust",
    # "edit" was missing, which is the plainest way to ask for one.
    # "Edit README.md to add an install section" only classified as an
    # edit because it also happened to say "add"; "edit setup.py to pin
    # the version" classified as a summary.
    "edit",
    # As a phrase, never as the bare verb. "Apply the changes" is a
    # request to act; "how does this apply", "apply the filter" and
    # "applying for a licence" are not, and a bare "apply" would read all
    # four the same way.
    "apply the change", "apply the changes",
)

# A request to explain something. Listed for readability and for the goal
# fallback; the builder treats "no edit word" as summarize anyway.
SUMMARIZE_WORDS = (
    "explain", "summarize", "summarise", "overview", "describe", "compare",
    "document", "what is", "what are", "how does", "why does", "walk me through",
)

# Words that put verification in scope -- but only behind an edit.
TEST_WORDS = (
    "test", "tests", "testing", "failing", "fails", "assert", "assertion",
    "pytest", "unit test", "coverage", "regression", "verify",
)

# Weather vocabulary, taken from the expansion tables rather than retyped.
# Topic classification already decides what counts as a weather message using
# exactly this list, and two lists would drift into disagreeing about whether
# a question is about the weather.
WEATHER_WORDS = DOMAIN_MARKERS[ExpansionDomain.WEATHER]

# A request to go and look something up -- imported, not retyped.
#
# This used to be a separate, deliberately narrower list than the routing
# side's, on the reasoning that a bare word like "search" would over-fire.
# The narrowness was the bug: a query could route as a search and then plan
# no search step, so the turn was classified as needing the web and then
# answered from the model's weights. What actually keeps a bare word safe
# is whole-word matching plus the local-scope veto, and both now live with
# the table in backend/core/search_intent.py.
#
# This is still the only planning decision in the codebase that can put the
# user's text on the wire to a third party. LOCAL_SCOPE_WORDS below is the
# half that keeps it off.
SEARCH_WORDS = _search_intent.WEB_SEARCH_PHRASES

# Phrases that mean the lookup is local. A query naming the user's own
# material is a retrieval question however it is phrased -- "search my notes"
# is a search of the note store, and sending those words to a search engine
# would leak private text to answer a question that was never about the web.
LOCAL_SCOPE_WORDS = _search_intent.LOCAL_SCOPE_PHRASES

# How many read steps a plan will name. Past this the list stops being a
# plan and becomes an inventory, and the bundle's own file cap is five, so
# this is a backstop rather than a limit anyone should meet.
MAX_READ_STEPS = 5


def _normalized(text: str) -> str:
    """Lowercase, punctuation-flattened and padded for whole-word matching."""
    lowered = str(text or "").lower().replace("'", "")
    return f" {' '.join(re.sub(r'[^a-z0-9]+', ' ', lowered).split())} "


def _mentions(text: str, words) -> bool:
    normalized = _normalized(text)
    return any(f" {word} " in normalized for word in words)


# Left over at the front of a query once the shared extractor has taken its
# one prefix off: "search the web for X" becomes "the web for X". Trimmed
# here rather than in the extractor, which the live search path also uses and
# which is not this layer's to reshape.
_SEARCH_FILLER = ("the", "a", "an", "web", "internet", "online", "for", "up", "about", "me")


def _clean_search_terms(terms: str) -> str:
    """Drop leading filler so the query sent is the query meant.

    Falls back to the original string if trimming would empty it -- a query
    of nothing but filler is better sent as the user wrote it than not sent
    at all.
    """
    words = str(terms or "").split()
    while words and words[0].lower().strip(",") in _SEARCH_FILLER:
        words = words[1:]
    return " ".join(words).strip() or str(terms or "").strip()


def _basename(path: str) -> str:
    cleaned = str(path or "").replace("\\", "/").rstrip("/")
    return cleaned.rsplit("/", 1)[-1] or cleaned


class PlanBuilder:
    """Turns a query and its evidence into a plan, or into nothing.

    Stateless. The caps are constructor arguments so a caller working with
    unusually wide evidence can widen the plan with it.
    """

    def __init__(self, max_read_steps: int = MAX_READ_STEPS) -> None:
        self.max_read_steps = max_read_steps

    # ------------------------------------------------------------------
    def documents(self, bundle) -> list[str]:
        """The distinct documents the evidence came from, best-scoring first.

        Deduplicated by path, because several chunks of one file are one
        document to read. Order follows the bundle, which is already sorted
        by score, so the plan works through the most relevant document
        first and two identical bundles always plan identically.
        """
        seen: list[str] = []
        for item in getattr(bundle, "files", None) or []:
            name = _basename(item.path) or item.provenance
            if name and name not in seen:
                seen.append(name)
        return seen[: self.max_read_steps]

    def terminal_kind(self, query: str, current_goal=None) -> str:
        """Whether the answer ends in a change or an explanation."""
        if _mentions(query, EDIT_WORDS):
            return KIND_EDIT

        goal = getattr(current_goal, "goal", None) or current_goal
        if isinstance(goal, str) and not _mentions(query, SUMMARIZE_WORDS):
            # The query asked for nothing in particular; the work in hand is
            # the only thing left that says what kind of answer is wanted.
            if _mentions(goal, EDIT_WORDS):
                return KIND_EDIT
        return KIND_SUMMARIZE

    # --------------------------------------------------------------
    # Actions
    # --------------------------------------------------------------
    # An action step names a registered tool and a target the
    # orchestrator can execute against. Two things it never does, and
    # both are the point:
    #
    #   It does not carry the new contents of a file. This method runs
    #   inside build(), which synthesis_engine calls BEFORE the model has
    #   generated anything -- plan, route, run tools, then assemble the
    #   prompt. At plan time the text to write does not exist. The model
    #   supplies it, and backend/core/action_plan.py reads it back out of
    #   the answer.
    #
    #   It does not invent a path. The target has to be a document the
    #   evidence bundle actually produced, so a plan cannot name a file
    #   nobody has seen. A request to act on something not in evidence
    #   gets no action step, which leaves the turn to answer in prose --
    #   the behaviour it had before actions existed.
    #
    # Deterministic, like the rest of this class: a vocabulary and a
    # lookup, no model and no I/O.
    def action_step(self, query: str, bundle, position: int, current_goal=None):
        """The action this query asks for, or None.

        None is the common case and is not a failure: most turns ask for
        an explanation, and an explanation is not an action.
        """
        documents = self.documents(bundle)

        if self.terminal_kind(query, current_goal) == KIND_EDIT:
            target = self._named_document(query, documents)
            if target is None:
                # Asked for an edit, named nothing that exists. Silent
                # here rather than guessing: picking "the first document
                # in the bundle" would write to a file the user did not
                # name.
                return None
            return PlanStep(
                id=step_id(position),
                kind=KIND_ACTION,
                target=target,
                description=f"Edit {target} as described above.",
                action={
                    "tool": "edit_file",
                    "target": target,
                    # Supplied by the model, not by the planner. See above.
                    "content": None,
                    "args": {"path": target},
                    "preconditions": [f"{target} is in the evidence for this turn"],
                    "postconditions": [],
                },
            )

        if _mentions(query, TEST_WORDS):
            return PlanStep(
                id=step_id(position),
                kind=KIND_ACTION,
                target=CONVERSATION_TARGET,
                description="Run the project's tests.",
                action={
                    "tool": "run_tests",
                    "target": CONVERSATION_TARGET,
                    "content": None,
                    # The scope stays empty: narrowing it means passing a
                    # fragment of the user's sentence to a test runner,
                    # and file_tools validates scope against a character
                    # allowlist precisely because that is not safe to
                    # improvise.
                    "args": {"scope": ""},
                    "preconditions": [],
                    "postconditions": [],
                },
            )

        return None

    def _named_document(self, query: str, documents) -> str | None:
        """The document this query names, or None.

        Matched against what the bundle produced rather than parsed out
        of the sentence. A filename extracted from prose is a filename
        nobody has checked exists.
        """
        lowered = str(query or "").lower()
        for document in documents:
            if document and document.lower() in lowered:
                return document
        return None

    def wants_tests(self, query: str, current_goal=None) -> bool:
        """Whether verification is in scope, from the query or the goal."""
        goal = getattr(current_goal, "goal", None) or current_goal
        if _mentions(query, TEST_WORDS):
            return True
        return isinstance(goal, str) and _mentions(goal, TEST_WORDS)

    # ------------------------------------------------------------------
    def wants_local_material(self, query: str) -> bool:
        """Whether the query points at the user's own material.

        Checked before any lookup fires. "Search my notes for the pipeline"
        contains "search for" and is nonetheless a question about the note
        store, and answering it by querying a search engine would both miss
        the answer and hand a private question to a third party.
        """
        return _mentions(query, LOCAL_SCOPE_WORDS)

    def weather_step(self, query: str, position: int) -> PlanStep | None:
        """A weather lookup for this query, or None.

        Requires a location. The registered weather tool takes one and has
        no useful behaviour without it, so a question with none produces no
        step rather than an invocation guaranteed to fail validation -- the
        model asks which city, exactly as it did before this kind existed.

        Location extraction reuses backend.core.tool_executor, which the
        weather path has always used, rather than adding a second parser
        that could read the same sentence differently.
        """
        if not _mentions(query, WEATHER_WORDS) or self.wants_local_material(query):
            return None

        try:
            from backend.core.tool_executor import extract_weather_location
        except ImportError:  # running from inside the backend directory
            from core.tool_executor import extract_weather_location

        location = extract_weather_location(query)
        if not location:
            return None

        return PlanStep(
            id=step_id(position),
            kind=KIND_WEATHER,
            target=location,
            description=f"Look up current weather for {location}.",
            args={"location": location},
        )

    def search_step(self, query: str, position: int) -> PlanStep | None:
        """A web lookup for this query, or None.

        Fires only on an explicit request, and never when the query named
        the user's own material -- see SEARCH_WORDS and LOCAL_SCOPE_WORDS
        for why both halves are needed.
        """
        # search_activation, not _mentions, so a question the classifier
        # claimed this turn plans the lookup that routing already decided
        # on. It is called without a generator and spends no inference:
        # with nothing primed it returns exactly what _mentions would, so
        # the planner stays deterministic when nothing primed it.
        from backend.core import search_activation

        if not search_activation.wants_web_search(query) or self.wants_local_material(query):
            return None

        try:
            from backend.core.tool_executor import extract_search_query
        except ImportError:  # running from inside the backend directory
            from core.tool_executor import extract_search_query

        terms = _clean_search_terms(
            extract_search_query(query) or str(query or "").strip()
        )
        if not terms:
            return None

        return PlanStep(
            id=step_id(position),
            kind=KIND_SEARCH,
            target=CONVERSATION_TARGET,
            description=f"Search the web for: {terms}.",
            args={"query": terms},
        )

    def lookup_steps(self, query: str, start: int = 1) -> list[PlanStep]:
        """The external lookups a query calls for, weather before search.

        A fixed order rather than the order the words appeared in, so the
        same question always produces the same plan.
        """
        steps: list[PlanStep] = []
        for factory in (self.weather_step, self.search_step):
            found = factory(query, start + len(steps))
            if found is not None:
                steps.append(found)
        return steps

    # ------------------------------------------------------------------
    def build(self, query: str, bundle, current_topic: str = "", current_goal=None) -> Plan:
        """The plan for one answer.

        Returns a single "answer" step whenever the evidence is one document
        or none -- there is no order to impose on a single source, and the
        prompt omits the section entirely.
        """
        documents = self.documents(bundle)
        steps: list[PlanStep] = self.lookup_steps(query)

        if len(documents) < 2 and not steps:
            return Plan([
                PlanStep(
                    id=step_id(1),
                    kind=KIND_ANSWER,
                    target=CONVERSATION_TARGET,
                    description="Answer the question directly from the evidence above.",
                )
            ])

        if len(documents) < 2:
            # Lookups with nothing to cross-reference. One terminal step
            # depending on them, rather than an analyze step comparing a
            # single source against itself.
            terminal_kind = self.terminal_kind(query, current_goal)
            steps.append(
                PlanStep(
                    id=step_id(len(steps) + 1),
                    kind=terminal_kind,
                    target=CONVERSATION_TARGET,
                    description=(
                        "Answer using what the lookup returned together with the "
                        "evidence above."
                    ),
                    depends_on=[step.id for step in steps],
                )
            )
            return Plan(steps)

        for document in documents:
            steps.append(
                PlanStep(
                    id=step_id(len(steps) + 1),
                    kind=KIND_READ,
                    target=document,
                    description=f"Work through what the evidence shows about {document}.",
                )
            )

        # The analysis waits on everything gathered before it, lookups
        # included: a comparison that ran before the forecast came back
        # would be comparing the documents against nothing.
        read_ids = [step.id for step in steps]
        analyze = PlanStep(
            id=step_id(len(steps) + 1),
            kind=KIND_ANALYZE,
            target=CONVERSATION_TARGET,
            description=(
                "Compare what the documents say, noting where they agree and "
                "where they do not."
            ),
            depends_on=read_ids,
        )
        steps.append(analyze)

        terminal_kind = self.terminal_kind(query, current_goal)
        terminal = PlanStep(
            id=step_id(len(steps) + 1),
            kind=terminal_kind,
            target=CONVERSATION_TARGET,
            description=(
                "Describe the change to make, and which document each part of it "
                "comes from."
                if terminal_kind == KIND_EDIT
                else "Give one combined answer, citing which document each part came from."
            ),
            depends_on=[analyze.id],
        )
        steps.append(terminal)

        if terminal_kind == KIND_EDIT and self.wants_tests(query, current_goal):
            steps.append(
                PlanStep(
                    id=step_id(len(steps) + 1),
                    kind=KIND_TEST,
                    target=CONVERSATION_TARGET,
                    description="Say how the change would be verified.",
                    depends_on=[terminal.id],
                )
            )

        # The action, last and dependent on the terminal step. Appended
        # rather than replacing anything: the terminal step is where the
        # model says what the change is, and the action is the request to
        # apply it. Dropping the first would leave an action whose
        # content nothing produced.
        action = self.action_step(query, bundle, len(steps) + 1, current_goal)
        if action is not None:
            steps.append(replace(action, depends_on=[terminal.id]))

        return Plan(steps)


def build_plan(query: str, bundle, current_topic: str = "", current_goal=None) -> Plan:
    """Convenience wrapper around a default PlanBuilder."""
    return PlanBuilder().build(query, bundle, current_topic, current_goal)
