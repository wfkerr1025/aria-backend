"""ARIA Lite - the shape of an animator graph, before Unity sees it.

Still no Unity. A graph is states and the transitions between them,
and almost everything worth knowing about one can be decided here: a
transition to a state nobody declared, a condition on a parameter that
was never added, a Trigger compared against a number, two default
states, a state with no way out.

WHY A STATE WITH NO WAY OUT IS CHECKED
--------------------------------------
The four transitions this pipeline was specified with are

    Idle -> Walk    when speed > 0.1
    Walk -> Run     when speed > 2.0
    Run  -> Idle    when speed < 0.1
    Attack          from any state, on the trigger

which is a graph a character can enter Attack in and never leave, and
where Walk only reaches Idle by first speeding up into Run. Both are
almost certainly not what anybody wants, and both are invisible until
somebody plays it.

So `default_graph` adds the return edges by default, and `graph_advice`
reports any state with no outgoing transition however the graph was
built. It is advice rather than a refusal because Unity accepts such a
graph and a caller may mean it -- but following a specification exactly
and quietly producing a character stuck in its attack animation is not
a service either, so it is said.

`validate_graph` is the other half: what Unity would REFUSE, or what
is internally inconsistent. The two are kept apart so a caller can be
warned without being blocked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.unity import animation_parameters as params

__all__ = [
    "ALL_STATES",
    "ANY_STATE",
    "ENTRY",
    "EXIT",
    "MODES",
    "MAX_STATES",
    "MAX_TRANSITIONS",
    "Condition",
    "Graph",
    "State",
    "Transition",
    "default_graph",
    "graph_advice",
    "validate_graph",
]

# The two special names add_animator_transition accepts for fromState,
# and the one it accepts for toState.
ANY_STATE = "AnyState"
ENTRY = "Entry"
EXIT = "Exit"

# The command's own list: If | IfNot | Greater | Less | Equals | NotEqual.
MODES = ("If", "IfNot", "Greater", "Less", "Equals", "NotEqual")

# Modes that compare against a number, and so need one.
THRESHOLD_MODES = ("Greater", "Less", "Equals", "NotEqual")

# Modes that ask whether a Trigger or Bool is set, and so must not
# carry a threshold.
FLAG_MODES = ("If", "IfNot")

# A ceiling on the graph, for the same reason there is one on spending.
# Nothing here costs money, but a loop that adds states adds them to
# the user's project, and a controller with four hundred states is not
# something anybody asked for.
MAX_STATES = 32
MAX_TRANSITIONS = 128


@dataclass(frozen=True)
class Condition:
    """One condition on a transition."""

    parameter: str
    mode: str = "If"
    threshold: Optional[float] = None

    def payload(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {"parameter": self.parameter, "mode": self.mode}
        if self.threshold is not None:
            body["threshold"] = self.threshold
        return body


@dataclass(frozen=True)
class State:
    """One animator state, and the clip it plays."""

    name: str
    clip: Optional[str] = None
    is_default: bool = False


@dataclass(frozen=True)
class Transition:
    """One edge. `source` may be AnyState; `target` may be Exit.

    `can_transition_to_self` is false by default and Unity's default is
    TRUE, which is the wrong way round for every edge here. An AnyState
    transition that may re-enter its own destination restarts that clip
    on every frame its condition holds -- an attack that never gets
    past its first few frames, a fall that keeps starting over.
    """

    source: str
    target: str
    conditions: Tuple[Condition, ...] = ()
    has_exit_time: bool = False
    exit_time: float = 0.0
    duration: float = 0.25
    can_transition_to_self: bool = False


@dataclass
class Graph:
    """A whole animator layer."""

    states: List[State] = field(default_factory=list)
    transitions: List[Transition] = field(default_factory=list)

    @property
    def names(self) -> List[str]:
        return [state.name for state in self.states]

    @property
    def default_state(self) -> Optional[State]:
        for state in self.states:
            if state.is_default:
                return state
        return None


# Every state this module knows how to wire, in the order a character
# uses them. Passing a subset is normal -- an edge whose endpoints are
# not both present is simply not built.
ALL_STATES = ("Idle", "Walk", "Run", "Attack", "Crouch", "CrouchWalk",
              "Jump", "Fall", "Land", "DodgeLeft", "DodgeRight")


def default_graph(state_names: Sequence[str] = ("Idle", "Walk", "Run", "Attack"),
                  *, clips: Optional[Dict[str, str]] = None,
                  complete: bool = True) -> Graph:
    """The locomotion graph this pipeline was specified with.

    The four transitions in the specification are built whenever the
    states they name are present. `complete` adds the edges that make
    it a graph a character can actually live in:

        Walk -> Idle    slowing down without speeding up first
        Run  -> Walk    slowing down out of a run
        Attack -> Idle  on exit time, so an attack ends

    Without them Attack is a trap and Walk cannot reach Idle. They are
    separable rather than assumed, because a caller building a graph
    for something that is not a character may genuinely want neither.
    """
    wanted = [str(name).strip() for name in state_names if str(name).strip()]
    clips = clips or {}

    states = [State(name, clip=clips.get(name), is_default=(index == 0))
              for index, name in enumerate(wanted)]
    present = set(wanted)
    transitions: List[Transition] = []

    def edge(source, target, *conditions, **rest):
        if source in present or source == ANY_STATE:
            if target in present or target == EXIT:
                transitions.append(
                    Transition(source, target, tuple(conditions), **rest))

    # The specification's four.
    edge("Idle", "Walk", Condition("speed", "Greater", 0.1))
    edge("Walk", "Run", Condition("speed", "Greater", 2.0))
    edge("Run", "Idle", Condition("speed", "Less", 0.1))
    edge(ANY_STATE, "Attack", Condition("attack", "If"))

    if complete:
        edge("Walk", "Idle", Condition("speed", "Less", 0.1))
        edge("Run", "Walk", Condition("speed", "Less", 2.0))
        # No condition: it leaves when the clip has played.
        edge("Attack", "Idle", has_exit_time=True, exit_time=0.9)

    # --- crouching ---------------------------------------------------
    edge("Idle", "Crouch", Condition("crouch", "If"))
    edge("Crouch", "Idle", Condition("crouch", "IfNot"))
    edge("Crouch", "CrouchWalk", Condition("speed", "Greater", 0.1))
    edge("CrouchWalk", "Crouch", Condition("speed", "Less", 0.1))
    # Standing up while moving goes to Walk, not to Idle: a character
    # that stops dead because it stood up is a bug somebody spends an
    # afternoon on.
    edge("CrouchWalk", "Walk", Condition("crouch", "IfNot"))

    # --- the air -----------------------------------------------------
    # Jump is a trigger from anywhere. Falling is a state of the world
    # and is driven by `grounded`, but NOT from AnyState:
    #
    #   AnyState -> Fall on !grounded fires the frame after a jump
    #   leaves the ground and overrides the Jump state immediately.
    #   Reported as "while running, if I jump, he does the falling
    #   animation" -- the jump was playing for about one frame.
    #
    # So falling is entered from the GROUNDED states, which is where
    # walking off a ledge actually happens, and a jump reaches it
    # through Jump -> Fall on exit time instead.
    edge(ANY_STATE, "Jump", Condition("jump", "If"))
    edge("Jump", "Fall", has_exit_time=True, exit_time=0.8)
    for grounded_state in ("Idle", "Walk", "Run", "Crouch", "CrouchWalk"):
        edge(grounded_state, "Fall", Condition("grounded", "IfNot"))
    edge("Fall", "Land", Condition("grounded", "If"))
    edge("Land", "Idle", has_exit_time=True, exit_time=0.8)

    # A jump with no Fall state to go to still has to end somewhere.
    if "Fall" not in present:
        edge("Jump", "Idle", has_exit_time=True, exit_time=0.9)
    if "Land" not in present:
        edge("Fall", "Idle", Condition("grounded", "If"))

    # --- dodges ------------------------------------------------------
    edge(ANY_STATE, "DodgeLeft", Condition("dodgeLeft", "If"))
    edge("DodgeLeft", "Idle", has_exit_time=True, exit_time=0.9)
    edge(ANY_STATE, "DodgeRight", Condition("dodgeRight", "If"))
    edge("DodgeRight", "Idle", has_exit_time=True, exit_time=0.9)

    return Graph(states=states, transitions=transitions)


def validate_graph(graph: Graph,
                   parameters: Sequence[params.Parameter]) -> List[str]:
    """Everything wrong with a graph, in words.

    Checked here rather than in Unity because every one of these
    reaches the Editor as a parameter-validation failure with no
    indication of which of eleven commands caused it.
    """
    problems: List[str] = []

    if not graph.states:
        problems.append("the graph has no states")
        return problems

    if len(graph.states) > MAX_STATES:
        problems.append(
            f"{len(graph.states)} states is more than the {MAX_STATES} this "
            "will build -- raise MAX_STATES if that is really wanted")
    if len(graph.transitions) > MAX_TRANSITIONS:
        problems.append(
            f"{len(graph.transitions)} transitions is more than the "
            f"{MAX_TRANSITIONS} this will build")

    seen = set()
    for state in graph.states:
        if not str(state.name or "").strip():
            problems.append("a state has no name")
        elif state.name in seen:
            problems.append(f"{state.name!r} is declared twice")
        seen.add(state.name)

    defaults = [state.name for state in graph.states if state.is_default]
    if not defaults:
        problems.append(
            "no state is the default, so the layer starts wherever Unity "
            "happens to put it")
    elif len(defaults) > 1:
        problems.append(
            f"{len(defaults)} states are marked default: {', '.join(defaults)}")

    known = {parameter.name: parameter for parameter in (parameters or ())}

    for transition in graph.transitions:
        where = f"{transition.source} -> {transition.target}"

        if transition.source not in seen and transition.source != ANY_STATE:
            problems.append(f"{where}: there is no state called "
                            f"{transition.source!r}")
        if transition.target not in seen and transition.target != EXIT:
            problems.append(f"{where}: there is no state called "
                            f"{transition.target!r}")

        for condition in transition.conditions:
            parameter = known.get(condition.parameter)
            if parameter is None:
                problems.append(
                    f"{where}: no parameter called {condition.parameter!r} "
                    "has been declared")
                continue

            if condition.mode not in MODES:
                problems.append(
                    f"{where}: {condition.mode!r} is not one of "
                    f"{', '.join(MODES)}")
                continue

            if condition.mode in THRESHOLD_MODES:
                if parameter.type not in params.NUMERIC_TYPES:
                    problems.append(
                        f"{where}: {condition.parameter!r} is a "
                        f"{parameter.type} and {condition.mode} compares "
                        "against a number")
                elif condition.threshold is None:
                    problems.append(
                        f"{where}: {condition.mode} needs a threshold")
            elif condition.mode in FLAG_MODES and condition.threshold is not None:
                problems.append(
                    f"{where}: {condition.mode} does not take a threshold")

    return problems


def graph_advice(graph: Graph) -> List[str]:
    """What is legal, builds, and is probably not what anybody meant.

    KEPT APART FROM validate_graph ON PURPOSE. Unity accepts both of
    these and so does this pipeline: a caller who asks for the four
    transitions in the specification and nothing else gets exactly
    that, and gets told what it will do. Refusing would be substituting
    our judgement for theirs; staying silent would ship a character
    stuck in its attack animation.
    """
    advice: List[str] = []

    leaves = {transition.source for transition in graph.transitions}
    if ANY_STATE in leaves:
        # An AnyState edge leaves every state, but it does not get you
        # OUT of the one it lands in.
        pass
    for state in graph.states:
        if state.name not in leaves:
            advice.append(
                f"nothing leaves {state.name!r}, so a character that enters "
                "it stays there")

    for transition in graph.transitions:
        if not transition.conditions and not transition.has_exit_time:
            advice.append(
                f"{transition.source} -> {transition.target} has no condition "
                "and no exit time, so it fires immediately")

    return advice
