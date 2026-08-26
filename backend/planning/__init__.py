"""ARIA Lite Phase 8 - multi-document planning.

When an answer has to work through several documents, the order it works
through them in is worth deciding before it starts rather than leaving to
whichever chunk scored highest.

    plan          the shapes -- PlanStep, Plan (pure data)
    plan_builder  query + evidence -> Plan (deterministic, no model)

The plan is structure handed to the model, not a sequence of actions ARIA
carries out; see plan.py for why that distinction is worth keeping sharp.
"""

from __future__ import annotations

__all__ = ["Plan", "PlanBuilder", "PlanStep", "build_plan"]


def __getattr__(name: str):
    if name in ("Plan", "PlanStep"):
        from backend.planning import plan

        return getattr(plan, name)
    if name in ("PlanBuilder", "build_plan"):
        from backend.planning import plan_builder

        return getattr(plan_builder, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
