"""ARIA Lite - the Unity Editor Bridge, reached from a chat turn.

aria/unity_editor_bridge.py knows how to drive an OPEN Unity Editor: it
writes ARIA/unity_commands.json and reads ARIA/unity_results.json. It
landed with a command router, a CLI and a test suite, and with nothing in
the chat path importing it. This module is that import.

WHY IT EXISTS
Measured on the developer's machine, with the bridge module present and
unreachable. Typed into chat:

    OpenScene("Assets/Scenes/SampleScene.unity")
    SaveScene("Assets/Scenes/WorkshopScene.unity")
    DeleteGameObject("Main Camera")
    DeleteGameObject("Directional Light")

Nothing owned that vocabulary, so the turn went to nemo-12b, which
matched the only shape it had -- a file path -- and began WRITING
Assets/Scenes/SampleScene.unity, generating scene YAML a token at a time.
It ran out of context part-way through. Had it not, it would have
overwritten a real scene with invented text. The truncation was the lucky
part.

WHAT THIS LAYER OWNS, AND WHAT IT DOES NOT
It owns text that is ALREADY bridge call syntax -- a message whose every
statement is `CommandName(...)` with literal arguments. That is an
unambiguous instruction, it maps to commands without a judgement call,
and no model belongs between it and the editor.

It does not own plain language. "delete the main camera" goes through the
registered unity_editor_command tool instead, so the orchestrator decides
whether the sentence was an instruction or a question about one. The
router that maps such a sentence lives in the bridge module
(parse_unity_command); this module does not keep a second copy of it.

Nothing here is best-effort. Every branch that did not reach the editor
says so in its first sentence, because reporting work that never happened
is the failure the whole file exists to prevent.
"""

from __future__ import annotations

import ast
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "ARGUMENTS",
    "POSITIONAL",
    "Unmappable",
    "answer_request",
    "describe_results",
    "parse_bridge_calls",
    "run_description",
    "send",
]


class Unmappable(ValueError):
    """A message that IS bridge calls, and that I could not turn into commands.

    Distinct from "not my business", which is None. The distinction is
    the whole point, and it cost a scene to learn -- see parse_bridge_calls.
    """


# ======================================================
# The call vocabulary
#
# Both tables mirror _CommandBuilder in aria/unity_editor_bridge.py:
# ARGUMENTS is every keyword its methods send, POSITIONAL is the order
# their signatures accept them in. They are written out rather than
# derived, because the Python parameter name and the argument the bridge
# receives differ (component_type -> componentType) and that mapping
# lives inside each method body. test_unity_editor_actions.py holds both
# tables to COMMANDS, so a command added to the bridge and forgotten here
# fails a test rather than a turn.
# ======================================================

ARGUMENTS: dict[str, tuple[str, ...]] = {
    "Ping": (),
    "CreateGameObject": ("name", "parent", "position", "rotation", "scale",
                         "primitive", "components"),
    "DeleteGameObject": ("target",),
    "AddComponent": ("target", "componentType", "allowDuplicate"),
    "RemoveComponent": ("target", "componentType"),
    "SetTransform": ("target", "position", "rotation", "scale", "local"),
    "SetField": ("target", "componentType", "field", "value"),
    "GetField": ("target", "componentType", "field"),
    "GetHierarchy": ("target", "depth"),
    "OpenScene": ("path", "additive", "discardChanges"),
    "SaveScene": ("path", "saveAll"),
    "CreatePrefab": ("target", "prefabPath", "connect"),
    "ModifyPrefab": ("prefabPath", "operations", "stopOnError"),
    "InstantiatePrefab": ("prefabPath", "name", "parent", "position", "rotation"),
    "CreateLight": ("type", "name", "parent", "position", "rotation", "intensity",
                    "color", "range", "spotAngle", "shadows"),
    "CreateCamera": ("name", "parent", "position", "rotation", "fov", "clearFlags",
                     "backgroundColor", "main", "near", "far", "orthographic",
                     "orthographicSize"),
}

