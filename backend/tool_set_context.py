"""ARIA Lite Phase 3 - set_context tool wrapper.

Adapts aria_memory_context.set_context() to the dispatcher's calling
convention.
"""

try:
    from backend.aria_memory_context import set_context
except ImportError:  # running from inside the backend directory
    from aria_memory_context import set_context


def tool_set_context(key: str, value: str) -> dict:
    set_context(key, value)
    return {
        "key": key,
        "value": value,
        "status": "updated",
    }


TOOL_SPEC = {
    "name": "set_context",
    "description": "Set or update a personal context key in ARIA Lite.",
    "args": {
        "key": {"type": "string", "required": True},
        "value": {"type": "string", "required": True},
    },
}
