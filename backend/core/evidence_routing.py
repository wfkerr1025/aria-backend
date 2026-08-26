"""ARIA Lite - matching the model to the turn when evidence is involved.

Reading three sources, noticing they disagree, and writing one answer that
says which is which is a different job from answering a question. A model
can be perfectly good at the second and not up to the first, and the
failure is quiet: it produces fluent text that ignores the evidence it was
handed.

Measured on this repo, same query, same stubbed lookup, price sitting in
the prompt:

    nemo-12b / mistral-7b / gpt-4   use the evidence
    phi-3-mini (3.8B)               "I'm unable to provide real-time stock
                                     prices, but I can perform a web search
                                     for you"
    qwen-0.5b (0.5B)                uses the evidence, then degenerates

Note what that rules out. A parameter floor was the obvious rule and it
does not work: phi-3-mini is 3.8B and clears any threshold that qwen-0.5b
fails, yet phi-3 is the one that ignores evidence and qwen is not. The
capability that matters here is not size, so this is an allowlist of models
observed to do the job rather than a number.

Three routes when the resolved model is not on the list, chosen by what is
actually available:

    cloud available      hand the turn to cloud routing
    local-only mode      keep the model, simplify what is asked of it
    neither              do not ask a model at all; show the evidence

The third is the important one. A small model given evidence it cannot use
will not say so -- it will answer anyway, from its weights, which is the
failure the whole evidence pipeline exists to prevent. Printing what the
lookup returned is a worse answer and a truthful one.
"""

from __future__ import annotations

__all__ = [
    "EVIDENCE_MODEL_ALLOWLIST",
    "EVIDENCE_TOOLS",
    "ROUTE_CLOUD",
    "ROUTE_NORMAL",
    "ROUTE_RAW_EVIDENCE",
    "ROUTE_SIMPLIFIED",
    "SYNTHESIS_FULL",
    "SYNTHESIS_RAW_EVIDENCE",
    "SYNTHESIS_SIMPLIFIED",
    "choose_route",
    "extract_tool_results",
    "format_raw_evidence",
    "is_evidence_bearing",
    "model_can_synthesize_evidence",
    "simplified_prompt",
]


# Tools whose output is evidence a model has to read and reconcile.
EVIDENCE_TOOLS = frozenset({
    "web_search",
    "file_lookup",
    "news_search",
    "product_search",
})

# Models observed to read evidence and answer from it rather than around
# it. Matched by prefix, because registry ids carry a quantisation suffix
# ("mistral-7b" is registered as "mistral-7b-q4km") and a re-quantised
# rebuild of the same model is the same model for this purpose.
EVIDENCE_MODEL_ALLOWLIST = frozenset({
    "nemo-12b",
    "mistral-7b",
})

# What the orchestrator decided to do about it.
ROUTE_NORMAL = "normal"
ROUTE_CLOUD = "cloud_fallback_for_evidence"
ROUTE_SIMPLIFIED = "simplified_local_synthesis"
ROUTE_RAW_EVIDENCE = "raw_evidence_fallback"

# What the turn ends up asking of the model, reported on the TurnResult.
SYNTHESIS_FULL = "full"
SYNTHESIS_SIMPLIFIED = "simplified_local"
SYNTHESIS_RAW_EVIDENCE = "raw_evidence"


def is_evidence_bearing(tool_runs) -> bool:
    """Whether this turn produced evidence a model has to read.

    Takes what actually ran, not what was expected to. Weather never
    reaches here (the fusion engine answers it without a model),
    self-knowledge answers from the registry, and trivial and model-switch
    intents run no tools at all -- so all four are False by construction
    rather than by a special case.
    """
    return any(name in EVIDENCE_TOOLS for name in (tool_runs or ()))


