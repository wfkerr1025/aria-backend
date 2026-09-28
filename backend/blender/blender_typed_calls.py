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

These plan actions, they do not call blender_actions' functions. Each of
those is a thin `_one(...)` wrapper that launches Blender immediately from
--factory-startup, so calling six of them in a row is six Blenders, five of
which open the default scene and cannot find what the first one made:

    RuntimeError: no object called 'BenchTop' -- the scene has: Camera, Cube, Light

A build is one scene. The whole list goes to run_actions once.

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
    "plan_blender_calls",
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

        limit = getattr(self, "max_positional", len(self.order))
        if len(node.args) > limit:
            raise Unmappable(
                f"{self.typed_name} takes at most {limit} unnamed "
                f"value(s) ({', '.join(self.order[:limit])}); {len(node.args)} were "
                f"given. Name the rest: {', '.join(self.order)}.")

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


# The one value a template-only call may give without naming it: the
# thing it acts on. ApplyShrinkwrap("Vest", target="Body") reads as it
# should; a second unnamed value would be a guess about which slot it
# fills, and a wrong guess is silent.
_FIRST_POSITIONAL = ("object", "objects", "mesh", "armature", "material",
                     "image", "lattice", "path", "name")


class TemplateOperation(Operation):
    """An action with no Python wrapper, callable straight from its template.

    Every template takes a dict, so its parameters are read from its
    source (templates.parameters) rather than from a signature. Named
    arguments only, apart from the first -- see _FIRST_POSITIONAL.
    """

    def __init__(self, typed_name: str, action: str) -> None:
        from backend.blender import blender_script_templates as templates

        self.typed_name = typed_name
        self.function = None
        self.python_name = action
        accepted = templates.parameters(action)
        first = next((p for p in _FIRST_POSITIONAL if p in accepted), None)
        self.order = ((first,) if first else ()) + tuple(p for p in accepted if p != first)
        self.max_positional = 1 if first else 0
        self.aliases = {}
        for parameter in self.order:
            self.aliases[parameter.lower()] = parameter
            self.aliases[_camel(parameter).lower()] = parameter


def _build() -> dict[str, Operation]:
    from backend.blender import blender_actions
    from backend.blender import blender_script_templates as templates

    built: dict[str, Operation] = {}
    for name in getattr(blender_actions, "__all__", []):
        if name in _NOT_OPERATIONS:
            continue

        function = getattr(blender_actions, name, None)
        if not inspect.isfunction(function):
            continue

        typed = _pascal(name)
        built[typed] = Operation(typed, function)

    # Everything else the templates can do. A wrapper is a nicety for
    # Python callers; it was never meant to be the gate on what a typed
    # call can reach, and for a while 47 of 84 actions sat behind it.
    wrapped = {operation.python_name for operation in built.values()}
    for action in templates.known_actions():
        if action not in wrapped and _pascal(action) not in built:
            built[_pascal(action)] = TemplateOperation(_pascal(action), action)

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


def plan_blender_calls(text: Any) -> Optional[list[dict]]:
    """The message as blender_actions descriptors, ready for run_actions.

    Every public action is a thin wrapper around _one("<its own name>",
    **kwargs), verified by a test, so the descriptor is mechanical: the
    action is the function name and the params are the bound arguments.
    """
    parsed = parse_blender_calls(text)
    if parsed is None:
        return None

    return [{"action": step["function"], "params": step["args"]} for step in parsed]


def answer_typed(text: Any, *, on_output=None) -> Optional[dict]:
    """Run a message of typed calls, or hand the turn back with None.

    One Blender launch for the whole message, so later steps can see what
    earlier ones built.
    """
    from backend.blender import blender_actions

    try:
        actions = plan_blender_calls(text)
    except Unmappable as refused:
        return {"ran": False, "text": "I did not run anything. " + str(refused)}

    if not actions:
        return None

    # Chat works on ONE scene that carries over between messages -- the
    # "chat" session -- so "ApplyBevel('Box')" can follow the message
    # that made the box, every step keeps a version to undo to, and
    # every step ends with a picture of what it did.
    from backend.blender import blender_session

    named = ", ".join(action["action"] for action in actions)
    result = blender_session.Session().run(actions, on_output=on_output)

    if not result.get("ran"):
        return {"ran": False, "text": (
            "I did not run Blender, and nothing happened.\n\n"
            + str(result.get("error")))}

    if not result.get("success"):
        return {"ran": True, "text": (
            f"I ran Blender with {len(actions)} operation(s) and it failed: "
            + str(result.get("error")) + "\n\nSteps: " + named
            + "\n\nThe scene was left as it was.")}

    made = result.get("created") or []
    exported = result.get("exported") or []

    lines = [f"Ran {len(actions)} Blender operation(s): {named}."]
    if made:
        lines.append("Created: " + ", ".join(made) + ".")
    if exported:
        lines.append("Exported:\n" + "\n".join("- " + path for path in exported))
    picture = blender_session.picture_markdown(result.get("renders") or [])
    if picture:
        lines.append(picture)
    lines.append("Say \"undo in Blender\" to take this step back.")

    return {"ran": True, "text": "\n\n".join(lines), "renders": result.get("renders") or []}
