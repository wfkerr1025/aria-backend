"""ARIA Lite Phase 6 - the synthesis prompt.

Turns an evidence bundle into the text a model is asked to answer from.

Pure string assembly, and deliberately so. The prompt is the contract
between retrieval and the answer, and it is the first thing anyone reads
when an answer comes out wrong -- so it has to be reproducible from the
bundle alone, with no clock, no configuration and no model in the path.
Two runs of the same query produce byte-identical prompts; if the answers
differ, the difference is the model's, and that is a much shorter
investigation.

The rules exist to close specific failure modes rather than to be generally
encouraging:

    "Use only the evidence"    the model has plenty to say about deployment
                              pipelines in general, none of it about this
                              user's
    "State disagreement"       two notes six months apart are the normal
                              case, not an error, and averaging them
                              produces a fact that is in neither
    "Say not found"            a confident answer from no evidence is the
                              single worst outcome available here
    "Do not repeat verbatim"   an answer that quotes 250 characters back
                              has not synthesized anything
    "Reconcile domains"        the same word means different things to
                              Unity and to the backend, and evidence from
                              both is a signal to distinguish them, not to
                              merge them

Scores are shown to the model rather than being used only to order the
list, because "prefer higher-score evidence" is unactionable if the model
cannot see which evidence that is.
"""

from __future__ import annotations

import re

try:
    from backend.tools.tool_registry import STATUS_OK
except ImportError:  # running from inside the backend directory
    from tools.tool_registry import STATUS_OK

try:
    from backend.aria_synthesis.evidence_bundle import EvidenceBundle
except ImportError:  # running from inside the backend directory
    from aria_synthesis.evidence_bundle import EvidenceBundle

__all__ = [
    "CONFLICT_INSTRUCTION",
    "NOT_FOUND_PHRASE",
    "SYNTHESIS_RULES",
    "build_synthesis_prompt",
]

# Closes the section listing detected disagreements. Kept beside the list
# rather than folded into the numbered rules below, because a rule that only
# applies sometimes reads as boilerplate wherever it sits -- next to the
# actual conflicts it reads as being about them.
CONFLICT_INSTRUCTION = (
    "When answering, acknowledge these disagreements explicitly. Do not "
    "resolve them by choosing a side unless the evidence says which is "
    "current."
)

# The exact words the model is told to use when the evidence does not answer
# the question. Fixed rather than paraphrased so a caller can detect it, and
# so the synthesis engine's own empty-bundle answer says the same thing the
# model would have said.
NOT_FOUND_PHRASE = "not found"

SYNTHESIS_RULES = (
    "Use ONLY the evidence above. Do not add facts from general knowledge, "
    "and do not infer beyond what the evidence states.",
    "Prefer higher-scoring evidence when two pieces are equally relevant. "
    "Scores are shown with each item.",
    "If sources disagree, say so explicitly. Name what each source claims "
    "and cite both. Do not average them or silently pick one.",
    f'If the evidence does not answer the question, say "{NOT_FOUND_PHRASE}" '
    "and stop. Never invent an answer.",
    "Merge overlapping evidence into one explanation rather than listing "
    "the same point once per source.",
    "Cite lightly and inline, in the form (note 12) or "
    "(file:build.md#pipeline). Do not add a bibliography.",
    "Summarize in your own words. Do not quote the evidence back verbatim.",
    "If the evidence spans more than one domain, reconcile them: say which "
    "part of the answer belongs to which, rather than blending them.",
)


def _successful_tool_count(tool_results) -> int:
    """How many tool results below are usable evidence.

    The same predicate _tool_result_lines prints on -- a result that never
    reached a tool is not evidence, and neither is one that ran and failed.
    Counting them here rather than trusting the caller keeps the summary
    and the section it summarises from ever disagreeing.
    """
    return sum(
        1 for result in (tool_results or [])
        if getattr(result, "ran", False) and getattr(result, "status", None) == "ok"
    )


