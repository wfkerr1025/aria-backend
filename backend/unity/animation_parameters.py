"""ARIA Lite - the parameters an animator graph is driven by.

No Unity here. A parameter is a name, a type and a default, and every
mistake worth catching about one -- a type Unity does not have, two
parameters with the same name, a condition comparing a Trigger to a
number -- can be caught before the Editor is asked anything.

WHY THE TYPES ARE A CLOSED SET
------------------------------
AnimatorControllerParameterType has exactly four members. Sending a
fifth reaches Unity as a parameter-validation failure from inside the
Editor, which is a slow and unclear way to learn about a typo. The
allowlist is spelled the way the command wants them, capitalised, so
what is checked here is what goes over.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "ATTACK",
    "DEFAULT_PARAMETERS",
    "NUMERIC_TYPES",
    "Parameter",
    "SPEED",
    "TYPES",
    "as_payloads",
    "find",
    "validate_parameters",
]

# Unity's AnimatorControllerParameterType, spelled as the command takes
# them. The command's own doc says: Float | Int | Bool | Trigger.
TYPES = ("Float", "Int", "Bool", "Trigger")

# The ones a threshold can be compared against. A Trigger has no value
# to compare, and a Bool is on or off -- so "speed > 2.0" is meaningful
# and "attack > 2.0" is not.
NUMERIC_TYPES = ("Float", "Int")


@dataclass(frozen=True)
class Parameter:
    """One animator parameter."""

    name: str
    type: str = "Float"
    default: Any = None

    def payload(self) -> Dict[str, Any]:
        """The arguments add_animator_parameter takes.

        `defaultValue` is dropped for a Trigger: the command documents
        it as ignored there, and sending an ignored argument is a small
        lie about what was configured.
        """
        body: Dict[str, Any] = {"name": self.name, "type": self.type}
        if self.default is not None and self.type != "Trigger":
            body["defaultValue"] = self.default
        return body


# What the spec asks for, and what a locomotion graph needs: one number
# to blend on and one trigger to fire.
SPEED = Parameter("speed", "Float", 0.0)
ATTACK = Parameter("attack", "Trigger")

DEFAULT_PARAMETERS = (SPEED, ATTACK)


def find(parameters: Sequence[Parameter], name: str) -> Optional[Parameter]:
    for parameter in parameters or ():
        if parameter.name == name:
            return parameter
    return None


def validate_parameters(parameters: Sequence[Parameter]) -> List[str]:
    """Everything wrong with a set of parameters, in words.

    Returned rather than raised: a caller assembling a graph wants all
    of the problems at once, not the first one repeatedly.
    """
    problems: List[str] = []
    seen = set()

    for parameter in parameters or ():
        name = str(getattr(parameter, "name", "") or "").strip()
        if not name:
            problems.append("a parameter has no name")
            continue
        if name in seen:
            problems.append(
                f"{name!r} is declared twice -- Unity keeps one and the "
                "graph then reads a parameter nobody set")
        seen.add(name)

        if parameter.type not in TYPES:
            problems.append(
                f"{name!r} is a {parameter.type!r}, which is not one of "
                f"{', '.join(TYPES)}")

        if parameter.type == "Trigger" and parameter.default is not None:
            problems.append(
                f"{name!r} is a Trigger with a default value, which the "
                "command ignores")

    return problems


def as_payloads(parameters: Sequence[Parameter]) -> List[Dict[str, Any]]:
    return [parameter.payload() for parameter in parameters or ()]
