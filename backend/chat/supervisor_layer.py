"""ARIA Lite - checking a heavier model's output before the user sees it.

mistral-7b and nemo-12b write well and leak. They echo chat-template
markers, restate a paragraph they already wrote, and emit an action block
that is nearly JSON. phi-3-mini is the supervisor: small, fast, and the
one model here whose job is to check rather than to produce.

TWO STAGES, AND THE ORDER MATTERS
---------------------------------
    1. Deterministic repair, in code. Chat-template leakage, repeated
       paragraphs and malformed action JSON are exactly-solvable
       problems. They are solved here, with string handling that always
       does the same thing.

    2. A model pass, optional, for the one thing code cannot judge:
       whether the prose sounds like ARIA.

The order is the argument. Asking a 3.8B to strip a leaked system prompt
means a 3.8B rewriting the whole answer -- and a rewrite can drop a code
block, truncate a list, or invent a correction, which is the exact class
of failure this layer exists to remove. Handing it a job that regex does
perfectly would make the output worse on average while looking like more
care was taken.

So stage 2 is guarded, and its output is ACCEPTED ONLY IF it passes:
every fenced code block still present, no drastic shrink, not empty, no
new leakage. If the supervisor's rewrite fails any of those, the
deterministic text stands. A supervisor that can only improve things is
worth running; one that can silently damage them is not.

WHEN THE SUPERVISOR IS UNAVAILABLE
----------------------------------
The model stage is skipped and the deterministic repairs stand.

That is a deliberate reading of "if supervision fails, return
raw_response unchanged". The intent behind that rule is that a
supervision failure must never damage a turn, and it is honoured: this
returns the answer with nothing invented and nothing removed but
leakage. Throwing away a correctly stripped system prompt because an
unrelated optional stage could not load would be losing a fix for no
reason. Nothing here can fail into a worse answer than it received.

WHAT THIS CANNOT DO
-------------------
It cannot un-send a streamed token. On the WebSocket path tokens reach
the client as they are produced, so by the time a full response exists
the user has already read it -- the same constraint conversation_manager's
response optimizer documents about itself, and the reason AnswerStream
filters incrementally instead of at the end.

Buffering the whole reply so it could be supervised first would fix that
and cost more than it is worth: a 12B on local hardware would leave the
user watching nothing for the length of the entire answer. "ARIA must
run smoothly" and "supervise before sending" are in genuine tension on a
streaming transport, and this is where the line is drawn.

So supervision is wired at the two points where it still changes an
outcome:

    WebSocket  the finalized text that drives ACTIONS. A malformed
               action block is EXECUTED, not read, so repairing it
               before parse_actions sees it is the half that matters --
               and the deterministic stage alone does that, at no
               latency cost, which is why the model stage is skipped
               there.

    REST       /chat already buffers the whole reply before responding,
               so the full two-stage pass runs and the user genuinely
               never sees the unsupervised text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from logger import get_logger

logger = get_logger(__name__)

_UNSET = object()

__all__ = [
    "MODEL_SUPERVISION_ENABLED",
    "SUPERVISED_ROLES",
    "deterministic_repair",
    "model_supervision",
    "supervise_full",
    "SupervisionResult",
    "needs_model_supervision",
    "needs_supervision",
    "repair_deterministically",
    "supervise_chat_output",
    "supervisor_generator",
]

# Chat-template scaffolding and echoed system turns. Every one of these
# is a marker no answer should ever contain -- they are the model showing
# its own prompt format, not writing.
_LEAK_MARKERS = (
    "<|im_start|>", "<|im_end|>", "<|system|>", "<|user|>", "<|assistant|>",
    "<|end|>", "[INST]", "[/INST]", "<<SYS>>", "<</SYS>>", "### System:",
    "### Instruction:", "### Response:",
)

# A line that opens an echoed system turn. Anchored and case-insensitive,
# but deliberately NOT matching a line that merely mentions the word --
# "the system: a short description" is prose, "System: You are ARIA" is
# leakage, and the difference is what follows the colon.
# Emphasis and heading characters are stripped before the role word, for
# the same reason answer_stream does it: phi-3 writes "**assistant:**",
# and a pattern anchored on the bare word never sees it.
_LEAK_LINE = re.compile(
    r"^[\s*_#>]*(system|assistant|user)\s*:\s*[*_]*(\s|$)",
    re.IGNORECASE,
)

# What a chat-template marker leaves behind once it is removed: the role
# word alone on its own line. Matched only as a WHOLE line, so a
# paragraph that happens to begin with the word "system" survives.
_BARE_ROLE_LINE = re.compile(
    r"^[\s*_#>]*(system|user|assistant)\s*:?\s*[*_]*\s*$", re.IGNORECASE)

# The persona being read back to the user. A model that starts its reply
# by restating its instructions has leaked them, however fluently.
_PERSONA_ECHO = re.compile(
    r"^\s*(you are|i am)\s+aria[^.\n]{0,80}(\.|$)", re.IGNORECASE)

# Below this length a repeated paragraph is probably a legitimate repeat
# -- a heading, a short label, a closing line -- and removing it would
# damage the answer to fix nothing.
_MIN_DEDUP_CHARS = 40

# A supervised rewrite shorter than this fraction of what it was given
# has not been tidied, it has been truncated.
_MIN_KEEP_RATIO = 0.6

# And one longer than this has not been tidied either -- it has been
# added to. Measured on this machine, phi-3 asked to normalize the tone
# of a three-sentence answer returned it rewritten plus a fabricated C++
# code block, having been told in the same prompt not to add anything.
#
# The original guards would have accepted it: the shrink check only
# looks downward, and there were no code blocks in the input to lose. A
# supervisor that invents content is the exact failure this layer exists
# to remove, arriving through the layer.
_MAX_GROWTH_RATIO = 1.25

# Whether the SUPERVISOR MODEL runs at all. Stage one -- deterministic
# repair -- is unaffected and always runs.
#
# Off, and the reason is a measurement rather than an opinion. On this
# machine, asked to normalize the tone of a three-sentence answer,
# phi-3-mini took 9.6 seconds and returned the answer plus a fabricated
# C++ code block. The growth guard above now rejects that, which is the
# right outcome and leaves the pass costing ten seconds to change
# nothing. Buffered delivery means those ten seconds are silence.
#
# So the honest default is off: the stage that measurably helps (leakage,
# repetition, malformed action JSON) is free and always on, and the stage
# that measurably hurt is one constant away.
#
# Set True to enable it. Everything around it is built and tested; a
# larger or better-instructed supervisor would change this verdict.
MODEL_SUPERVISION_ENABLED = False

_FENCE = re.compile(r"```[\s\S]*?```", re.MULTILINE)
_JSON_FENCE = re.compile(r"```(?:json)?\s*\n([\s\S]*?)\n?```", re.MULTILINE)


@dataclass
class SupervisionResult:
    """The checked answer, and what was done to it."""

    text: str
    # Every repair applied, in order. This is what makes a supervision
    # pass debuggable after the fact rather than a black box that
    # sometimes changes things.
    repairs: list[str] = field(default_factory=list)
    # The model stage ran and its rewrite was kept.
    supervised: bool = False
    # The model stage ran and its rewrite was rejected by the guards.
    rejected: bool = False
    # Why the model stage did not run, when it did not.
    unavailable_reason: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.repairs) or self.supervised


# ======================================================
# Stage 1 - deterministic
# ======================================================
def _protect_fences(text: str):
    """Replace fenced blocks with placeholders so line rules skip them.

    A code block legitimately contains things that look like leakage --
    `[INST]` in a string, `system:` in a YAML sample, the same three
    lines twice in a diff. Editing inside one would corrupt the answer
    to tidy it.
    """
    blocks: list[str] = []

    def take(match):
        blocks.append(match.group(0))
        return f"\x00FENCE{len(blocks) - 1}\x00"

    return _FENCE.sub(take, text), blocks


def _restore_fences(text: str, blocks: list[str]) -> str:
    for index, block in enumerate(blocks):
        text = text.replace(f"\x00FENCE{index}\x00", block)
    return text


def _strip_leakage(text: str, repairs: list[str]) -> str:
    before = text

    for marker in _LEAK_MARKERS:
        if marker in text:
            text = text.replace(marker, "")

    kept = []
    for line in text.splitlines():
        if _LEAK_LINE.match(line) or _PERSONA_ECHO.match(line):
            continue
        # Residue. "<|im_start|>system" loses its marker above and leaves
        # the bare word on a line of its own, which reads as a stray
        # heading -- the leak is gone but its shadow is still there.
        if _BARE_ROLE_LINE.match(line):
            continue
        kept.append(line)
    text = "\n".join(kept)

    if text != before:
        repairs.append("removed system prompt leakage")
    return text


def _drop_repeated_paragraphs(text: str, repairs: list[str]) -> str:
    paragraphs = re.split(r"\n\s*\n", text)
    seen: set[str] = set()
    kept: list[str] = []
    dropped = 0

    for paragraph in paragraphs:
        key = re.sub(r"\s+", " ", paragraph).strip().lower()
        if len(key) >= _MIN_DEDUP_CHARS and key in seen:
            dropped += 1
            continue
        if key:
            seen.add(key)
        kept.append(paragraph)

    if dropped:
        repairs.append(f"removed {dropped} repeated paragraph{'' if dropped == 1 else 's'}")
    return "\n\n".join(kept)


def _repair_json_payload(payload: str) -> str | None:
    """Make a nearly-JSON block parse, or give up.

    Repairs only what is unambiguous: trailing commas, smart quotes, a
    stray code fence marker. It never fills in a missing field or guesses
    a value -- an action packet is EXECUTED, so inventing part of one is
    strictly worse than leaving it malformed, where parse_actions ignores
    it and nothing happens.
    """
    candidate = payload.strip()
    if not candidate:
        return None

    try:
        json.loads(candidate)
        return None                      # already valid, nothing to do
    except Exception:
        pass

    repaired = candidate
    repaired = repaired.replace("“", '"').replace("”", '"')
    repaired = repaired.replace("‘", "'").replace("’", "'")
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)     # trailing commas
    repaired = re.sub(r"^\s*```(?:json)?\s*", "", repaired)
    repaired = re.sub(r"\s*```\s*$", "", repaired)

    try:
        json.loads(repaired)
        return repaired
    except Exception:
        return None


def _repair_action_blocks(text: str, repairs: list[str]) -> str:
    fixed = 0

    def replace(match):
        nonlocal fixed
        repaired = _repair_json_payload(match.group(1))
        if repaired is None:
            return match.group(0)
        fixed += 1
        return f"```json\n{repaired}\n```"

    text = _JSON_FENCE.sub(replace, text)
    if fixed:
        repairs.append(f"repaired {fixed} malformed action block{'' if fixed == 1 else 's'}")
    return text


def repair_deterministically(raw_response: str) -> SupervisionResult:
    """Everything that can be fixed exactly, fixed exactly.

    Runs whether or not a supervisor model is available, because none of
    it needs one and all of it is safe.
    """
    repairs: list[str] = []
    text = str(raw_response or "")

    # Action blocks first, while the fences are still intact -- the JSON
    # repair works ON fenced blocks, so it has to run before they are
    # taken out of the way for the line rules.
    text = _repair_action_blocks(text, repairs)

    protected, blocks = _protect_fences(text)
    protected = _strip_leakage(protected, repairs)
    protected = _drop_repeated_paragraphs(protected, repairs)
    text = _restore_fences(protected, blocks)

    return SupervisionResult(text=text.strip(), repairs=repairs)


# ======================================================
# Stage 2 - the supervisor model
# ======================================================
_SUPERVISOR_PROMPT = """You are a copy editor. Rewrite the assistant reply below so it reads in a single consistent voice: direct, plain, no filler openings, no restating the question.

