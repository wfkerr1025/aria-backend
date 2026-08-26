"""ARIA Lite Phase 3 - list_self tool wrapper.

Adapts aria_memory_self.list_self() to the dispatcher's calling
convention, and reports the version in force for each key alongside its
value -- a self-description is more useful when you can see how many
times it has been revised.
"""

try:
    from backend.aria_memory_self import get_self_version, list_self
except ImportError:  # running from inside the backend directory
    from aria_memory_self import get_self_version, list_self


def tool_list_self() -> dict:
    knowledge = list_self()
    return {
        "self": knowledge,
        "versions": {key: get_self_version(key) for key in knowledge},
        "count": len(knowledge),
    }


TOOL_SPEC = {
    "name": "list_self",
    "description": "List every self-knowledge key ARIA Lite holds, with its version.",
    "args": {},
}