# How much of a lookup's own words the summary may carry.
#
# Extractive, and deliberately so: this line sits directly above evidence
# the model is told to use and nothing else. A generated sentence here
# would be an unsourced claim in the one place the prompt insists every
# claim be sourced -- and a model reading its own summary as evidence is
# how a paraphrase becomes a fact.
SUMMARY_MAX_SENTENCES = 5
SUMMARY_MAX_CHARS = 600

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _structured_findings(tool_results) -> list:
    """Every structured finding the lookups produced, in order.

    Reads ToolResult.normalized, which the tool layer fills in. Falls back
    to nothing rather than to the rendered line: a summary assembled from
    a line that was itself assembled from these fields would be a copy of
    a copy, and would silently lose whichever fields the line dropped.
    """
    findings = []
    for result in tool_results or []:
        if getattr(result, "status", None) != STATUS_OK:
            continue
        findings.extend(getattr(result, "normalized", ()) or ())
    return findings


def _extractive_summary(findings) -> str:
    """The findings' own sentences, bounded, in the order they arrived.

    Ordering is the tool's (rank), not a re-ranking, so the same evidence
    always produces the same summary. Nothing is rewritten: sentences are
    taken whole, and the only edit is where the budget runs out.
    """
    sentences: list[str] = []
    seen: set[str] = set()

    for finding in findings:
        text = " ".join(str(getattr(finding, "snippet", "") or "").split())
        if not text:
            continue
        for sentence in _SENTENCE_SPLIT.split(text):
            sentence = sentence.strip()
            key = sentence.lower()
            if not sentence or key in seen:
                continue
            seen.add(key)
            sentences.append(sentence)
            if len(sentences) >= SUMMARY_MAX_SENTENCES:
                break
        if len(sentences) >= SUMMARY_MAX_SENTENCES:
            break

    if not sentences:
        return ""

    summary = " ".join(sentences)
    if len(summary) <= SUMMARY_MAX_CHARS:
        return summary

    cut = summary[:SUMMARY_MAX_CHARS]
    boundary = cut.rfind(" ")
    if boundary > SUMMARY_MAX_CHARS // 2:
        cut = cut[:boundary]
    return cut.rstrip(" ,;:") + "\u2026"


def _evidence_summary(bundle: EvidenceBundle, tool_results=None) -> str:
    """One line telling the model what it is about to read.

    Cheap, and it does real work: a model that knows it has one note behaves
    differently from one that thinks it has a corpus, and stating the domain
    up front is what makes the reconcile-domains rule actionable.

    tool_results are counted because a lookup is evidence. This line used to
    describe the bundle alone, which meant a turn that searched the web and
    got an answer still opened with "No evidence was retrieved for this
    query" -- directly above a Tool Results section containing the answer.
    Large models resolved the contradiction by believing the section; small
    ones obeyed the sentence, and rule 4 below tells a model with no
    evidence to say "not found" and stop. That is exactly what they did,
    over and over:

        qwen2.5-0.5b  ->  "not found\nnot found\nnot found..."

    A prompt must not tell a model it has nothing while handing it
    something.
    """
    lookups = _successful_tool_count(tool_results)

    if not bundle:
        if lookups:
            # No notes or files, but the lookup below is real evidence and
            # has to be named as such -- the model is about to be told to
            # use only the evidence it was given.
            summary = (
                f"{lookups} tool result{'s' if lookups != 1 else ''} below, "
                "listed under Tool Results. Nothing was retrieved from notes "
                "or files, so that lookup is the evidence for this query."
            )
            found = _extractive_summary(_structured_findings(tool_results))
            return f"{summary} They report: {found}" if found else summary
        return "No evidence was retrieved for this query."

    parts = []
    if bundle.notes:
        parts.append(f"{len(bundle.notes)} note{'s' if len(bundle.notes) != 1 else ''}")
    if bundle.files:
        parts.append(f"{len(bundle.files)} file excerpt{'s' if len(bundle.files) != 1 else ''}")

    domains = ", ".join(domain.value for domain in bundle.domains)
    summary = f"{' and '.join(parts)} retrieved, domain: {domains}."

    if lookups:
        summary += (
            f" {lookups} tool result{'s' if lookups != 1 else ''} below "
            "count as evidence too."
        )
        found = _extractive_summary(_structured_findings(tool_results))
        if found:
            summary += f" They report: {found}"

    dropped = bundle.meta.get("dropped") or {}
    held_back = int(dropped.get("notes") or 0) + int(dropped.get("files") or 0)
    if held_back:
        # Said out loud so the model does not present the top five as the
        # whole of what is known.
        summary += f" {held_back} lower-scoring item(s) were not included."
    return summary