def model_can_synthesize_evidence(model_id: str | None) -> bool:
    """Whether this model is trusted to answer from supplied evidence.

    model_id is None for cloud and Automatic routing, where ProviderRouter
    picks a frontier model -- those are trusted.
    """
    if not model_id:
        return True
    lowered = model_id.lower()
    return any(lowered.startswith(allowed) for allowed in EVIDENCE_MODEL_ALLOWLIST)


def choose_route(
    *,
    expects_evidence: bool,
    model_id: str | None,
    mode: str,
    cloud_available: bool,
) -> str:
    """Which of the three fallbacks this turn needs, if any.

    Called at model-resolution time, before the tools have run, so
    `expects_evidence` is a prediction. It has to be: the model is resolved
    and safety-checked several steps before Engine B executes anything, and
    swapping the model afterwards would mean the resource projection was
    made against a model that is no longer the one being loaded.

    Local Mode never routes to cloud, whatever else is true. Absolute mode
    separation is not a preference to be traded against answer quality --
    a user in Local Mode has said their data does not leave the machine,
    and a better answer is not worth breaking that.
    """
    if not expects_evidence or model_can_synthesize_evidence(model_id):
        return ROUTE_NORMAL

    if mode == "local":
        return ROUTE_SIMPLIFIED
    if cloud_available:
        return ROUTE_CLOUD
    return ROUTE_RAW_EVIDENCE


# ======================================================
# Reading the evidence back out
# ======================================================
# Engine B renders tool output into the synthesis prompt under a fixed
# heading and returns the assembled string. Options B and C need those
# lines on their own, so they are read back out of the prompt rather than
# by running the tools a second time -- one execution, one set of results,
# and no duplicate of the plan/route/execute sequence that Engine B owns.
#
# The coupling to that heading is real and is pinned by a test, so a change
# to the section format fails loudly here instead of silently returning
# nothing.
_TOOL_SECTION_HEADING = "Tool Results:"


def extract_tool_results(prompt: str) -> list[str]:
    """The Tool Results lines from an assembled synthesis prompt."""
    if not prompt or _TOOL_SECTION_HEADING not in prompt:
        return []

    _, _, rest = prompt.partition(_TOOL_SECTION_HEADING)
    lines = []
    for line in rest.splitlines():
        stripped = line.strip()
        if not stripped:
            if lines:
                break          # blank line ends the section
            continue
        if not stripped.startswith("-"):
            if stripped.startswith("("):
                continue       # the "(N of these failed...)" note
            break              # the next section has started
        lines.append(stripped.lstrip("- ").strip())
    return lines


def format_raw_evidence(evidence: list[str], query: str = "") -> str:
    """The lookup's own words, with one line of framing and no model.

    Deliberately plain. This path exists because no model available to this
    turn can be trusted to summarise the evidence without talking around
    it, so nothing here paraphrases: the user reads what the lookup
    returned and draws their own conclusion.
    """
    if not evidence:
        return (
            "I looked this up but the search returned nothing usable, and the "
            "model available for this turn is not one I'd trust to answer "
            "without evidence."
        )

    lead = (
        "Here is what the search returned. I'm showing it as-is rather than "
        "summarising it, because the model available for this turn isn't one "
        "I'd trust to read evidence accurately."
    )
    return "\n".join([lead, ""] + [f"- {line}" for line in evidence])


def simplified_prompt(query: str, evidence: list[str]) -> str:
    """A small prompt for a small model: one question, the findings, answer.

    Everything the full synthesis prompt does to help a capable model --
    the evidence summary, the plan, conflict detection, the eight synthesis
    rules -- is removed, because on a small model that structure is the
    problem rather than the help. It is a document to be continued, and it
    gets continued: the labels come back in the answer, and the
    instructions get restated. What is left here is a question and some
    facts.
    """
    lines = [f"Question: {query}", "", "What the lookup returned:"]
    lines += [f"- {item}" for item in evidence]
    lines += [
        "",
        "Answer the question in one or two sentences, using only the lines "
        "above. Do not add anything you were not told. If they do not answer "
        "the question, say so.",
    ]
    return "\n".join(lines)
