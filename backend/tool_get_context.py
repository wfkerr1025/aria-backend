"""ARIA Lite Phase 3 - get_context tool wrapper.

Adapts aria_memory_context.get_context() to the dispatcher's calling
convention. A missing key yields value=None rather than an error.
"""

try:
    from backend.aria_memory_context import get_context
except ImportError:  # running from inside the backend directory
    from aria_memory_context import get_context


def tool_get_context(key: str) -> dict:
    value = get_context(key)
    return {
        "key": key,
        "value": value,
    }


TOOL_SPEC = {
    "name": "get_context",
    "description": "Retrieve a personal context value from ARIA Lite.",
    "args": {
        "key": {"type": "string", "required": True},
    },
}
