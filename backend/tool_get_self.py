"""ARIA Lite Phase 3 - get_self tool wrapper.

Adapts aria_memory_self.get_self() to the dispatcher's calling
convention. A missing key yields value=None rather than an error.
"""

try:
    from backend.aria_memory_self import get_self
except ImportError:  # running from inside the backend directory
    from aria_memory_self import get_self


def tool_get_self(key: str) -> dict:
    value = get_self(key)
    return {
        "key": key,
        "value": value,
    }


TOOL_SPEC = {
    "name": "get_self",
    "description": "Retrieve a self-knowledge value from ARIA Lite.",
    "args": {
        "key": {"type": "string", "required": True},
    },
}
