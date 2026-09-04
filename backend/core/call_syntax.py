"""Reading a message that IS calls, rather than a sentence about them.

The Unity side proved the shape: a person types

    CreateGameObject("OreNode", primitive="Sphere", position={"x":0,...})

and that is not a request to a model, it is an instruction. Parsing it
exactly beats routing it to something that will paraphrase it.

This module is the generic half of that -- reading `Name(args)` statements
out of a message with Python's own parser, so quoting, nesting and dicts
behave the way anyone typing them expects. What the names MEAN is the
caller's business.

Three outcomes, and the difference between the last two is the point:

    None        no statement is a call, so a question about AddCube
                reaches a model instead of this layer
    a list      every statement mapped
    Unmappable  these ARE calls and one defeated me -- the caller must
                say so rather than hand a broken message onward

That third outcome exists because the alternative was measured: one
unmappable line sent a whole message of good commands to a small model,
which began writing a scene file by hand.

`backend/unity/unity_editor_actions.py` still carries its own copy of this
logic. Unifying them is worth doing behind the test suite; it was not
worth risking the working Unity path to write this one.
"""

from __future__ import annotations

import ast
import re
from typing import Any, Optional

__all__ = [
    "Unmappable",
    "parse_calls",
    "strip_code_fence",
]


class Unmappable(ValueError):
    """A message that IS calls, and that could not be read as any."""


_FENCE = re.compile(r"^\s*```[^\n]*\n(?P<body>.*?)\n?\s*```\s*$", re.S)


def strip_code_fence(text: Any) -> str:
    """The body of a fenced block, or the text unchanged."""
    body = str(text or "").strip()
    if not body:
        return ""

    match = _FENCE.match(body)
    return match.group("body").strip() if match else body


def _names_a_call(piece: str, canonical: dict[str, str]) -> Optional[str]:
    """The canonical name a statement opens with, or None."""
    head = piece.lstrip()
    match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", head)
    if not match:
        return None

    return canonical.get(match.group(1).lower())


def _closes(text: str) -> bool:
    """Whether brackets balance, so a call may span several lines."""
    depth = 0
    quote = ""
    escaped = False

    for character in text:
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue

        if character in "\"'":
            quote = character
        elif character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1

    return depth <= 0 and not quote


def _statements(body: str, canonical: dict[str, str]) -> list[str]:
    """Split a message into statements, keeping multi-line calls whole."""
    statements: list[str] = []
    current: list[str] = []

    for line in body.splitlines():
        stripped = line.strip()

        if not current:
            if not stripped:
                continue
            current.append(line)
        else:
            current.append(line)

        joined = "\n".join(current)
        if _closes(joined):
            statements.append(joined.strip())
            current = []

    if current:
        statements.append("\n".join(current).strip())

    return [statement for statement in statements if statement]


def _call_node(statement: str) -> Optional[ast.Call]:
    """The call in a statement, or None when it is not one."""
    try:
        tree = ast.parse(statement.strip(), mode="eval")
    except SyntaxError:
        return None

    node = tree.body
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return None

    return node


def parse_calls(text: Any, names: dict[str, str]) -> Optional[list[tuple[str, ast.Call]]]:
    """Every call in a message, as (canonical name, AST node).

    `names` maps lower-case spellings to canonical ones, which is what
    decides whether a message is this layer's business at all.

    Raises Unmappable when the message clearly carries calls but one of
    them does not parse.
    """
    body = strip_code_fence(text)
    if not body:
        return None

    statements = _statements(body, names)
    if not statements:
        return None

    read: list[tuple[str, Optional[ast.Call]]] = []
    for statement in statements:
        name = _names_a_call(statement, names)
        node = _call_node(statement) if name else None
        read.append((statement, (name, node) if name and node else None))

    if not any(call for _statement, call in read):
        return None

    calls: list[tuple[str, ast.Call]] = []
    for statement, call in read:
        if call is None:
            # A message is one thing or the other. Guessing which lines
            # were meant to run is how a sentence becomes an edit.
            raise Unmappable(
                "I can run calls or read a sentence, not both in one message. "
                f"I did not know what to do with: {statement}")
        calls.append(call)

    return calls


def literal(node: ast.AST, where: str) -> Any:
    """A literal argument, or Unmappable saying which one was not."""
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        raise Unmappable(
            f"{where} must be a plain value -- a number, string, list or "
            f"dict -- not an expression.") from None
