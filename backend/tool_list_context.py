"""ARIA Lite Phase 3 - list_context tool wrapper.

Adapts aria_memory_context.list_context() to the dispatcher's calling
convention. Expired entries are already filtered out by the store, so
what this returns is what ARIA currently believes about the user.
"""

try:
    from backend.aria_memory_context import list_context
except ImportError:  # running from inside the backend directory
    from aria_memory_context import list_context


def tool_list_context() -> dict:
    context = list_context()
    return {
        "context": context,
        "count": len(context),
    }


TOOL_SPEC = {
    "name": "list_context",
    "description": "List every live personal context key ARIA Lite holds.",
    "args": {},
}