def _conversation_lines(bundle: EvidenceBundle) -> list[str]:
    """The carried turns, or [] when there are none.

    This is where selection earns its keep. Choosing which past turns
    matter and then showing the model none of them would make the whole
    exercise decorative: the point of picking four turns out of fifty is
    that those four go in front of the model and the other forty-six do
    not.

    Turns come after the evidence rather than before it. They are context
    for reading the evidence -- what was already asked, what was already
    answered -- and a model that reads them first tends to answer the
    earlier question instead of the current one.

    A turn from outside the current topic or goal is hedged rather than
    dropped, because it earned its place on the score: it is usually the
    turn where the user said the thing that connects the two subjects.
    """
    if not bundle.turns:
        return []

    lines = [f"Conversation Context ({len(bundle.turns)} earlier turn(s), most relevant first):"]
    lines += [f"- {turn.label}" for turn in bundle.turns]

    hedges = []
    current_topic = bundle.current_topic
    if current_topic:
        strayed = [turn for turn in bundle.turns if turn.topic and turn.topic != current_topic]
        if strayed:
            verb = "comes" if len(strayed) == 1 else "come"
            hedges.append(f"{len(strayed)} {verb} from another topic")

    current_goal = bundle.current_goal
    if current_goal:
        other_goal = [turn for turn in bundle.turns if turn.goal and turn.goal != current_goal]
        if other_goal:
            hedges.append(f"{len(other_goal)} from another goal")

    if hedges:
        lines.append(
            f"(Of these, {' and '.join(hedges)} -- treat those as "
            "background, and answer the current question.)"
        )
    return lines


def _goal_focus(bundle: EvidenceBundle) -> str:
    """What to say about the work in hand, or "" to say nothing.

    Printed whenever a goal is known, unlike the topic line, which only
    appears when something is off-topic. The difference is that a goal is
    an instruction about what the answer is for -- a user working on "fix
    the shader error" wants the answer aimed at that, whether or not the
    evidence strayed -- while a topic is only useful as a warning.

    An inferred goal is labelled as inferred. The model should weigh "you
    said you wanted to fix the shader error" differently from "you seem to
    be talking about Unity", and hiding the difference would invite it to
    treat a guess as an instruction.
    """
    goal = bundle.current_goal
    if not goal:
        return ""

    stated = bundle.meta.get("goal_explicit")
    qualifier = "stated" if stated else "inferred"
    line = f"Current Goal ({qualifier}): {goal}."

    # How much evidence came from outside this goal is said once, in the
    # Long-Context Analysis section, rather than here as well.
    return line


