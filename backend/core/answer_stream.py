"""ARIA Lite - the answer, separated from the scaffolding, as it streams.

conversation_manager's RESPONSE OPTIMIZER says of the WebSocket path:

    by the time the full text is available to run this against, those
    tokens are already sent and rendered. It's applied there too, but only
    to log what the optimizer found -- the honest fix for the streaming
    path is the stop-sequence prevention above, not a post-hoc filter that
    can't un-send anything.

That is exactly right, and it is why this module exists: it applies the
same rules *before* a token is sent, so it can un-send by never sending.
Nothing here is a new policy. The rules are optimize_response's -- stop at
a fabricated turn boundary, collapse a repetition loop -- plus one the
streaming path needs and the buffered path never did: strip the prompt's
own section headers when a model echoes them back.

Why a model echoes them at all: the synthesis prompt is a structured
document (User Query / Evidence Summary / Plan / Tool Results / Synthesis
Instructions), and a small local model completing that document tends to
continue the pattern rather than break out of it. Observed on this repo:

    phi-3-mini  ->  "MSFT is ... $412.30"
                    (Note 12)
                    - Response: MSFT is ... $412.30
                    Solution:
                    The Microsoft (MSFT) stock is ...
                    Instruction:            <- starts a new fabricated turn

    gpt-4       ->  "Microsoft (MSFT) is currently trading at $412.30 ..."

The answer is in there both times. What differs is how much scaffolding
comes with it, and whether generation runs on past the end.

Three rules, and the difference between them matters:

  * a scaffolding *prefix* is stripped, never the text after it. "-
    Response: MSFT is $412.30" loses six characters, not the price, and
    "web_search (step1): ..." loses the step id, not the finding. No
    prefix rule in here can delete an answer.
  * a *terminator* truncates, because everything after it is a turn the
    model invented. That is optimize_response's existing behaviour and
    one of the two places content is deliberately dropped.
  * a *loop* truncates too, for the same reason: a segment repeated back
    to back carries nothing the copy before it did not.

Latency: a line is held only until it is long enough that no marker could
still match it (or until it ends). Prose crosses that in a few tokens, so
tokens stream essentially as they arrive; short lines -- which is what a
repetition loop produces -- are held long enough to be recognised.

What this does and does not fix. The prompt used to tell a turn with tool
evidence that it had none, and small models obeyed: qwen2.5-0.5b answered
"not found" until it ran out of tokens. That is fixed at the source now,
in synthesis_prompt's evidence summary, which is where it belonged -- no
filter can turn a refusal into an answer.

What is left for this module is degeneration *after* a correct answer:
scaffolding echoed back, a conversation invented in the user's voice, a
phrase repeated to fill the context. Those are artifacts of how the prompt
is shaped and how small the model is, they arrive after the answer the
user wanted, and cutting them is safe in a way that inventing content
never is.
"""

from __future__ import annotations

import re

__all__ = [
    "AnswerStream",
    "SCAFFOLD_PREFIXES",
    "TERMINATORS",
    "detect_repetition_loop",
]


# A line starting with one of these is a turn the model invented, or the
# prompt's instruction block restarting. Everything from here on is
# dropped -- the same truncation optimize_response performs on
# _TURN_BOUNDARY_MARKERS, applied a few hundred milliseconds earlier.
TERMINATORS: tuple[str, ...] = (
    "user:",
    "system:",
    "assistant:",
    "user query:",
    "instruction:",
    "instructions:",
    "synthesis instructions:",
    "### instruction",
    "<|user|>",
    "<|system|>",
    # The prompt's own Conversation Context format. It renders past turns
    # as "- [user] ..." / "- [assistant] ...", and a model completing that
    # document carries on writing the conversation rather than answering
    # it. Observed on phi-3-mini: a correct answer, then
    #
    #     - [user] how much is MSFT trading at
    #     - [assistant] Currently, Microsoft (MSFT) is trading at $412.30
    #
    # -- a transcript of a exchange that never happened, in the user's
    # voice. Worse than noise: it puts words in their mouth.
    "- [user]",
    "- [assistant]",
    "- [system]",
    "[user]",
    "[assistant]",
    "[system]",
)

# A Tool Results line, echoed back verbatim: "web_search (step1): ...".
# The tool name and step id are how the prompt was assembled; the text
# after the colon is the finding, and only that is kept.
_TOOL_RESULT_ECHO = re.compile(r"^[a-z][a-z0-9_]* \(step\d+\):\s*", re.IGNORECASE)

