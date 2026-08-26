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

import difflib
import re

__all__ = [
    "AnswerStream",
    "SCAFFOLD_PREFIXES",
    "TERMINATORS",
    "SENTENCE_SIMILARITY_THRESHOLD",
    "detect_repetition_loop",
    "sentence_similarity",
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

# The same scaffolding, but embedded in a sentence rather than opening a
# line. Verification caught phi-3 writing
#
#     Microsoft is trading at $412.30 (Tool Result: web_search (step1)).
#
# which the line-anchored pattern above cannot see. Applied in order:
# the parenthesised whole first, then the bare label, then a naked
# "tool (stepN)", then any empty parentheses left behind.
#
# "(step" followed by digits is the discriminating part. It is not
# something that occurs in ordinary prose, which is what makes stripping
# it safe without a whitelist of tool names.
_INLINE_TOOL_ECHOES = (
    re.compile(r"\s*\(\s*tool\s+results?\s*:[^()]*(?:\([^()]*\))?[^()]*\)", re.IGNORECASE),
    re.compile(r"\btool\s+results?\s*:\s*", re.IGNORECASE),
    re.compile(r"\b[a-z][a-z0-9_]*\s*\(step\d+\)\s*:?", re.IGNORECASE),
    re.compile(r"\s*\(\s*\)"),
)


def _strip_inline_echoes(text: str, repair: bool = False) -> tuple[str, int]:
    """Remove embedded tool scaffolding, keeping the sentence around it.

    Returns (cleaned, removals). Only ever shortens, so it is safe to run
    repeatedly over a growing buffer.

    repair=True also tidies the whitespace a removal leaves behind. The
    caller passes it once a removal has happened anywhere in this line,
    not just in this call -- the doubled space usually arrives a token
    after the echo did, and a call that removed nothing would otherwise
    skip the repair and let it through.
    """
    cleaned = text
    removals = 0
    for pattern in _INLINE_TOOL_ECHOES:
        cleaned, count = pattern.subn("", cleaned)
        removals += count

    if removals or repair:
        # Removing a parenthetical mid-sentence leaves a doubled space or
        # a space before the full stop.
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"[ \t]+([.,;:!?])", r"\1", cleaned)
    return cleaned, removals


# How much of a line to keep back before sending it.
#
# The filter can only remove what it has not sent, and an inline echo is
# recognisable only once the whole of it has arrived. Holding a short tail
# buys exactly that: text streams with a bounded lag rather than a
# line-at-a-time stutter, and an echo that fits inside the tail is gone
# before the client ever sees it. Sized to comfortably contain
# "(Tool Result: web_search (step1))", the longest form observed.
#
# The trade is honest and bounded: an echo split across the emit boundary
# -- one appearing early in a very long single-line answer -- still gets
# through. Every case seen in practice sits at the end of a sentence.
_LAG_CHARS = 64

