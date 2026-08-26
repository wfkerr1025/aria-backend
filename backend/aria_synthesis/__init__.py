"""ARIA Lite Phase 6 - multi-source answer synthesis.

Retrieval decides what ARIA has read; synthesis decides what it says. The
two are kept in separate packages because they fail differently: retrieval
is wrong when it returns the wrong chunks, synthesis is wrong when it
misreads chunks that were right. Debugging either is much easier when the
seam between them is a single inspectable object.

    evidence_bundle    the seam -- what was retrieved, normalized
    bundle_builder     retrieval output -> bundle (pure, no model calls)
    synthesis_prompt   bundle -> prompt (pure, deterministic)
    synthesis_engine   prompt -> answer (the only part that calls a model)

Nothing here reaches back into retrieval. A bundle can be built from any
list of ranked items, printed, diffed and asserted on without a database or
an embedding backend, which is what makes the prompt testable at all.
"""

from __future__ import annotations

__all__ = [
    "EvidenceBundle",
    "EvidenceFile",
    "EvidenceNote",
    "answer_with_evidence",
    "build_evidence_bundle",
    "build_synthesis_prompt",
]


def __getattr__(name: str):
    """Re-export the public API without importing the world at package load.

    synthesis_engine pulls in the inference stack, which is expensive and
    unnecessary for a caller that only wants to build a bundle.
    """
    if name in ("EvidenceBundle", "EvidenceFile", "EvidenceNote"):
        from backend.aria_synthesis import evidence_bundle

        return getattr(evidence_bundle, name)
    if name == "build_evidence_bundle":
        from backend.aria_synthesis.bundle_builder import build_evidence_bundle

        return build_evidence_bundle
    if name == "build_synthesis_prompt":
        from backend.aria_synthesis.synthesis_prompt import build_synthesis_prompt

        return build_synthesis_prompt
    if name == "answer_with_evidence":
        from backend.aria_synthesis.synthesis_engine import answer_with_evidence

        return answer_with_evidence
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
