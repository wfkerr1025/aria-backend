"""ARIA Lite Phase 3 - set_self tool wrapper.

Adapts aria_memory_self.set_self() to the dispatcher's calling
convention.
"""

try:
    from backend.aria_memory_self import set_self
except ImportError:  # running from inside the backend directory
    from aria_memory_self import set_self


def tool_set_self(key: str, value: str) -> dict:
    set_self(key, value)
    return {
        "key": key,
        "value": value,
        "status": "updated",
    }


TOOL_SPEC = {
    "name": "set_self",
    "description": "Set or update ARIA Lite's self-knowledge key.",
    "args": {
        "key": {"type": "string", "required": True},
        "value": {"type": "string", "required": True},
    },
}