# Section headers from the synthesis prompt, echoed back. The header is
# removed and whatever follows it on the line is kept.
SCAFFOLD_PREFIXES: tuple[str, ...] = (
    "answer:",
    "final answer:",
    "response:",
    "- response:",
    # Markdown-heading forms of the same label. phi-3 emitted "### Response"
    # verbatim; the plain "response:" entry above does not match it because
    # the hashes come first.
    "### response",
    "## response",
    "# response",
    "### answer",
    "## answer",
    # Seen leading an echoed restatement. Harmless to strip: the rule keeps
    # everything after the label.
    "support:",
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


# --- Near-duplicate sentences -------------------------------------------
#
# The periodicity guard above needs the repetition to be character-exact.
# A small model often restates instead, which reads the same to a person
# and not at all the same to a substring comparison:
#
#     Microsoft (MSFT) is currently trading at $412.30, which is an
#     increase of 1.2% (source: example.com).
#     Based on a recent web search, Microsoft (MSFT) is currently trading
#     at $412.30, experiencing a 1.2% increase (source: example.com).
#
# This compares each completed sentence against the last few, on
# normalised text, with difflib. No embeddings: an embedding call per
# sentence inside the streaming path would cost more than the tokens it
# saves, and the model that would serve it is the one already busy
# generating.
#
# WHAT THE THRESHOLD CAN AND CANNOT DO. Measured on this repo, with
# difflib.SequenceMatcher over normalised sentences:
#
#     real paraphrase loops        0.742   0.822   1.000
#     legitimately distinct pairs  0.351   0.367   0.643   0.881
#
# Those ranges overlap. "This function returns the active model id" and
# "...the fallback model id" score 0.881 while saying opposite things, and
# a real loop scored 0.742. So no single lexical threshold separates them,
# and the choice is which error to make.
#
# The default is deliberately conservative. At 0.92 this catches only
# near-verbatim restatement -- little that the periodicity guard misses --
# and that is the intended trade: truncating a legitimate answer is a far
# worse failure than letting a paraphrase through, and every other rule in
# this module is built so it cannot delete an answer. Lower it below about
# 0.89 and it starts cutting correct text.
SENTENCE_SIMILARITY_THRESHOLD = 0.92
_SENTENCE_WINDOW = 3       # how many recent sentences to compare against
_SENTENCE_MIN_CHARS = 20   # shorter than this is too small to judge

_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def _normalize_sentence(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def sentence_similarity(a: str, b: str) -> float:
    """How alike two sentences are, on normalised text. 0.0 to 1.0."""
    return difflib.SequenceMatcher(None, _normalize_sentence(a), _normalize_sentence(b)).ratio()


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

    def __init__(
        self,
        reveal_reasoning: bool = False,
        similarity_threshold: float | None = None,
    ) -> None:
        self.reveal_reasoning = reveal_reasoning
        # Configurable per caller; see SENTENCE_SIMILARITY_THRESHOLD for
        # what the default does and why it is set where it is.
        self.similarity_threshold = (
            SENTENCE_SIMILARITY_THRESHOLD if similarity_threshold is None
            else similarity_threshold
        )
        self._pending = ""          # current line, not yet released
        self._released = False      # current line decided; stream it straight through
        self._stopped = False       # hit a terminator; nothing more is published
        self._last_line = None      # for the repetition guard
        self._emitted_any = False
        self._blank_run = False
        self._line_out = ""       # the current line so far, for loop detection
        self._lag = ""            # released but deliberately not yet sent
        self._line_dirty = False  # an echo was removed from this line
        self._prose = ""          # released text awaiting sentence boundaries
        self._recent: list[str] = []
        self.stripped_prefixes = 0
        self.stripped_inline = 0
        self.dropped_repeats = 0
        self.terminated_at: str | None = None
        self.stopped_looping = False
        self.stopped_repeating_sentence = False

    # ------------------------------------------------------------------
    @property
    def stats(self) -> dict:
        """What the filter did, for the log line the transport writes."""
        return {
            "stripped_prefixes": self.stripped_prefixes,
            "stripped_inline": self.stripped_inline,
            "dropped_repeats": self.dropped_repeats,
            "terminated_at": self.terminated_at,
            "stopped_looping": self.stopped_looping,
            "stopped_repeating_sentence": self.stopped_repeating_sentence,
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

        # Once per token, not once per character: the check is cheap but
        # not free, and a loop is not going to be missed by noticing it a
        # few characters late. _line_out is maintained as text is
        # released, not as it is sent, so the lag buffer does not delay
        # detection.
        if not self._stopped and detect_repetition_loop(self._line_out):
            self._stopped = True
            self.stopped_looping = True

        if not self._stopped:
            self._check_repeated_sentence()

        return emitted

    def _check_repeated_sentence(self) -> None:
        """Stop if a completed sentence restates a recent one.

        Runs on sentence boundaries rather than tokens, so the cost is a
        handful of difflib comparisons per sentence.
        """
        while True:
            match = _SENTENCE_END.search(self._prose)
            if not match:
                return

            sentence, self._prose = self._prose[:match.end()], self._prose[match.end():]
            normalized = _normalize_sentence(sentence)

            # Too short to judge: "Not found." and "Yes." are ordinary
            # things to say twice.
            if len(normalized) < _SENTENCE_MIN_CHARS:
                continue

            for previous in self._recent:
                ratio = difflib.SequenceMatcher(None, previous, normalized).ratio()
                if ratio >= self.similarity_threshold:
                    self._stopped = True
                    self.stopped_looping = True
                    self.stopped_repeating_sentence = True
                    return

            self._recent.append(normalized)
            if len(self._recent) > _SENTENCE_WINDOW:
                self._recent.pop(0)

    def finish(self) -> str:
        """Flush the final line, which may have arrived without a newline."""
        if self.reveal_reasoning:
            return ""
        if self._stopped:
            # Nothing more may be published, and anything still lagging
            # belongs to the part being cut.
            self._lag = ""
            return ""
        return self._end_line(final=True)

    # ------------------------------------------------------------------
    def _lagged(self, text: str) -> str:
        """Queue released text, returning only what is now safe to send.

        Inline cleanup runs over the whole retained tail on every call. It
        only ever shortens, so repeating it as the buffer grows is both
        cheap and idempotent.
        """
        if not text:
            return ""

        self._lag += text
        self._lag, removed = _strip_inline_echoes(self._lag, repair=self._line_dirty)
        self.stripped_inline += removed
        self._line_dirty = self._line_dirty or bool(removed)

        if len(self._lag) <= _LAG_CHARS:
            return ""
        out, self._lag = self._lag[:-_LAG_CHARS], self._lag[-_LAG_CHARS:]
        return out

    def _flush(self) -> str:
        """Send whatever the lag buffer is still holding."""
        out, removed = _strip_inline_echoes(self._lag, repair=self._line_dirty)
        self.stripped_inline += removed
        self._lag = ""
        return out

    def _feed(self, char: str) -> str:
        if self._released:
            self._line_out += char
            self._prose += char
            return self._lagged(char)

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
        self._line_out += line
        self._prose += line
        return self._lagged(line)

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
            # Already streaming this line; the lag buffer still holds its
            # tail, and this is the last chance to clean it. A
            # partly-streamed line cannot be compared for repetition,
            # because half of it has already gone out.
            tail = self._flush()
            self._line_dirty = False
            self._released = False
            self._last_line = None
            self._blank_run = False
            self._line_out = ""
            return tail if final else tail + "\n"

        line = self._strip_markers(self._pending)
        self._pending = ""
        if self._stopped:
            return ""

        self._line_out = ""
        line, removed = _strip_inline_echoes(line, repair=self._line_dirty)
        self.stripped_inline += removed
        self._line_dirty = False
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

        self._prose += line + " "
        self._last_line = stripped
        self._emitted_any = True
        self._blank_run = False
        return line if final else line + "\n"
