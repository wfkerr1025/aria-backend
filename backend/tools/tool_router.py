"""ARIA Lite Phase 9 - mapping plan steps onto the tools they would need.

NOT backend/core/tool_router.py, which routes live tool calls through
execute_tool_truthful and records what came back. This one turns a Plan into
a list of intentions and touches nothing.

The mapping is one line per kind and deliberately dull:

    read      -> read_file, when the step names a document
    edit      -> edit_file, when the step names a document
    test      -> run_tests
    search    -> web_search, when the step carries a query
    weather   -> weather, when the step carries a location
    analyze   -> nothing
    summarize -> nothing

The last two matter as much as the first three. Analysis and summary are the
model's work, and inventing a tool for them would mean routing a step to
something that cannot do it -- so a step with no tool is not a gap, it is the
normal case, and the model handles it as it always did.

"When the step names a document" is what keeps read and edit honest. A Phase
8 plan targets `conversation` for its analyze and terminal steps, because a
synthesis plan describes a change rather than making one; only a step
pointing at an actual document has anything for a file tool to open. In
practice that means today's plans route their read steps and their test step,
and their edit step stays with the model -- which is correct while nothing is
wired up to edit anything.

Deterministic: same plan and goal in, same invocations out, in plan order.
No model, no I/O.
"""

from __future__ import annotations

try:
    from backend.planning.plan import CONVERSATION_TARGET
    from backend.tools.tool_registry import ToolInvocation, tool_for_kind
except ImportError:  # running from inside the backend directory
    from planning.plan import CONVERSATION_TARGET
    from tools.tool_registry import ToolInvocation, tool_for_kind

__all__ = ["ToolRouter", "route_plan"]


def _is_document(target: str) -> bool:
    """Whether a step's target names something a file tool could open.

    Anything that is not the conversation itself. Deliberately not a path
    check: a plan carries the basename a citation uses ("build.md"), not an
    absolute path, and demanding a separator here would reject every target
    a real plan produces.
    """
    return bool(target) and target != CONVERSATION_TARGET


class ToolRouter:
    """Turns a plan into the tool calls it would need.

    Stateless, and returns intentions rather than results -- routing decides
    what would be called, executing decides what happens, and keeping them
    apart is what lets the first be exercised without the second existing.
    """

    def route(self, plan, current_goal=None) -> list[ToolInvocation]:
        """The tool invocations a plan implies, in plan order.

        current_goal refines the arguments rather than the routing. The goal
        says what the user is trying to do, which is the only thing here that
        knows what "the tests" or "the change" refers to -- so it flows into
        the args, while which tool gets called stays a function of the step
        alone. Routing that varied with the goal would make the same plan
        mean different things on different turns.

        A plan with nothing to run returns an empty list, which is the
        common case and not a failure.
        """
        invocations: list[ToolInvocation] = []
        goal_text = str(getattr(current_goal, "goal", None) or current_goal or "") or None

        for step in getattr(plan, "steps", None) or []:
            tool = tool_for_kind(step.kind)
            if tool is None:
                continue  # analyze, summarize, answer -- the model's work

            if tool.name in ("web_search", "weather"):
                # A lookup carries its own arguments: the builder extracted
                # the location or the search terms from the query, and the
                # router is not the place to parse a sentence a second time.
                # A step that arrived without them is dropped rather than
                # guessed at -- calling a network tool on an invented
                # argument is worse than not calling it.
                args = dict(step.args or {})
                required = "query" if tool.name == "web_search" else "location"
                if not args.get(required):
                    continue
            elif tool.name in ("read_file", "edit_file"):
                if not _is_document(step.target):
                    continue
                args = {"path": step.target}
                if tool.name == "edit_file" and goal_text:
                    args["change"] = goal_text
            else:
                # run_tests. The goal is what says which tests are meant;
                # without one the scope is the whole suite.
                args = {"scope": goal_text or "all"}

            invocations.append(
                ToolInvocation(tool_name=tool.name, args=args, step_id=step.id)
            )
        return invocations


def route_plan(plan, current_goal=None) -> list[ToolInvocation]:
    """Convenience wrapper around a default ToolRouter."""
    return ToolRouter().route(plan, current_goal)