POSITIONAL: dict[str, tuple[str, ...]] = {
    "Ping": (),
    "CreateGameObject": ("name", "parent", "position", "rotation", "scale"),
    "DeleteGameObject": ("target",),
    "AddComponent": ("target", "componentType"),
    "RemoveComponent": ("target", "componentType"),
    "SetTransform": ("target", "position", "rotation", "scale"),
    "SetField": ("target", "componentType", "field", "value"),
    "GetField": ("target", "componentType", "field"),
    "GetHierarchy": ("target",),
    "OpenScene": ("path",),
    "SaveScene": ("path",),
    "CreatePrefab": ("target", "prefabPath"),
    "ModifyPrefab": ("prefabPath", "operations"),
    "InstantiatePrefab": ("prefabPath", "name", "parent", "position", "rotation"),
    "CreateLight": ("type", "name", "position", "rotation", "intensity", "color"),
    "CreateCamera": ("name", "position", "rotation", "fov", "clearFlags"),
}


# ======================================================
# Reading the message
# ======================================================

def _without_code_fence(text: Any) -> str:
    """The message, with one wrapping ``` fence removed.

    Someone pasting commands into chat often fences them, and a fence is
    punctuation around the instruction rather than part of it.
    """
    body = str(text or "").strip()
    if not body.startswith("```"):
        return body
    lines = body.splitlines()[1:]
    while lines and lines[-1].strip() in {"```", ""}:
        lines.pop()
    return "\n".join(lines).strip()


def _statements(body: str) -> list[str]:
    """One call per entry, split on the two separators a person uses."""
    found = []
    for line in body.splitlines():
        for part in line.split(";"):
            part = part.strip().rstrip(",")
            if part:
                found.append(part)
    return found