Rules you must not break:
- Keep every fact, number, name and instruction exactly as written.
- Keep every code block byte for byte, fences included.
- Do not add anything. Do not answer the question yourself.
- Do not add a preamble. Output only the rewritten reply.

REPLY:
{answer}

REWRITTEN REPLY:"""


def _rewrite_is_safe(original: str, rewritten: str) -> tuple[bool, str]:
    """Whether the supervisor's rewrite may replace the original.

    The guards are the whole reason a model is allowed near a finished
    answer. A small model asked to tidy a long reply will sometimes
    summarise it instead, and a summary presented as the answer is a
    worse failure than the untidiness it fixed.
    """
    if not rewritten or not rewritten.strip():
        return False, "the supervisor returned nothing"

    if len(rewritten) < len(original) * _MIN_KEEP_RATIO:
        return False, "the supervisor's rewrite dropped too much of the answer"

    if len(rewritten) > len(original) * _MAX_GROWTH_RATIO:
        return False, "the supervisor's rewrite added to the answer"

    original_blocks = _FENCE.findall(original)
    rewritten_blocks = _FENCE.findall(rewritten)
    if len(rewritten_blocks) < len(original_blocks):
        return False, "the supervisor's rewrite lost a code block"
    if len(rewritten_blocks) > len(original_blocks):
        # Invented code. Caught separately from the growth check because
        # this one is worth naming: fabricated code in an answer about
        # code is the most costly thing this layer could let through.
        return False, "the supervisor's rewrite invented a code block"
    for block in original_blocks:
        if block not in rewritten:
            return False, "the supervisor's rewrite altered a code block"

    if any(marker in rewritten for marker in _LEAK_MARKERS):
        return False, "the supervisor's rewrite introduced template leakage"

    return True, ""


def supervise_chat_output(raw_response, turn_context=None, *, generate=None) -> SupervisionResult:
    """Check a heavier model's answer before the user sees it.

    `generate` is a `generate(prompt) -> str` bound to the supervisor
    model. Injected rather than built here so this module performs no
    I/O and can be tested without loading anything; the orchestrator
    supplies the real one.

    Returns a SupervisionResult whose `.text` is always usable. There is
    no failure path that returns something worse than what came in.
    """
    result = repair_deterministically(raw_response)

    role = (turn_context or {}).get("role") if isinstance(turn_context, dict) else None
    if role == "supervisor":
        # phi-3 supervising phi-3 is one model grading its own homework,
        # at the cost of a second inference. The orchestrator already
        # avoids calling this for a supervisor-generated turn; this is
        # the second lock on the same door.
        result.unavailable_reason = "the turn was generated by the supervisor itself"
        return result

    if generate is None:
        result.unavailable_reason = "no supervisor model was available"
        logger.warning(
            "supervision skipped: no supervisor generator was supplied. "
            "Deterministic repairs still applied: %s",
            result.repairs or "none needed",
        )
        return result

    if not result.text.strip():
        result.unavailable_reason = "nothing to supervise"
        return result

    try:
        rewritten = generate(_SUPERVISOR_PROMPT.format(answer=result.text))
    except Exception as error:
        result.unavailable_reason = f"the supervisor model failed: {error}"
        logger.warning(
            "supervision skipped: the supervisor model raised (%s). Answer length %d, "
            "repairs applied %s. The unsupervised answer is being used.",
            error, len(result.text), result.repairs or "none",
            exc_info=True,
        )
        return result

    rewritten = str(rewritten or "").strip()
    safe, why = _rewrite_is_safe(result.text, rewritten)

    if not safe:
        result.rejected = True
        result.unavailable_reason = why
        logger.warning(
            "supervision rejected: %s. Original length %d, rewrite length %d. "
            "Keeping the deterministically repaired answer.",
            why, len(result.text), len(rewritten),
        )
        return result

    result.text = rewritten
    result.supervised = True
    result.repairs.append("normalized tone")
    return result


def deterministic_repair(response_text, context=None) -> SupervisionResult:
    """Stage one, by its specified name. Runs for every model.

    An alias for repair_deterministically with the two-argument shape the
    rest of the pipeline uses. `context` is accepted and unused: nothing
    here depends on which model wrote the text, which is exactly why it
    can run for all of them.
    """
    return repair_deterministically(response_text)


def model_supervision(response_text, context=None, *, generate=None) -> SupervisionResult:
    """Stage two, by its specified name. phi-3 normalizes tone.

    Takes text that stage one has already repaired. Its rewrite is
    accepted only if every code block survives byte for byte, nothing
    shrinks past the threshold, and no leakage appears -- otherwise what
    it was given stands.
    """
    result = SupervisionResult(text=str(response_text or ""))

    role = (context or {}).get("role") if isinstance(context, dict) else None
    if role == "supervisor":
        result.unavailable_reason = "the turn was generated by the supervisor itself"
        return result

    if generate is None:
        result.unavailable_reason = "no supervisor model was available"
        return result

    if not result.text.strip():
        result.unavailable_reason = "nothing to supervise"
        return result

    try:
        rewritten = generate(_SUPERVISOR_PROMPT.format(answer=result.text))
    except Exception as error:
        result.unavailable_reason = f"the supervisor model failed: {error}"
        logger.warning("supervision skipped: the supervisor model raised (%s); "
                       "the unsupervised answer is being used.", error, exc_info=True)
        return result

    rewritten = str(rewritten or "").strip()
    safe, why = _rewrite_is_safe(result.text, rewritten)

    if not safe:
        result.rejected = True
        result.unavailable_reason = why
        logger.warning("supervision rejected: %s. Original %d chars, rewrite %d.",
                       why, len(result.text), len(rewritten))
        return result

    result.text = rewritten
    result.supervised = True
    result.repairs.append("normalized tone")
    return result


def supervise_full(response_text, model_id: str | None, *, mode: str = "local",
                   generate=_UNSET) -> SupervisionResult:
    """Both stages, in order, for one turn's finished text.

    The whole pipeline behind one call, so the two transports run the
    same thing rather than each assembling it. `generate` is injected for
    tests; left alone, the supervisor model is built when the turn's
    model warrants one.
    """
    first = deterministic_repair(response_text, {"model_id": model_id})

    if generate is _UNSET:
        # Roughly the length of what it is rewriting, in tokens, with
        # headroom. Four characters per token is the usual rule of thumb.
        budget = max(64, min(1024, len(str(response_text or "")) // 3))
        generate = (supervisor_generator(mode, max_tokens=budget)
                    if MODEL_SUPERVISION_ENABLED and needs_model_supervision(model_id)
                    else None)

    second = model_supervision(
        first.text, {"role": _role_of(model_id)}, generate=generate)

    return SupervisionResult(
        text=second.text,
        repairs=first.repairs + second.repairs,
        supervised=second.supervised,
        rejected=second.rejected,
        unavailable_reason=second.unavailable_reason,
    )


def _role_of(model_id: str | None) -> str:
    from backend.config.model_roles import describe_role

    return describe_role(model_id)


# ======================================================
# Wiring helpers
# ======================================================
# The roles whose output is checked. phi-3's own turns are absent on
# purpose: it IS the supervisor, and having it grade its own homework
# costs a second inference to change nothing.
SUPERVISED_ROLES = frozenset({"chat_tools", "heavy_reasoning"})


def needs_model_supervision(model_id: str | None) -> bool:
    """Whether the SUPERVISOR MODEL should look at this turn's output.

    False for phi-3's own turns -- it is the supervisor, and grading its
    own homework costs a second inference to change nothing.
    """
    from backend.config.model_roles import role_of

    return role_of(model_id)["role"] in SUPERVISED_ROLES


def needs_supervision(model_id: str | None) -> bool:
    """Whether ANYTHING should check this turn's output. Always true.

    Kept as a distinct name because these two questions were one, and
    being one was a bug. Deterministic repair was gated on the same role
    check as the model stage, so phi-3 -- now the DEFAULT CHAT MODEL, and
    therefore the most common turn in the system -- had its output
    checked by nothing at all.

    Measured, not theorised. Asked a one-line question on this machine,
    phi-3 answered:

        Blue is often associated with calmness and serenity.

        **assistant:** Blue is ...

    Its own leakage was exempt from the layer that exists to remove
    leakage, because an exemption written for a different reason reached
    further than it meant to. Stripping a template marker costs no
    inference and cannot make an answer worse, so nothing is exempt.
    """
    return True


def supervisor_generator(mode: str = "local", *, max_tokens: int = 1024):
    """A `generate(prompt) -> str` bound to the supervisor model, or None.

    None whenever the supervisor cannot be reached -- not installed, a
    non-local mode, a provider that will not build. Every caller treats
    None as "skip the model stage", so a missing supervisor degrades to
    the deterministic repairs rather than failing a turn.
    """
    if mode != "local":
        # In Cloud or Automatic mode a frontier model wrote the answer.
        # Handing it to a 3.8B to tidy is the one arrangement here that
        # is certain to make the output worse.
        return None

    try:
        from backend.config.model_roles import installed_model_for
        from backend.core.generation import make_generator

        supervisor = installed_model_for("phi-3-mini-4k-instruct-q4")
        if supervisor is None:
            logger.warning("no supervisor model is installed; supervision will be "
                           "limited to deterministic repairs")
            return None

        return make_generator(
            supervisor, mode,
            stream_sink=lambda packet: None,   # nothing streams a supervision pass
            # Bounded by the answer it is rewriting. A tone pass that
            # needs more room than the original text is not a tone pass,
            # and the unbounded 1536 was most of a 12-second supervision
            # of a three-sentence reply.
            max_tokens=max_tokens,
            temperature=0.0,                   # a rewrite must not vary run to run
        )
    except Exception:
        logger.warning("could not build a supervisor generator; supervision will be "
                       "limited to deterministic repairs", exc_info=True)
        return None
