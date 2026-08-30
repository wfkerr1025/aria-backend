"""ARIA Lite - keeping the action block off the screen while it streams.

Unbuffering the stream brought back a problem buffering had hidden: the
action block is part of the token stream, so the user watches

    ```json
    {"tool": "edit_file", "path": "hello_world.py", "conte

arrive one fragment at a time, and then sees it replaced a second later
by "Done: created hello_world.py". Machine syntax scrolling past is
exactly what action_render exists to prevent, and streaming had put it
back.

So the fence is withheld as it arrives. Prose streams live, a ```python
block streams live -- that is code the user asked about -- and a ```json
block emits nothing at all. The revision at the end of the turn supplies
the description in its place.

WHY THIS IS NOT ANSWERSTREAM
----------------------------
AnswerStream removes scaffolding the model should never have written:
leaked role markers, echoed prompt sections. What it drops is noise.

This drops something real and wanted -- the action -- because the reader
should see its EFFECT rather than its syntax. Different reason, different
lifetime (this one is per-stream state, not a filter table), and mixing
them would mean a component with two purposes and one name.

WHAT IT COSTS
-------------
A few characters of latency at a backtick. When "```" arrives the filter
cannot yet know whether the language tag is json, so it holds until the
tag is complete -- at most one short line. Everything else passes
through untouched, in the same fragments it arrived in.
"""

from __future__ import annotations

__all__ = ["ActionBlockFilter"]

# Long enough for "```" plus the longest language tag worth waiting for.
# A tag longer than this is not one we suppress, so there is no reason to
# keep holding.
_MAX_TAG = 16

_TEXT, _TAG, _SUPPRESS, _CODE = "text", "tag", "suppress", "code"


class ActionBlockFilter:
    """Streams prose and code through; swallows ```json blocks.

    Fed the same fragments the provider produces, in order. Returns what
    may be shown now, which is often "" while a fence marker is being
    resolved.
    """

    def __init__(self) -> None:
        self._state = _TEXT
        self._held = ""
        # True once a json fence has been seen, so the caller can tell
        # "nothing was suppressed" from "an action was hidden".
        self.suppressed_a_block = False

    def push(self, fragment: str) -> str:
        out: list[str] = []

        for char in str(fragment or ""):
            if self._state == _TEXT:
                if char == "`":
                    self._held += char
                    if self._held == "```":
                        self._state = _TAG
                        self._held = ""
                    continue
                if self._held:
                    # Backticks that turned out not to open a fence --
                    # inline code, or a stray one. They are the user's
                    # text and go out as written.
                    out.append(self._held)
                    self._held = ""
                out.append(char)
                continue

            if self._state == _TAG:
                self._held += char
                if char == "\n" or len(self._held) > _MAX_TAG:
                    tag = self._held.strip().lower()
                    if tag.startswith("json") or tag == "":
                        # An unlabelled fence opening on a brace is an
                        # action a model did not label -- parse_actions
                        # reads those, so the display must hide them too.
                        self._state = _SUPPRESS
                        self.suppressed_a_block = True
                    else:
                        self._state = _CODE
                        out.append("```" + self._held)
                    self._held = ""
                continue

            if self._state == _SUPPRESS:
                self._held += char
                if self._held.endswith("```"):
                    self._state = _TEXT
                    self._held = ""
                elif len(self._held) > 4:
                    # Only the tail can close the fence; the rest is
                    # payload and is never shown.
                    self._held = self._held[-3:]
                continue

            # _CODE: a real code block, shown as written.
            out.append(char)
            self._held = (self._held + char)[-3:]
            if self._held == "```":
                self._state = _TEXT
                self._held = ""

        return "".join(out)

    def finish(self) -> str:
        """Whatever is still held and is safe to show.

        A stream that ends mid-fence has nothing publishable left: an
        unterminated ```json is a truncated action, and showing half of
        one helps nobody.
        """
        if self._state in (_SUPPRESS, _TAG):
            self._held = ""
            return ""

        tail, self._held = self._held, ""
        return tail
