# backend/llm/provider_registry.py

PROVIDERS = {
    # ============================================================
    # AUTO (virtual provider)
    # ============================================================
    "auto": {
        "installed": True,
        "model": "default",
        "description": "Automatically selects the best available provider",
        "capabilities": {},  # auto is not a real provider
        "client": None,
    },

    # ============================================================
    # LOCAL LLM (default fallback provider)
    # ============================================================
    "local": {
        "installed": True,
        "model": "mistral-nemo-12b-instruct-2407",
        "description": "Local LM Studio inference",
        "capabilities": {
            "general": 1.0,
            "chat": 1.0,
            "deep_reasoning": 0.8,
            "code_generate": 0.7,
            "summarize": 0.7,
            "document": 0.6,
            "translate": 0.5,
        },
        "client": None,
    },

    # ============================================================
    # OPENAI (primary cloud provider)
    # ============================================================
    "openai": {
        "installed": True,  # you said you have the API key
        "model": "gpt-4o",
        "description": "Primary cloud provider for strong reasoning and code",
        "capabilities": {
            # 🔥 REQUIRED FOR BASIC CHAT
            "general": 1.0,
            "chat": 1.0,

            # 🔥 Your existing intents
            "code_generate": 0.95,
            "module_build": 0.9,
            "backend_subsystem": 0.95,
            "story_create": 0.85,
            "song_create": 0.8,
            "poem_create": 0.8,
            "script_create": 0.85,
            "image_prompt": 0.8,
            "deep_reasoning": 0.95,
            "planning": 0.9,
            "summarize": 0.9,
            "document": 0.9,
            "translate": 0.9,
        },
        "client": None,
    },

    # ============================================================
    # GROK (creative + reasoning)
    # ============================================================
    "grok": {
        "installed": False,  # until you wire it
        "model": "grok-latest",
        "description": "Creative + reasoning provider",
        "capabilities": {
            "general": 0.8,
            "chat": 0.9,
            "story_create": 0.95,
            "song_create": 0.9,
            "poem_create": 0.9,
            "script_create": 0.9,
            "deep_reasoning": 0.85,
            "planning": 0.75,
        },
        "client": None,
    },

    # ============================================================
    # AZURE (enterprise stability)
    # ============================================================
    "azure": {
        "installed": False,  # until you wire it
        "model": "azure-gpt",
        "description": "Enterprise-grade cloud provider",
        "capabilities": {
            "general": 0.9,
            "chat": 0.9,
            "code_generate": 0.9,
            "module_build": 0.9,
            "backend_subsystem": 0.9,
            "deep_reasoning": 0.85,
            "planning": 0.8,
        },
        "client": None,
    },

    # ============================================================
    # ATHERIAL (experimental)
    # ============================================================
    "atherial": {
        "installed": False,
        "model": "atherial-latest",
        "description": "Experimental provider",
        "capabilities": {
            "general": 0.2,
            "chat": 0.3,
        },
        "client": None,
    },
}