def _tool_result_lines(tool_results) -> list[str]:
    """The Tool Results section, or [] when nothing actually ran.

    Only results that reached a tool are printed. A result that was skipped
    or malformed ran nothing, and listing it under a heading reading "Tool
    Results" would be a claim that something happened -- the failure this
    section has to be built against, because a model that believes it will
    tell the user the tests passed and the user has no way to check.

    Errors *are* printed, and deliberately: a tool that ran and was refused,
    or ran and failed, is a fact about the world that the answer should be
    able to account for. What is never printed is a line for something that
    was never attempted.

    A list, not prose. These are outputs to read, and a paragraph wrapped
    around them is a paragraph the model has to unpick before it can use one.
    """
    ran = [result for result in (tool_results or []) if getattr(result, "ran", False)]
    if not ran:
        return []

    lines = ["Tool Results:"]
    lines += [f"- {result.line}" for result in ran]

    failed = sum(1 for result in ran if result.status != "ok")
    if failed:
        lines.append(
            f"({failed} of these failed. Say so if it affects the answer, and do "
            "not describe a failed tool as having done its job.)"
        )
    return lines


def _plan_lines(plan) -> list[str]:
    """The Plan section, or [] when there is nothing worth planning.

    Rendered as a numbered list and nothing else. The steps are structure --
    the order the answer works through its documents -- and turning them
    into prose would produce a paragraph the model has to parse back into
    an order before it can follow one.

    A single-step plan prints nothing. "1. answer conversation: answer the
    question" is a heading with no information under it, and a section that
    appears on every simple query is a section a reader learns to skip on
    the queries where it matters.
    """
    if not plan or getattr(plan, "is_single_answer", False):
        return []

    steps = getattr(plan, "steps", None) or []
    if not steps:
        return []
    return ["Plan:", *[f"{number}. {step.line}" for number, step in enumerate(steps, start=1)]]


def _long_context_lines(analysis) -> list[str]:
    """The Long-Context Analysis section, or [] when there is nothing to say.

    This is where the scope caveats live, all of them, in one place. Before
    Phase 7.4 they were scattered: the topic line was computed next to the
    evidence summary, the goal caveat next to the goal, and the cross-
    boundary conflicts inside the conflict summaries. Each was correct and
    together they read as four unrelated asides.

    The two conflict lists are rendered as counts rather than as their full
    sentences. Every one of those conflicts is already printed above, in the
    Conflicts Detected section, worded to say which boundary it crosses --
    repeating them here would put the same sentence in front of the model
    twice and make the section look like new information. The analysis
    *carries* the full strings, for a caller that wants them; the prompt
    points at what is already there.
    """
    if not analysis:
        return []

    body: list[str] = []
    for key in ("topic_focus", "goal_focus"):
        value = analysis.get(key)
        if value:
            body.append(f"- {value}")

    for key, noun in (
        ("cross_topic_conflicts", "different topics"),
        ("cross_goal_conflicts", "outside the current goal"),
    ):
        found = analysis.get(key) or []
        if found:
            verb = "involves" if len(found) == 1 else "involve"
            body.append(
                f"- {len(found)} of the disagreements above {verb} sources "
                f"from {noun}; weigh those more cautiously."
            )

    body += [f"- {warning}" for warning in analysis.get("continuity_warnings") or []]

    if not body:
        return []
    return ["Long-Context Analysis:", *body]


def _note_lines(bundle: EvidenceBundle) -> list[str]:
    if not bundle.notes:
        return ["(none)"]
    return [
        f"- [{note.provenance}] (score {note.score:.2f}) {note.snippet}"
        for note in bundle.notes
    ]


def _file_lines(bundle: EvidenceBundle) -> list[str]:
    if not bundle.files:
        return ["(none)"]
    return [
        f"- [{item.provenance}] (score {item.score:.2f}) {item.snippet}"
        for item in bundle.files
    ]


