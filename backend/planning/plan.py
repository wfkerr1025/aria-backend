"""ARIA Lite Phase 8 - the shape of a multi-step answer.

A plan is a short, ordered list of what an answer has to do before it can be
written: which documents to work through, what to conclude from them, and
what to produce at the end.

Worth being precise about what these steps are, because the vocabulary
borrows from tools that execute things and this does not. ARIA does not open
files, edit them or run tests. A step is an instruction to the model about
how to organise the answer it is about to write -- "read build.md" means
"work through what the evidence shows about build.md", and "test" means "say
how the change would be verified". Nothing here performs an action, and a
plan that read as a promise to would be a plan that lies.

Pure data, deliberately. The builder decides what the steps are, the prompt
decides how they are shown, and this module holds neither decision -- which
is what lets a plan be logged, diffed and asserted on without either of the
other two in the room.

Step ids are positional ("step1", "step2") rather than derived from what a
step does. Two reads of two files would collide on any name based on their
kind, and a dependency that points at the wrong step is a much worse bug
than an unmemorable id.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "KIND_ANALYZE",
    "KIND_ANSWER",
    "KIND_EDIT",
    "KIND_READ",
    "KIND_SEARCH",
    "KIND_SUMMARIZE",
    "KIND_TEST",
    "KIND_WEATHER",
    "KINDS",
    "LOOKUP_KINDS",
    "CONVERSATION_TARGET",
    "Plan",
    "PlanStep",
    "step_id",
]

KIND_READ = "read"
KIND_ANALYZE = "analyze"
KIND_EDIT = "edit"
KIND_TEST = "test"
KIND_SUMMARIZE = "summarize"

# Lookups: steps that fetch a fact from outside the evidence bundle rather
# than working through what retrieval already found. Both reach the network,
# which is what separates them from every other kind here -- see the router
# and plan_builder for how narrowly they are allowed to fire.
KIND_SEARCH = "search"
KIND_WEATHER = "weather"

# The kinds that gather external facts, in the order a plan lists them.
LOOKUP_KINDS = (KIND_WEATHER, KIND_SEARCH)

# The single-step plan's kind. Not in the spec's list of the five, but the
# thing a simple question needs is not one of those five -- it is "answer
# this" -- and giving it a kind of its own keeps "did planning decide this
# was simple?" a question with a one-word answer.
KIND_ANSWER = "answer"

KINDS = frozenset({
    KIND_READ, KIND_ANALYZE, KIND_EDIT, KIND_TEST, KIND_SUMMARIZE, KIND_ANSWER,
    KIND_SEARCH, KIND_WEATHER,
})

# The target for a step that works on what was said rather than on a
# document.
CONVERSATION_TARGET = "conversation"


def step_id(position: int) -> str:
    """The id of the step at a given 1-based position."""
    return f"step{position}"


@dataclass(frozen=True)
class PlanStep:
    """One thing the answer has to do."""

    id: str
    kind: str
    target: str
    description: str
    depends_on: list[str] = field(default_factory=list)
    # Arguments a lookup step needs that its target cannot carry: the
    # location for a weather step, the query for a search one. Empty for
    # every other kind, whose target is the whole of what the step is about.
    #
    # Added with a default so a five-field PlanStep -- which is every step
    # any earlier phase builds -- is unchanged.
    args: dict = field(default_factory=dict)

    @property
    def line(self) -> str:
        """How this reads in a plan listing."""
        return f"{self.kind} {self.target}: {self.description}"


@dataclass(frozen=True)
class Plan:
    """The steps an answer works through, in order."""

    steps: list[PlanStep] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.steps)

    def __bool__(self) -> bool:
        return bool(self.steps)

    @property
    def is_single_answer(self) -> bool:
        """True when there is nothing to plan -- one step, answer directly.

        The prompt uses this to decide whether to print a plan at all. A
        one-line plan saying "answer the question" is a heading with no
        information under it, and printing one on every simple query teaches
        a reader to skip the section on the queries where it matters.
        """
        return len(self.steps) == 1 and self.steps[0].kind == KIND_ANSWER

    @property
    def kinds(self) -> list[str]:
        return [step.kind for step in self.steps]

    @property
    def targets(self) -> list[str]:
        return [step.target for step in self.steps]

    def by_id(self, step_id_: str) -> PlanStep | None:
        for step in self.steps:
            if step.id == step_id_:
                return step
        return None

    def of_kind(self, kind: str) -> list[PlanStep]:
        return [step for step in self.steps if step.kind == kind]

    @property
    def dependencies_resolve(self) -> bool:
        """Whether every depends_on points at an earlier step in this plan.

        Both halves matter. A dependency on a step that does not exist is a
        broken plan; one on a *later* step is a plan that cannot be worked
        through in the order it is written, which is the only order anything
        downstream reads it in.
        """
        seen: set[str] = set()
        for step in self.steps:
            if any(required not in seen for required in step.depends_on):
                return False
            seen.add(step.id)
        return True
