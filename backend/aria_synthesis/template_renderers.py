"""ARIA Lite Phase 6.3 - turning a chosen template into an instruction.

One short block of text per template, dropped into the prompt so the model
knows what shape to write in.

Three things every instruction here avoids:

    Evidence. Not a snippet, not a provenance string, not a count of notes.
    The evidence section already carries all of that, and a structure
    instruction that quotes it invites the model to answer from the
    instruction instead of from the sources.

    Content. "List the three steps" would be telling the model what it
    found before it has read anything. These say how to arrange an answer,
    never what the answer is.

    Padding. Each is one or two sentences. A structure instruction competing
    with the synthesis rules for attention makes both weaker, and the model
    is going to spend its attention on whichever it read last.

EXPLANATION renders nothing at all. It is the default -- the shape an answer
takes when nobody asked for a shape -- and an instruction saying "write
prose" is a sentence that only ever displaces a more useful one. The prompt
omits the whole section when this returns empty.
"""

from __future__ import annotations

try:
    from backend.aria_synthesis.evidence_bundle import EvidenceBundle
    from backend.aria_synthesis.template_classifier import TemplateType
except ImportError:  # running from inside the backend directory
    from aria_synthesis.evidence_bundle import EvidenceBundle
    from aria_synthesis.template_classifier import TemplateType

__all__ = ["CONFLICT_PLACEMENT", "TEMPLATE_INSTRUCTIONS", "render_template"]

TEMPLATE_INSTRUCTIONS: dict[TemplateType, str] = {
    TemplateType.STEPS: (
        "Structure the answer as a numbered sequence of steps, in the order "
        "they must be carried out. Say so plainly if the evidence does not "
        "cover a step rather than filling the gap."
    ),
    TemplateType.DIAGNOSTIC: (
        "Structure the answer as a list of possible causes, each with the "
        "check that would confirm or rule it out. Order them by how well "
        "the evidence supports them."
    ),
    TemplateType.COMPARISON: (
        "Structure the answer as a comparison of the alternatives, covering "
        "the same points for each so they can be read side by side. Note "
        "where the evidence covers one alternative better than the other."
    ),
    TemplateType.CHECKLIST: (
        "Structure the answer as a checklist of required items, one per "
        "line. Mark anything the evidence describes as optional rather than "
        "required."
    ),
    TemplateType.DECISION_GUIDE: (
        "Structure the answer as a decision guide: the criteria that decide "
        "the choice, what the evidence says about each, and which option "
        "that favours. Recommend one only if the evidence supports it."
    ),
    TemplateType.SUMMARY: (
        "Structure the answer as a short overview: the main points first, "
        "grouped by topic, with detail only where the evidence is specific."
    ),
    # EXPLANATION is absent deliberately -- see the module docstring.
}

# Added when the evidence disagrees with itself. Where a disagreement goes
# depends on the shape of the answer: a disputed step belongs at that step,
# not in a footnote nobody reads after following the first four. Generic by
# necessity -- naming the conflict here would leak evidence into a block
# that must not carry any.
CONFLICT_PLACEMENT = (
    "Where sources disagree, note it at the point in the structure it "
    "affects rather than collecting the disagreements at the end."
)


def render_template(
    template: TemplateType,
    bundle: EvidenceBundle = None,
    conflicts=None,
) -> str:
    """The instruction block for one template, or "" for no instruction.

    bundle is accepted for symmetry with the rest of the pipeline and for
    renderers that may later want to vary on what was retrieved; nothing
    here reads it today, which is what keeps the output a pure function of
    the template and whether anything conflicts.

    conflicts adds a placement note rather than any of their content. The
    structure is what decides where a disagreement should appear, so the
    instruction that sets the structure is the right place to say it.
    """
    instruction = TEMPLATE_INSTRUCTIONS.get(template, "")
    if not instruction:
        return ""
    if conflicts:
        return f"{instruction} {CONFLICT_PLACEMENT}"
    return instruction
