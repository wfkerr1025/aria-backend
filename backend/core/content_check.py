"""ARIA Lite - is this content safe to put in the project?

Nothing checked what a model wrote before it was written. Measured on
this machine, nemo-12b proposed:

    def open_world:
        print("Opening the world...")

which is not Python -- the parentheses are missing -- and it would have
landed in the project as a file that cannot be imported. "ARIA created
the file" and "ARIA created a working file" were the same claim, and
only the first one was true.

WHAT THIS CHECKS, AND WHAT IT CANNOT
------------------------------------
It answers one question honestly: does this text parse as what its
extension says it is. That catches the failure that actually happens --
a model writing almost-correct code -- and it is exact, because a syntax
error is not a matter of opinion.

It does NOT check whether the code is correct, whether it does what was
asked, or whether it breaks something elsewhere. Those need the test
suite, and running it takes ninety seconds on this project, which is not
something to spend on every turn without being asked. What this does is
make the difference between "written" and "written and at least
parseable" visible, and refuse the auto-create shortcut for anything
that fails.

WHY A FAILURE STAGES RATHER THAN REFUSES
----------------------------------------
A model that writes broken code has still done most of the work, and the
user may want it anyway -- as a starting point, or because the checker
is wrong about a dialect. So a file that does not parse is not thrown
away: it goes to the staging area with the reason attached, where its
diff can be read and committed deliberately. The shortcut is what is
withdrawn, not the work.

Unknown extensions pass. A checker that guessed at file types it does
not understand would block real work to look thorough.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import PurePosixPath

from logger import get_logger

logger = get_logger(__name__)

__all__ = ["CheckResult", "check_content", "describe_problem"]


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    # None when nothing was checked -- an extension with no checker. That
    # is different from "checked and fine", and the caller may want to
    # say so rather than imply a guarantee it does not have.
    checked: bool = False
    problem: str | None = None
    line: int | None = None

    @property
    def failed(self) -> bool:
        return self.checked and not self.ok


def _check_python(content: str) -> CheckResult:
    try:
        ast.parse(content)
        return CheckResult(ok=True, checked=True)
    except SyntaxError as error:
        return CheckResult(
            ok=False, checked=True,
            problem=f"{error.msg}", line=error.lineno,
        )


def _check_json(content: str) -> CheckResult:
    try:
        json.loads(content)
        return CheckResult(ok=True, checked=True)
    except ValueError as error:
        line = getattr(error, "lineno", None)
        return CheckResult(ok=False, checked=True, problem=str(error), line=line)


# Languages with no parser available here. Balanced delimiters is a weak
# check and a real one: the failure a model produces is a truncated file,
# and a truncated file has unbalanced braces.
#
# Only reported when the imbalance is unambiguous. Braces inside strings
# and comments are skipped, because counting them naively would flag
# every file containing a "{" in a string -- a checker that cries wolf
# gets switched off, and then it checks nothing.
_BRACE_LANGUAGES = {"cs", "js", "mjs", "cjs", "jsx", "ts", "tsx", "java",
                    "c", "h", "cpp", "hpp", "cc", "go", "rs", "php", "css", "scss"}


def _strip_strings_and_comments(content: str) -> str:
    out = []
    index = 0
    length = len(content)

    while index < length:
        char = content[index]

        if char in "\"'`":
            quote = char
            index += 1
            while index < length:
                if content[index] == chr(92):
                    index += 2
                    continue
                if content[index] == quote:
                    index += 1
                    break
                index += 1
            continue

        if char == "/" and index + 1 < length and content[index + 1] == "/":
            while index < length and content[index] != "\n":
                index += 1
            continue

        if char == "/" and index + 1 < length and content[index + 1] == "*":
            index += 2
            while index + 1 < length and not (content[index] == "*" and content[index + 1] == "/"):
                index += 1
            index += 2
            continue

        out.append(char)
        index += 1

    return "".join(out)


def _check_braces(content: str) -> CheckResult:
    code = _strip_strings_and_comments(content)

    for opener, closer, name in (("{", "}", "brace"), ("(", ")", "parenthesis"),
                                 ("[", "]", "bracket")):
        difference = code.count(opener) - code.count(closer)
        if difference > 0:
            return CheckResult(
                ok=False, checked=True,
                problem=f"{difference} unclosed {name}{'' if difference == 1 else 's'} "
                        f"- the file looks truncated",
            )
        if difference < 0:
            return CheckResult(
                ok=False, checked=True,
                problem=f"{-difference} unmatched closing {name}"
                        f"{'' if difference == -1 else 'es'}",
            )

    return CheckResult(ok=True, checked=True)


def check_content(path: str, content: str) -> CheckResult:
    """Whether this text is well-formed for the file it is going into.

    Never raises. A checker that fails is reported as "not checked",
    because refusing a turn over a fault in the checker would be worse
    than the thing it is checking for.
    """
    text = str(content or "")
    if not text.strip():
        # An empty file is well-formed. Whether it is WANTED is a
        # different question, and the renderer already says so.
        return CheckResult(ok=True, checked=False)

    suffix = PurePosixPath(str(path or "").replace(chr(92), "/")).suffix.lower().lstrip(".")

    try:
        if suffix == "py":
            return _check_python(text)
        if suffix in ("json", "ipynb"):
            return _check_json(text)
        if suffix in _BRACE_LANGUAGES:
            return _check_braces(text)
    except Exception:  # pragma: no cover - a checker fault is not a verdict
        logger.exception("content check failed for %s; treating it as unchecked", path)
        return CheckResult(ok=True, checked=False)

    return CheckResult(ok=True, checked=False)


def describe_problem(path: str, result: CheckResult) -> str:
    """One line the user can act on."""
    where = f" at line {result.line}" if result.line else ""
    return f"`{path}` does not parse{where}: {result.problem}"
