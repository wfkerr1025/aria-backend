"""ARIA Lite Phase 6 - the synthesis entry point.

The only part of Phase 6 that calls a model. Everything above it -- the
bundle, the prompt -- is pure, which is what makes this module small: it
builds, it calls, it returns.

Two paths never reach the model at all:

    Nothing was retrieved. The honest answer is "not found", and it is
    already known before any tokens are spent. Asking a model to say it
    invites the one failure mode Phase 6 exists to prevent, which is a
    fluent answer built from no evidence.

    The model is unavailable. ARIA runs on a local engine that may have no
    model loaded, so this returns a plain listing of what was retrieved
    instead of raising. That answer is visibly not synthesized -- it cites
    every source and merges nothing -- which is the right failure: the user
    still gets the evidence, and nobody mistakes it for a considered reply.

Generation is injected, always. There is no working default: reaching a
model without going through ProviderRouter would skip mode separation and
the user's model selection, so default_generate raises instead of falling
back. Callers build one with backend.core.generation.make_generator, which
resolves the provider and can stream; tests pass their own callable, which
keeps the suite hermetic.
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    from backend.aria_synthesis.bundle_builder import build_evidence_bundle
    from backend.aria_synthesis.conflict_detection import detect_conflicts
    from backend.aria_synthesis.conflict_summary import summarize_conflicts
    from backend.aria_synthesis.evidence_bundle import EvidenceBundle
    from backend.aria_synthesis.long_context_rules import LongContextRules
    from backend.planning.plan_builder import PlanBuilder
    from backend.tools.tool_executor import ToolExecutor
    from backend.tools.tool_registry import STATUS_OK
    from backend.tools.tool_router import ToolRouter
    from backend.aria_synthesis.synthesis_prompt import (
        NOT_FOUND_PHRASE,
        build_synthesis_prompt,
    )
    from backend.aria_synthesis.template_classifier import (
        TemplateType,
        classify_template,
        template_for_goal,
    )
    from backend.aria_synthesis.template_renderers import render_template
except ImportError:  # running from inside the backend directory
    from aria_synthesis.bundle_builder import build_evidence_bundle
    from aria_synthesis.conflict_detection import detect_conflicts
    from aria_synthesis.conflict_summary import summarize_conflicts
    from aria_synthesis.evidence_bundle import EvidenceBundle
    from aria_synthesis.long_context_rules import LongContextRules
    from planning.plan_builder import PlanBuilder
    from tools.tool_executor import ToolExecutor
    from tools.tool_registry import STATUS_OK
    from tools.tool_router import ToolRouter
    from aria_synthesis.synthesis_prompt import NOT_FOUND_PHRASE, build_synthesis_prompt
    from aria_synthesis.template_classifier import (
        TemplateType,
        classify_template,
        template_for_goal,
    )
    from aria_synthesis.template_renderers import render_template

# Resolved from the repository root either way, as everywhere else in the
# backend -- there is a logger.py beside backend/, not inside it.
from logger import get_logger

logger = get_logger(__name__)

class GenerationNotConfigured(RuntimeError):
    """Synthesis was asked to generate with no generator supplied."""


__all__ = [
    "GenerationNotConfigured",
    "AnswerPrompt",
    "build_answer_prompt",
    "NO_EVIDENCE_ANSWER",
    "answer_with_evidence",
    "default_generate",
    "synthesize",
]

# Re-exported so a caller can inspect what synthesis saw without importing
# two more modules to do it.
__all__ += ["classify_template", "detect_conflicts", "render_template", "summarize_conflicts"]

# What ARIA says when retrieval came back empty. Uses the same phrase the
# prompt instructs the model to use, so "not found" means one thing whether
# the model produced it or this module did.
NO_EVIDENCE_ANSWER = (
    f"{NOT_FOUND_PHRASE} - nothing in your notes or files matches that query."
)

# Deterministic decoding. A synthesis that reworded itself between two
# identical runs would make the whole pure-prompt exercise pointless.
TEMPERATURE = 0.0
SEED = 0
MAX_TOKENS = 1024


def default_generate(prompt: str, model_id: str | None = None) -> str:
    """Refuses. A generator must be supplied by the caller.

    This used to build its own LocalInferenceEngine and call .infer(),
    which went around ProviderRouter entirely -- Cloud Mode ignored, the
    session's model discarded, absolute mode separation unenforced. Every
    answer it produced was routed wrongly, and nothing said so.

    It raises rather than being deleted because the failure it prevents is
    silent. A caller that forgets to pass `generate` would otherwise get a
    plausible answer from the wrong provider; now it gets a message naming
    the function to use. Tests that need a stub already pass their own
    callable, so nothing legitimate depends on this path.

    Build one with backend.core.generation.make_generator(model_id, mode),
    which resolves through ProviderRouter and can stream.
    """
    raise GenerationNotConfigured(
        "synthesis was asked to generate without a generator. Pass "
        "generate=make_generator(model_id, mode) from backend.core.generation; "
        "reaching a model any other way skips ProviderRouter and mode separation."
    )


def _evidence_listing(bundle: EvidenceBundle) -> str:
    """What to say when there is evidence but no model to synthesize it.

    Deliberately unpolished. This is the raw material of an answer rather
    than an answer, and it should read that way -- a user who sees it should
    understand that ARIA found things and could not think about them.
    """
    lines = [
        f"Found {bundle.item_count} relevant item(s) but could not synthesize an "
        "answer (no model available). The evidence:"
    ]
    for item in (*bundle.notes, *bundle.files):
        lines.append(f"- ({item.label}) {item.snippet}")
    return "\n".join(lines)


class _GoalView:
    """The two goal fields long-context rules read, off a bundle.

    A bundle records the goal as flat meta rather than holding a GoalState:
    meta is what gets logged and compared, and a dataclass in it would make
    that awkward. The rules want something with .goal on it, so this is the
    adapter -- three lines rather than teaching either side about the other.
    """

    __slots__ = ("goal", "topic")

    def __init__(self, goal: str, topic: str) -> None:
        self.goal = goal
        self.topic = topic


def _goal_view(bundle: EvidenceBundle):
    return _GoalView(bundle.current_goal, bundle.goal_topic) if bundle.current_goal else None


@dataclass(frozen=True)
class AnswerPrompt:
    """What Engine B produced for one turn.

    text is the synthesis prompt, or None when there was no evidence and
    no successful lookup -- the caller's signal to answer without a model.

    tool_runs names the tools that actually executed, in order. It exists
    because "did a lookup happen" was previously unanswerable from
    outside: a turn that planned a search, failed to run it, and answered
    from the model's weights looked identical to one that searched. That
    ambiguity is what let a fabricated stock price pass for an answer, so
    the fact is now reported rather than inferred.
    """

    text: str | None
    tool_runs: tuple[str, ...] = ()


def build_answer_prompt(query: str, bundle: EvidenceBundle) -> AnswerPrompt:
    """The full synthesis prompt for a bundle, plus what ran to build it.

    Split out of synthesize() so a caller that streams can take the prompt
    and generate it through its own provider path, rather than receiving a
    finished string it would have to emit in one piece. Engine B still owns
    every decision in here -- conflicts, template, long-context analysis,
    the plan, the tool results -- and still knows nothing about streaming.

    Returns an AnswerPrompt whose text is None for a turn with no evidence
    and no successful lookup, which is the caller's signal to answer "not
    found" without a model.
    """
    plan = PlanBuilder().build(query, bundle, bundle.current_topic, _goal_view(bundle))
    invocations = ToolRouter().route(plan, _goal_view(bundle))
    tool_results = ToolExecutor().execute(invocations)
    tool_runs = tuple(result.tool_name for result in tool_results)

    if bundle.is_empty and not any(result.status == STATUS_OK for result in tool_results):
        return AnswerPrompt(None, tool_runs)

    conflicts = detect_conflicts(bundle, bundle.goal_topic)
    template = classify_template(query, bundle)
    if template is TemplateType.EXPLANATION and bundle.current_goal:
        template = template_for_goal(bundle.current_goal) or classify_template(
            bundle.current_goal, bundle
        )

    long_context = LongContextRules().apply(
        bundle, bundle.turns, bundle.current_topic, _goal_view(bundle), conflicts,
    )

    return AnswerPrompt(
        build_synthesis_prompt(
            query,
            bundle,
            summarize_conflicts(conflicts),
            render_template(template, bundle, conflicts),
            long_context,
            plan,
            tool_results,
        ),
        tool_runs,
    )


def synthesize(query: str, bundle: EvidenceBundle, generate=None, model_id=None) -> str:
    """Answer one query from an already-built bundle.

    Split out from answer_with_evidence so a caller that has a bundle in
    hand -- having logged it, filtered it or built it from somewhere other
    than retrieval -- does not have to rebuild it to get an answer.

    Conflict detection and template classification both run here rather
    than in the builder. A bundle is a record of what was retrieved and
    should mean the same thing whoever reads it; which of its statements
    contradict each other, and what shape an answer to this query should
    take, are readings of that record, and readings belong to the step that
    acts on them. It also keeps the cost where the benefit is -- a caller
    that only wants the evidence pays for neither.
    """
    # Planning and the tool layer run before the empty-evidence check,
    # because a lookup is evidence. A weather question has no notes behind it
    # by its nature: checking the bundle first would mean the one kind of
    # question that most needs a tool could never reach one.
    #
    # The guarantee that check exists for is unchanged, and is now enforced
    # by outcome rather than by ordering: with nothing retrieved and nothing
    # returned, the answer is still "not found". Only a lookup that actually
    # came back lets an otherwise-empty query through.
    plan = PlanBuilder().build(query, bundle, bundle.current_topic, _goal_view(bundle))
    invocations = ToolRouter().route(plan, _goal_view(bundle))
    tool_results = ToolExecutor().execute(invocations)

    if bundle.is_empty and not any(result.status == STATUS_OK for result in tool_results):
        return NO_EVIDENCE_ANSWER

    conflicts = detect_conflicts(bundle, bundle.goal_topic)
    # The shape is chosen from the raw query, not the de-framed one: "how do
    # I" is framing by every other measure in this codebase, and here it is
    # the entire signal.
    template = classify_template(query, bundle)
    if template is TemplateType.EXPLANATION and bundle.current_goal:
        # The query asked for no particular shape -- which is what a bare
        # "continue" or "why is it still failing" always does. The goal is
        # the only thing left that says what kind of answer is wanted, and
        # "fix the shader error" asks for something quite different from
        # "compare addressables and assetbundles". Only consulted on the
        # default, so a query that did ask for a shape still gets it.
        template = template_for_goal(bundle.current_goal) or classify_template(
            bundle.current_goal, bundle
        )
    # The bundle already carries the turns selection kept and the topic and
    # goal they were selected against, so the analysis is assembled from
    # what is on it rather than from a second walk of the conversation --
    # two readings of one history that could disagree is exactly the bug
    # this layer exists to remove.
    long_context = LongContextRules().apply(
        bundle,
        bundle.turns,
        bundle.current_topic,
        _goal_view(bundle),
        conflicts,
    )
    # Built from the bundle and the goal for the same reason the analysis
    # is: everything planning needs was already decided upstream, and a
    # second reading of the conversation here could disagree with the first.
    prompt = build_synthesis_prompt(
        query,
        bundle,
        summarize_conflicts(conflicts),
        render_template(template, bundle, conflicts),
        long_context,
        plan,
        tool_results,
    )
    generate = generate or default_generate

    try:
        answer = generate(prompt)
    except Exception:
        logger.exception("Synthesis generation raised; falling back to the evidence listing.")
        answer = ""

    answer = str(answer or "").strip()
    return answer if answer else _evidence_listing(bundle)


def answer_with_evidence(
    query: str,
    retrieval_result,
    note_limit: int | None = None,
    file_limit: int | None = None,
    routing_intent: str | None = None,
    conversation=None,
    generate=None,
    model_id: str | None = None,
) -> str:
    """Turn retrieval output into a single synthesized answer.

    The whole of Phase 6 in one call: bundle the evidence, build the prompt,
    ask the model, return what it said. Retrieval is the caller's job and is
    not touched here -- pass in whatever search_ranked, search_files_ranked
    or search_hybrid returned, concatenated if more than one.

    conversation is the message history. It is used to record which topic
    the conversation is on and which goal the user is pursuing, so the
    prompt can tell evidence about the work in hand from evidence that
    merely scored well -- and, when the query itself asks for no particular
    shape of answer, so the goal can choose one. Pass the same history that
    was given to retrieval: the two making different decisions about what is
    being worked on is worse than neither of them knowing.
    """
    limits = {}
    if note_limit is not None:
        limits["note_limit"] = note_limit
    if file_limit is not None:
        limits["file_limit"] = file_limit

    bundle = build_evidence_bundle(
        query,
        retrieval_result,
        routing_intent=routing_intent,
        conversation=conversation,
        **limits,
    )
    return synthesize(query, bundle, generate=generate, model_id=model_id)