def build_synthesis_prompt(
    query: str,
    bundle: EvidenceBundle,
    conflict_summaries=None,
    template_instruction: str | None = None,
    long_context=None,
    plan=None,
    tool_results=None,
) -> str:
    """Assemble the full synthesis prompt for one query and its evidence.

    query is passed separately from bundle.query so a caller can put the
    question in front of the model in a different form than the one
    retrieval searched with -- the raw question is what should be answered,
    even when a cleaned version is what was searched for. They are normally
    the same string.

    The de-framed query is included alongside it only when the two differ,
    where it tells the model what retrieval actually looked for, and is left
    out otherwise rather than printing the same sentence twice.

    conflict_summaries are the neutral one-liners from conflict_summary,
    and are optional: a caller that has not run detection gets exactly the
    prompt it got before conflicts existed. When there are none the whole
    section is omitted rather than printed empty -- an always-present
    "Conflicts Detected: (none)" trains a reader, and a model, to skip the
    heading on the occasions when it matters.

    The section sits between the evidence and the instructions on purpose.
    The model should know what is disputed while it is still reading, not
    after it has formed a view from the highest-scoring source.

    A Conversation Context section lists the past turns selection judged
    worth carrying, when there are any, and hedges the ones that came from
    another topic or another goal. It sits after the conflicts and before
    the answer structure: it is context for reading the evidence, not
    evidence itself, and a model that meets it first tends to answer the
    question it finds there rather than the one being asked.

    A goal line follows the evidence summary whenever the bundle knows what
    the user is working on, and says whether that goal was stated or
    inferred.

    plan is the multi-document plan, and prints as a numbered list of steps
    just before the answer structure -- the order to work in, then the shape
    to write in. It is omitted entirely for a single-step plan, which is
    what a question with one document or none produces.

    tool_results are the ToolResults the tool layer produced for that plan,
    listed after it. Only those that actually reached a tool are printed, so
    the section is absent both when no tool was called for and when every
    call was skipped -- a heading claiming results is never shown over
    things that did not run.

    long_context is the analysis from long_context_rules, and is optional
    like everything else added since Phase 6.1: without it the prompt is
    exactly what it was. It renders after the conflicts and the carried
    turns and before the answer structure, which is where a caveat about
    how far the evidence reaches belongs -- after everything it is a caveat
    about, before the instruction on what to do with it. Both halves are required: without a current topic there
    is nothing to compare against, and without off-topic evidence there is
    nothing to warn about.

    template_instruction says what shape the answer should take, and is
    optional in the same way and for the same reason. It follows the
    conflicts because where a disagreement belongs depends on the structure,
    and precedes the synthesis rules because those are about what may be
    said rather than how to arrange it. An empty instruction -- which is
    what the default explanation shape renders -- omits the section rather
    than printing a heading with nothing under it.
    """
    cleaned = bundle.cleaned_query
    lines = [
        f"User Query: {query}",
    ]
    if cleaned and cleaned != query:
        lines.append(f"Search Topic: {cleaned}")

    lines += ["", f"Evidence Summary: {_evidence_summary(bundle, tool_results)}"]

    goal_line = _goal_focus(bundle)
    if goal_line:
        lines.append(goal_line)

    lines += [
        "",
        "Notes:",
        *_note_lines(bundle),
        "",
        "Files:",
        *_file_lines(bundle),
    ]

    summaries = [str(summary) for summary in (conflict_summaries or []) if summary]
    if summaries:
        lines += [
            "",
            "Conflicts Detected:",
            *[f"- {summary}" for summary in summaries],
            CONFLICT_INSTRUCTION,
        ]

    conversation = _conversation_lines(bundle)
    if conversation:
        lines += ["", *conversation]

    analysis = _long_context_lines(long_context)
    if analysis:
        lines += ["", *analysis]

    steps = _plan_lines(plan)
    if steps:
        lines += ["", *steps]

    tools = _tool_result_lines(tool_results)
    if tools:
        lines += ["", *tools]

    instruction = " ".join(str(template_instruction or "").split())
    if instruction:
        lines += ["", "Answer Structure:", instruction]

    lines += [
        "",
        "Synthesis Instructions:",
        *[f"{number}. {rule}" for number, rule in enumerate(SYNTHESIS_RULES, start=1)],
        "",
        "Produce a single coherent answer.",
    ]
    return "\n".join(lines)