def _call_to(statement: str, canonical: dict[str, str]):
    """(command name, the parsed call) when the statement is one of ours.

    None means the statement is not a bridge call at all -- prose, a
    sentence with a call quoted inside it, a name this bridge does not
    have. That is an ownership question, and it is answered by the SHAPE
    of the statement, never by whether its arguments turned out to work.
    """
    try:
        node = ast.parse(statement, mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return None
    name = canonical.get(node.func.id.lower())
    return None if name is None else (name, node)


_ORDINALS = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth"}


def _given_twice(name: str, key: str, order: tuple[str, ...]) -> str:
    """The collision that cost a scene, explained with the line that works.

    CreateGameObject and CreateCamera take the name first. CreateLight
    takes the light TYPE first and the name second. Someone writing the
    three together puts the name first in all of them, so

        CreateLight("DirectionalLight", type="Directional", intensity=1.2)

    hands type both ways. The bridge's own signature is the odd one out
    here, so the message says which order it wants rather than implying
    the person got it wrong.
    """
    place = order.index(key) + 1
    ordinal = _ORDINALS.get(place, f"{place}th")
    return (f"{name} was given {key} twice -- as its {ordinal} unnamed value and "
            f"again by name. Unnamed values are read in this order: "
            f"{', '.join(order)}. Naming them avoids the question: "
            f"{name}({', '.join(item + '=...' for item in order)}).")


def _arguments(name: str, node: ast.Call, statement: str) -> dict:
    """The call's arguments, or Unmappable saying what is wrong with the line.

    ast is the whole safety story: the statement is PARSED and its values
    read with literal_eval, never executed, so
    `OpenScene(__import__("os").system("del *"))` is a line this refuses
    rather than a hole.
    """
    if any(isinstance(arg, ast.Starred) for arg in node.args):
        raise Unmappable(f"{statement} unpacks its arguments, and I would have to "
                         f"run something to find out what they are. Write them out.")
    if any(keyword.arg is None for keyword in node.keywords):
        raise Unmappable(f"{statement} unpacks its keywords, and I would have to "
                         f"run something to find out what they are. Write them out.")

    try:
        positional = [ast.literal_eval(arg) for arg in node.args]
        supplied = {keyword.arg: ast.literal_eval(keyword.value)
                    for keyword in node.keywords}
    except ValueError:
        raise Unmappable(
            f"{name} takes plain values -- text, numbers, lists and objects. "
            f"{statement} passes something I would have to run to find out.")

    order = POSITIONAL[name]
    if len(positional) > len(order):
        raise Unmappable(
            f"{name} reads at most {len(order)} unnamed value"
            f"{'' if len(order) == 1 else 's'} ({', '.join(order) or 'none'}), and "
            f"{statement} gives {len(positional)}.")
    args: dict[str, Any] = dict(zip(order, positional))

    # componentType, component_type and componenttype are the same
    # argument. The bridge's own spelling is what gets sent.
    known = {key.lower(): key for key in ARGUMENTS[name]}
    for raw, value in supplied.items():
        key = known.get(raw.replace("_", "").lower())
        if key is None:
            raise Unmappable(f"{name} has no {raw} argument. It takes: "
                             f"{', '.join(ARGUMENTS[name]) or 'nothing'}.")
        if key in args:
            raise Unmappable(_given_twice(name, key, order))
        args[key] = value

    return args


def _command(name: str, args: dict) -> dict:
    from aria.unity_editor_bridge import make_command

    if name == "SetField":
        # value is the one argument whose None is a value rather than an
        # omission -- clearing an object reference is SetField(..., None).
        # make_command drops Nones, so this one is built by hand, exactly
        # as _CommandBuilder.set_field does.
        return {"command": "SetField",
                "args": {key: value for key, value in args.items()
                         if key == "value" or value is not None}}
    return make_command(name, **args)


def parse_bridge_calls(text: Any) -> Optional[list[dict]]:
    """The commands in a message that is already bridge call syntax.

    Three outcomes, and the difference between the last two is the whole
    reason this function was rewritten:

        None            not my business -- no statement is a bridge call,
                        so a question about OpenScene reaches a model
        a list          every statement mapped
        Unmappable      these ARE bridge calls and one of them defeated
                        me; the caller must answer, not hand the turn on

    WHY THE THIRD OUTCOME EXISTS
    It used to return None for a broken line too, and the second measured
    scene-write came straight out of that. Typed into chat:

        CreateCamera("MainCamera", position={"x":0,"y":1.6,"z":-3}, ...)
        CreateLight("DirectionalLight", type="Directional", ...)

    CreateLight reads the light type as its first unnamed value, so type
    arrived twice, so that one statement did not map -- and the whole
    message, four good commands included, went to nemo-12b, which started
    writing Assets/Scenes/WorkshopScene.unity as YAML.

    A message carrying a bridge call is this layer's business even when
    it is wrong. Being wrong is something to say, not something to pass
    to a model.
    """
    from aria.unity_editor_bridge import COMMANDS

    body = _without_code_fence(text)
    if not body:
        return None

    canonical = {name.lower(): name for name in COMMANDS}
    statements = _statements(body)
    calls = [(statement, _call_to(statement, canonical)) for statement in statements]
    if not any(call for _statement, call in calls):
        return None

    commands = []
    for statement, call in calls:
        if call is None:
            # A message is one thing or the other. Guessing which lines
            # were meant to run is how a sentence becomes an edit.
            raise Unmappable(
                f"I can run bridge commands or read a sentence, not both in one "
                f"message. I did not know what to do with: {statement}")
        name, node = call
        commands.append(_command(name, _arguments(name, node, statement)))
    return commands


# ======================================================
# Saying what happened
# ======================================================

def _target_of(command: dict) -> str:
    """The one argument worth naming in a one-line summary."""
    args = command.get("args") or {}
    for key in ("path", "target", "name", "prefabPath"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def describe_results(commands, results) -> str:
    """One line per command: what it was, and what the editor said back.

    Commands with no answer are listed as not reached, rather than left
    out. A batch that stopped half way through is a thing the user has to
    know the shape of, because the scene is now in neither state.
    """
    answers = list(results or [])
    lines = []
    for index, command in enumerate(commands):
        heading = f"{command.get('command', '?')} {_target_of(command)}".strip()
        if index >= len(answers):
            lines.append(f"- not reached  {heading}")
            continue
        answer = answers[index]
        note = answer.message or ""
        lines.append(f"- {'ok' if answer.success else 'FAILED'}  {heading}"
                     f"{': ' + note if note else ''}")
    return "\n".join(lines)


def _nothing_happened(reason: str) -> dict:
    return {"ran": False,
            "text": f"I did not send anything to Unity, and nothing changed.\n\n{reason}"}


# ======================================================
# The turn
# ======================================================

def answer_request(text: Any, *, bridge=None) -> Optional[dict]:
    """Answer a message that is bridge call syntax, or hand the turn back.

    Returns None when the message is not this layer's business, and
    {"ran": bool, "text": str} when it is.
    """
    try:
        commands = parse_bridge_calls(text)
    except Unmappable as error:
        # Bridge calls I could not read. Answering is the point: silence
        # here is what sent the last one to a model, which wrote a scene.
        return _nothing_happened(str(error))
    if commands is None:
        return None

    from aria import unity_editor_bridge as ueb

    try:
        bridge = bridge if bridge is not None else ueb.get_bridge()
    except ueb.UnityBridgeUnavailable as error:
        return _nothing_happened(str(error))
    except Exception as error:  # pragma: no cover - a resolver, not logic
        logger.exception("could not open the Unity Editor Bridge")
        return _nothing_happened(str(error) or "I could not find a Unity project.")

    if not bridge.is_installed():
        return _nothing_happened(
            f"The bridge script is not installed in {bridge.project_root}. Run "
            f"`python -m aria.unity_editor_bridge --install`, let Unity compile it, "
            f"then ask again.")

    return send(commands, bridge)


def send(commands: list[dict], bridge) -> dict:
    """Send parsed commands and report what the editor did with them."""
    from aria import unity_editor_bridge as ueb

    try:
        results = bridge.send(commands)
    except ueb.UnityBridgeTimeout:
        # The commands file is written and still sitting there. Saying so
        # is the difference between "nothing happened" and "nothing has
        # happened YET", and the two have different next steps.
        return {"ran": False, "text": (
            f"Unity did not answer within {bridge.timeout:.0f} seconds, so I do not "
            f"know whether any of it ran.\n\n"
            f"The commands are waiting in {bridge.commands_path}. Open the project "
            f"in Unity and run ARIA > Run Bridge Commands, or ask me again with the "
            f"editor open.")}
    except ueb.UnityBridgeError as error:
        # stop_on_error is on, so this is "up to here, and then it stopped".
        return {"ran": True, "text": (
            f"Unity refused a command and stopped there.\n\n"
            f"{describe_results(commands, error.results)}\n\n{error}")}
    except Exception as error:  # pragma: no cover - the file half, not the editor
        logger.exception("the Unity Editor Bridge exchange failed")
        return {"ran": False,
                "text": f"I could not complete the exchange with Unity: {error}"}

    if results is None:
        return {"ran": False, "text": (
            f"I wrote the commands to {bridge.commands_path} and did not wait for "
            f"them. Run ARIA > Run Bridge Commands in Unity to apply them.")}

    count = len(commands)
    return {"ran": True, "text": (
        f"Ran {count} command{'' if count == 1 else 's'} in the open Unity editor.\n\n"
        f"{describe_results(commands, results.results)}")}


def run_description(description: str, *, bridge=None) -> dict:
    """A plain-language request, routed by the bridge and sent.

    This is what the registered unity_editor_command tool calls. The
    routing is the bridge's own parse_unity_command; the only thing this
    adds is the honest reporting the typed path gets.
    """
    from aria import unity_editor_bridge as ueb

    try:
        commands = ueb.parse_unity_command(description)
    except ueb.UnroutableCommand as error:
        return _nothing_happened(str(error))

    try:
        bridge = bridge if bridge is not None else ueb.get_bridge()
    except ueb.UnityBridgeUnavailable as error:
        return _nothing_happened(str(error))

    if not bridge.is_installed():
        return _nothing_happened(
            f"The bridge script is not installed in {bridge.project_root}.")

    return send(commands, bridge)
