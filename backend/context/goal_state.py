"""ARIA Lite Phase 7.2 - what the user is currently trying to do.

Topic segmentation answers "what is this conversation about". A goal
answers the more useful question: "what are they trying to get done". The
two come apart constantly. A whole Unity conversation can contain three
separate goals -- fix a shader, then set up addressables, then understand
why the build is slow -- and answering the third with evidence gathered for
the first is a failure that topic scoping alone cannot catch.

A stack rather than a single value, because goals nest. Someone fixing a
shader hits a build error, asks about the build error, and then goes back
to the shader; the shader goal was never abandoned, it was suspended. A
stack keeps the suspended one rather than overwriting it, which is what
makes returning to it possible.

Everything here is frozen. A goal is a record of something the user said,
and an update is a new record rather than an edit to the old one -- which
means a stack can be logged at one turn and compared against the next
without the earlier copy having quietly changed underneath.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

__all__ = [
    "SOURCE_CARRY_FORWARD",
    "SOURCE_EXPLICIT",
    "SOURCE_IMPLICIT",
    "SOURCE_RESUMPTION",
    "GoalStack",
    "GoalState",
]

# How a goal came to be the current one. Recorded because the three deserve
# different amounts of trust: a goal the user stated is worth more than one
# inferred from a topic change, and a reader debugging a strange answer wants
# to know which they are looking at.
SOURCE_EXPLICIT = "explicit"          # the user said what they wanted
SOURCE_CARRY_FORWARD = "carry-forward"  # nothing new was said; it still stands
SOURCE_IMPLICIT = "implicit"          # inferred from a topic change
SOURCE_RESUMPTION = "resumption"      # the user came back to suspended work


@dataclass(frozen=True)
class GoalState:
    """One thing the user is trying to get done."""

    goal: str
    topic: str
    created_at: float = 0.0
    last_updated: float = 0.0
    explicit: bool = False
    continuity_source: str = SOURCE_EXPLICIT

    def touched(self, at: float, source: str) -> "GoalState":
        """A copy marked as still current, as of `at`.

        Returns a new state rather than mutating: the old one may already be
        held by a caller that logged it, and a goal that changed after being
        recorded would make the log a description of the present rather than
        of the moment it was written.
        """
        return replace(self, last_updated=at, continuity_source=source)

    def moved_to(self, topic: str, at: float, source: str) -> "GoalState":
        """A copy of this goal now being pursued under a different topic."""
        return replace(self, topic=topic, last_updated=at, continuity_source=source)


@dataclass(frozen=True)
class GoalStack:
    """The goals in play, oldest at the bottom, current on top."""

    stack: list[GoalState] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.stack)

    def __bool__(self) -> bool:
        return bool(self.stack)

    @property
    def current(self) -> GoalState | None:
        """The goal being pursued now, or None on an empty stack."""
        return self.stack[-1] if self.stack else None

    @property
    def goals(self) -> list[str]:
        return [state.goal for state in self.stack]

    def pushed(self, state: GoalState) -> "GoalStack":
        """A new stack with `state` on top."""
        return GoalStack(stack=[*self.stack, state])

    def replaced_top(self, state: GoalState) -> "GoalStack":
        """A new stack with the current goal swapped for an updated copy."""
        if not self.stack:
            return GoalStack(stack=[state])
        return GoalStack(stack=[*self.stack[:-1], state])

    def resume(self) -> GoalState | None:
        """The most recent goal the user actually stated, below the top one.

        This is what a resumption marker resumes. Searched from just under
        the top downwards, so a detour that buried two pieces of work
        returns to the nearer of them.

        Only explicit goals qualify. An implicit goal is a guess made from a
        topic change, and resuming a guess would let "anyway" walk the
        conversation back into a subject the user never asked to work on.

        Returns None when there is nothing to go back to, which is not a
        failure: a resumption marker with no suspended work is just a word.

        Does not mutate, and does not pop -- it answers "which goal", and
        `resumed_to` is what reshapes the stack around the answer.
        """
        for state in reversed(self.stack[:-1]):
            if state.explicit:
                return state
        return None

    def find_suspended_explicit_goal_by_topic(self, topic: str) -> GoalState | None:
        """The nearest suspended goal the user stated on a given topic.

        Searched from the top downwards, skipping the top itself: the goal
        currently in play is what a resumption is moving *away* from, and
        letting it match would make "anyway" a no-op that reported itself as
        a return.

        Explicit only, for the same reason resume() is. An implicit goal is
        a guess made from a topic change, and naming a topic is not a reason
        to go back to a guess -- the ordinary topic rules already handle
        that case, and better.

        This is what lets "anyway, the prefab is broken too" find the shader
        goal: the message names Unity, and Unity is where the suspended work
        was left.
        """
        if not topic:
            return None
        for state in reversed(self.stack[:-1]):
            if state.explicit and state.topic == topic:
                return state
        return None

    def resumed_to(
        self,
        state: GoalState,
        at: float | None = None,
        source: str = SOURCE_RESUMPTION,
    ) -> "GoalStack":
        """A stack with `state` moved to the top and the detour dropped.

        The detour is the *implicit* goals stacked above `state` -- all of
        them, not one, since a wandering conversation leaves several and
        surfacing the second-most-recent is nobody's idea of going back.

        Explicit goals are never removed, wherever they sit. A goal the user
        stated is work they asked for and have not said they abandoned;
        going back to something earlier suspends it rather than deleting it,
        exactly as starting it suspended what came before. So resuming a
        goal from under a stated one keeps that one below, still findable.

        Passing `at` also marks the resumed goal as current as of that
        moment, which is what a caller resuming work wants; leaving it out
        restructures the stack and changes nothing about the goal itself.
        Either way a new state is built rather than the old one edited.
        """
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index] is not state:
                continue
            kept = [
                *self.stack[:index],
                *(item for item in self.stack[index + 1:] if item.explicit),
                state.touched(at, source) if at is not None else state,
            ]
            return GoalStack(stack=kept)
        return self

    def find(self, topic: str) -> GoalState | None:
        """The most recent goal on a topic, or None.

        Searched from the top down, so returning to a suspended topic finds
        the goal that was suspended rather than the first one ever set on it.
        """
        for state in reversed(self.stack):
            if state.topic == topic:
                return state
        return None
