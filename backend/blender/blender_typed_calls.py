"""Typed calls for Blender, the way the Unity bridge already has them.

The natural-language mapper in front of blender_actions handles one simple
action per sentence, and loses the rest of it:

    "add a sphere named Boulder and apply a decimate modifier"
        -> add_sphere(name="Boulder and apply a decimate modifier")

That is fine for "make me a cube" and useless for building a prop, which is
twenty operations with real numbers on them. This reads the same syntax the
Unity side takes:

    AddCube("BenchTop", size=1, location=[0, 0, 0.9])
    Scale("BenchTop", 1.2, 0.1, 0.7)
    ApplyBevel("BenchTop", amount=0.02, segments=2, apply=True)
    ExportGlb("Assets/Models/bench.glb", "BenchTop")

The vocabulary is DERIVED from blender_actions by inspection rather than
written out. The Unity tables have to be hand-written because the Python
parameter names and the arguments the C# bridge receives differ; here the
function IS the target, so deriving it means the two can never drift.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any, Optional

from backend.core.call_syntax import Unmappable, literal, parse_calls

__all__ = [
    "Unmappable",
    "OPERATIONS",
    "typed_names",
    "parse_blender_calls",
    "run_blender_calls",
    "answer_typed",
]

# Not operations: plumbing, or things that take a callback.
_NOT_OPERATIONS = frozenset({
    "BlenderUnavailable", "PLUGIN_ID", "answer_request", "output_dir",
    "blender_path", "run_actions",
})


# NAME FIRST, deliberately unlike the Python signatures.
#
# add_cube is (size, location, name), so AddCube("BenchTop", ...) would
# read the name as the size and then be told size was given twice. Every
# other Create* in ARIA -- CreateGameObject, CreateCamera, and CreateLight
# after it was fixed -- takes the name first, because that is what someone
# typing these lines means. This table is the TYPED-CALL order and is not
# obliged to match the signature.
POSITIONAL_ORDER: dict[str, tuple[str, ...]] = {
    "add_cube": ("name", "size", "location"),
    "add_plane": ("name", "size", "location"),
    "add_sphere": ("name", "radius", "location"),
    "add_cylinder": ("name", "radius", "depth", "location"),
    "add_torus": ("name", "major_radius", "minor_radius", "location"),
}


def _pascal(snake: str) -> str:
    """add_cube -> AddCube, smart_uv_project -> SmartUvProject."""
    return "".join(part.capitalize() for part in snake.split("_"))


def _camel(snake: str) -> str:
    """parent_obj -> parentObj, so either spelling is accepted."""
    head, _, tail = snake.partition("_")
    return head + "".join(part.capitalize() for part in tail.split("_") if part)


class Operation:
    """One callable Blender action and how its arguments may be spelled."""

    def __init__(self, typed_name: str, function) -> None:
        self.typed_name = typed_name
        self.function = function
        self.python_name = function.__name__

        signature = inspect.signature(function)
        parameters = tuple(signature.parameters)

        override = POSITIONAL_ORDER.get(self.python_name)
        if override is not None and set(override) <= set(parameters):
            # Named parameters keep working either way; only the order
            # unnamed values are read in changes.
            self.order = override + tuple(p for p in parameters if p not in override)
        else:
            self.order = parameters

        # Both snake_case and camelCase reach the same parameter, because
        # a person typing calls should not have to remember which the
        # Python side happened to use.
        self.aliases: dict[str, str] = {}
        for parameter in self.order:
            self.aliases[parameter.lower()] = parameter
            self.aliases[_camel(parameter).lower()] = parameter

    def bind(self, node: ast.Call) -> dict:
        """The keyword arguments this call means, or Unmappable."""
        arguments: dict[str, Any] = {}

        if len(node.args) > len(self.order):
            raise Unmappable(
                f"{self.typed_name} takes at most {len(self.order)} unnamed "
                f"values ({', '.join(self.order)}); {len(node.args)} were given.")

        for index, value in enumerate(node.args):
            parameter = self.order[index]
            arguments[parameter] = literal(value, f"{self.typed_name}'s {parameter}")

        for keyword in node.keywords:
            if keyword.arg is None:
                raise Unmappable(f"{self.typed_name} does not take **kwargs.")

            parameter = self.aliases.get(keyword.arg.lower())
            if parameter is None:
                raise Unmappable(
                    f"{self.typed_name} has no {keyword.arg} argument. "
                    f"It takes: {', '.join(self.order)}.")

            if parameter in arguments:
                # The same trap CreateLight hit on the Unity side: a value
                # given positionally and again by name, silently winning
                # whichever way the code happened to read it.
                raise Unmappable(
                    f"{self.typed_name} was given {parameter} twice -- once as an "
                    f"unnamed value and again by name. Unnamed values are read in "
                    f"this order: {', '.join(self.order)}.")

            arguments[parameter] = literal(keyword.value,
                                           f"{self.typed_name}'s {parameter}")

        return arguments


def _build() -> dict[str, Operation]:
    from backend.blender import blender_actions

    built: dict[str, Operation] = {}
    for name in getattr(blender_actions, "__all__", []):
        if name in _NOT_OPERATIONS:
            continue

        function = getattr(blender_actions, name, None)
        if not inspect.isfunction(function):
            continue

        typed = _pascal(name)
        built[typed] = Operation(typed, function)

    return built


OPERATIONS: dict[str, Operation] = _build()


def typed_names() -> dict[str, str]:
    """Lower-case spelling -> canonical typed name."""
    return {name.lower(): name for name in OPERATIONS}


def parse_blender_calls(text: Any) -> Optional[list[dict]]:
    """The operations in a message that is already typed-call syntax.

    None when the message is not calls at all -- so "how does AddCube
    work?" still reaches a model. Unmappable when it plainly is calls and
    one of them is wrong, because being wrong is something to say rather
    than something to pass on.
    """
    calls = parse_calls(text, typed_names())
    if calls is None:
        return None

    planned: list[dict] = []
    for typed_name, node in calls:
        operation = OPERATIONS[typed_name]
        planned.append({
            "operation": typed_name,
            "function": operation.python_name,
            "args": operation.bind(node),
        })

    return planned


def run_blender_calls(planned: list[dict]) -> list[dict]:
    """Run planned operations in order, stopping at the first failure.

    Stops rather than continuing because these are construction steps: a
    bevel applied to an object the previous line failed to create is not
    a partial success, it is a different object.
    """
    results: list[dict] = []

    for step in planned:
        operation = OPERATIONS[step["operation"]]
        try:
            outcome = operation.function(**step["args"])
            ok = not isinstance(outcome, dict) or outcome.get("ok", True)
            results.append({"operation": step["operation"], "ok": bool(ok), "result": outcome})
            if not ok:
                break
        except Exception as failure:  # a Blender fault is a result, not a crash
            results.append({
                "operation": step["operation"],
                "ok": False,
                "result": {"error": str(failure)},
            })
            break

    return results


def answer_typed(text: Any) -> Optional[dict]:
    """Handle a message of typed calls, or hand the turn back with None."""
    try:
        planned = parse_blender_calls(text)
    except Unmappable as refused:
        return {"ran": False, "text": "I did not run anything. " + str(refused)}

    if not planned:
        return None

    results = run_blender_calls(planned)
    done = sum(1 for result in results if result["ok"])

    if done == len(planned):
        return {"ran": True,
                "text": f"Ran {done} Blender operation(s): "
                        + ", ".join(step["operation"] for step in planned) + "."}

    failed = next(result for result in results if not result["ok"])
    detail = failed["result"].get("error") if isinstance(failed["result"], dict) else ""

    return {"ran": done > 0,
            "text": f"Ran {done} of {len(planned)} operations, then {failed['operation']} "
                    f"failed{': ' + detail if detail else ''}. Nothing after it ran."}