# Section headers from the synthesis prompt, echoed back. The header is
# removed and whatever follows it on the line is kept.
SCAFFOLD_PREFIXES: tuple[str, ...] = (
    "answer:",
    "final answer:",
    "response:",
    "- response:",
    "solution:",
    "- solution:",
    "output:",
    "plan:",
    "tool results:",
    "evidence summary:",
    "search topic:",
)

_MAX_MARKER = max(len(m) for m in TERMINATORS + SCAFFOLD_PREFIXES)

# How much of a line to hold before deciding it is ordinary text. Long
# enough for the longest marker, and for a tool-result echo like
# "web_search (step1): " which is matched by pattern rather than by
# literal and so has no length in the table above.
_HOLD_CHARS = max(_MAX_MARKER, 32)

# --- Repetition inside a single line -------------------------------------
#
# The line-level guard below collapses a model that repeats whole lines.
# It cannot see a model that repeats within one, which is what a small
# model does when it runs out of things to say but not out of tokens.
# Observed on qwen2.5-0.5b, all on one line:
#
#     ... $412.30, up 1.2% (source: example.com). (note: no other
#     information is provided) (file:web_search_result.md#search_result)
#     (note: no other information is provided) (file:web_search_result.md
#     #search_result) (file:web_search_result.md#search_result) ...
#
# Detection is periodicity: if the tail of the line is the same segment
# repeated back to back, generation has stopped producing information.
#
# Two thresholds rather than one, because how much repetition is
# suspicious depends on how long the repeated part is. "very very very" is
# a person talking; forty characters repeated verbatim is not.
_LOOP_LONG_SEGMENT = 30     # this long, twice in a row is already a loop
_LOOP_SHORT_SEGMENT = 12    # shorter than that, wait for a third copy
_LOOP_WINDOW = 800          # how far back to look; bounds the cost


def detect_repetition_loop(text: str) -> bool:
    """Whether this text ends in a segment repeated back to back.

    Public because the local provider stops generation on the same
    condition. Two copies of this rule with two sets of thresholds would
    drift, and the provider halting at a different point than the
    transport truncates is precisely the bug that would be invisible --
    the user sees identical output either way, and only the wasted
    seconds differ.

    Checked once per token rather than per character, and against a
    bounded window, so the cost is a few hundred slice comparisons on a
    string no longer than _LOOP_WINDOW -- irrelevant next to generating
    the token that triggered it.
    """
    tail = text[-_LOOP_WINDOW:]
    limit = len(tail) // 2

    for period in range(_LOOP_SHORT_SEGMENT, limit + 1):
        segment = tail[-period:]

        # A horizontal rule ("--------", "=-=-=-=-", a row of dots) is
        # perfectly periodic and perfectly legitimate. Repetition only
        # means a loop when there was something to repeat, so a segment
        # built from one or two distinct characters is not evidence of
        # one.
        if len(set(segment.strip())) <= 2:
            continue

        repeats = 2 if period >= _LOOP_LONG_SEGMENT else 3
        if len(tail) >= period * repeats and tail.endswith(segment * repeats):
            return True
    return False


def _matches(lowered: str, markers: tuple[str, ...]) -> str | None:
    for marker in markers:
        if lowered.startswith(marker):
            return marker
    return None


def _could_still_match(lowered: str) -> bool:
    """Whether this partial line might yet become a marker.

    A line is held only while this is true, so the hold is bounded by the
    longest marker rather than by the length of the answer.
    """
    return any(
        marker.startswith(lowered)
        for marker in TERMINATORS + SCAFFOLD_PREFIXES
    )


