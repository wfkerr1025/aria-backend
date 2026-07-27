class IntentDetector:
    """
    Expanded intent detector for ARIA Lite.
    Supports engineering, reasoning, creative, and media tasks.
    Rule-based, lightweight, and easily extendable.
    """

    def __init__(self):
        self.intent_map = {
            # Engineering / Code
            "code_generate": [
                "code", "python", "script", "function", "class", "module",
                "backend", "refactor", "rewrite", "optimize", "bug", "fix",
                "hello world"
            ],
            "module_build": [
                "create module", "new module", "multi-file", "package",
                "subsystem", "engine", "diagnostics panel"
            ],
            "backend_subsystem": [
                "router", "engine", "metadata", "context", "workspace",
                "transaction", "fs", "security"
            ],

            # Reasoning
            "deep_reasoning": [
                "explain", "why", "how", "analyze", "debug", "trace",
                "investigate", "root cause"
            ],
            "planning": [
                "plan", "steps", "workflow", "roadmap", "design"
            ],

            # Creative Writing
            "story_create": [
                "story", "lore", "fantasy", "novel", "chapter",
                "worldbuild", "character", "plot"
            ],
            "song_create": [
                "song", "lyrics", "melody", "chorus", "verse"
            ],
            "poem_create": [
                "poem", "poetry", "haiku", "stanza"
            ],
            "script_create": [
                "script", "dialogue", "scene", "screenplay"
            ],

            # Media / Image
            "image_prompt": [
                "image", "picture", "visual", "concept art", "prompt"
            ],
            "image_edit": [
                "edit image", "modify image", "change image"
            ],

            # Utility
            "summarize": [
                "summarize", "tl;dr", "shorten"
            ],
            "document": [
                "documentation", "docstring", "comment", "describe"
            ],
            "translate": [
                "translate", "convert language"
            ],
        }

    def detect(self, prompt: str) -> str:
        if not prompt:
            return "general"

        p = prompt.lower()

        # Try keyword matching
        for intent, keywords in self.intent_map.items():
            if any(k in p for k in keywords):
                return intent

        # Fallback — ALWAYS return something valid
        return "general"