class AnswerStream:
    """Turns a raw token stream into the publishable answer.

    push() returns the text that is safe to send now, which is often "".
    finish() returns whatever is left once generation ends. A caller that
    sends everything both of them return has sent the answer and nothing
    else.

    reveal_reasoning=True passes everything through untouched, for a
    caller that has explicitly asked to see the model's raw output. It is
    off by default: the scaffolding is an artifact of how the prompt is
    built, not something a user asked to read.
    """

    def __init__(self, reveal_reasoning: bool = False) -> None:
        self.reveal_reasoning = reveal_reasoning
        self._pending = ""          # current line, not yet released
        self._released = False      # current line decided; stream it straight through
        self._stopped = False       # hit a terminator; nothing more is published
        self._last_line = None      # for the repetition guard
        self._emitted_any = False
        self._blank_run = False
        self._line_out = ""       # what has been emitted for the current line
        self.stripped_prefixes = 0
        self.dropped_repeats = 0
        self.terminated_at: str | None = None
        self.stopped_looping = False

    # ------------------------------------------------------------------
    @property
    def stats(self) -> dict:
        """What the filter did, for the log line the transport writes."""
        return {
            "stripped_prefixes": self.stripped_prefixes,
            "dropped_repeats": self.dropped_repeats,
            "terminated_at": self.terminated_at,
            "stopped_looping": self.stopped_looping,
        }

    # ------------------------------------------------------------------
    def push(self, token: str) -> str:
        """Feed one token; get back the text that may be sent now."""
        if self.reveal_reasoning:
            return token
        if self._stopped or not token:
            return ""

        out = []
        for char in token:
            if char == "\n":
                out.append(self._end_line())
                if self._stopped:
                    break
            else:
                out.append(self._feed(char))

        emitted = "".join(out)
        self._line_out += emitted.rsplit("\n", 1)[-1] if "\n" in emitted else emitted

        # Once per token, not once per character: the check is cheap but
        # not free, and a loop is not going to be missed by noticing it a
        # few characters late.
        if not self._stopped and detect_repetition_loop(self._line_out):
            self._stopped = True
            self.stopped_looping = True

        return emitted

    def finish(self) -> str:
        """Flush the final line, which may have arrived without a newline."""
        if self.reveal_reasoning or self._stopped:
            return ""
        return self._end_line(final=True)

    # ------------------------------------------------------------------
    def _feed(self, char: str) -> str:
        if self._released:
            return char

        self._pending += char
        lowered = self._pending.lstrip().lower()

        # Hold while a marker could still match, and hold anyway until the
        # line is longer than the longest marker. The second half is what
        # makes the repetition guard possible: a loop emits short lines,
        # and a short line is still whole when it ends, so it can be
        # compared against the one before it. Prose passes _MAX_MARKER in
        # a token or two and streams from there.
        if _could_still_match(lowered) or len(lowered) < _HOLD_CHARS:
            return ""

        return self._release()

    def _release(self) -> str:
        """No marker can match this line now -- publish what is held.

        The marker checks run here too, not only at end-of-line: a header
        is recognisable well before the line ends, and releasing the held
        text without checking would send "- Response: " verbatim.
        """
        line = self._pending
        self._pending = ""
        line = self._strip_markers(line)
        if self._stopped:
            return ""

        self._released = True
        if line.strip():
            self._emitted_any = True
            self._blank_run = False
        return line

    def _strip_markers(self, line: str) -> str:
        """Apply the terminator and scaffolding rules to one line."""
        lowered = line.lstrip().lower()

        terminator = _matches(lowered, TERMINATORS)
        if terminator:
            self._stopped = True
            self.terminated_at = terminator
            return ""

        prefix = _matches(lowered, SCAFFOLD_PREFIXES)
        if prefix:
            self.stripped_prefixes += 1
            # Keep the content, drop only the label.
            return line.lstrip()[len(prefix):].lstrip()

        echo = _TOOL_RESULT_ECHO.match(line.lstrip())
        if echo:
            self.stripped_prefixes += 1
            return line.lstrip()[echo.end():].lstrip()
        return line

    def _end_line(self, final: bool = False) -> str:
        """Finish the current line and decide what of it is publishable."""
        if self._released:
            # Already streaming this line; only the newline is left. A
            # partly-streamed line cannot be compared for repetition,
            # because half of it has already gone out.
            self._released = False
            self._last_line = None
            self._blank_run = False
            self._line_out = ""
            return "" if final else "\n"

        line = self._strip_markers(self._pending)
        self._pending = ""
        if self._stopped:
            return ""

        self._line_out = ""
        stripped = line.strip()

        if not stripped:
            # One blank line survives a run of them, and none at all
            # before the answer has started.
            if not self._emitted_any or self._blank_run or final:
                return ""
            self._blank_run = True
            return "\n"

        if stripped == self._last_line:
            self.dropped_repeats += 1
            return ""

        self._last_line = stripped
        self._emitted_any = True
        self._blank_run = False
        return line if final else line + "\n"
