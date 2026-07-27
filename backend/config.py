import json
import os
from pathlib import Path

CONFIG_PATH = Path("config/ARIAConfig.json")


def load_config():
    """
    Unified ARIA Lite configuration loader.
    - Loads ARIAConfig.json
    - Ensures dict-like behavior
    - Applies environment overrides (LM Studio, API keys)
    - Returns a predictable config dict
    """

    # ---------------------------------------------------------
    # BASE CONFIG
    # ---------------------------------------------------------
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            config = {}
    else:
        config = {}

    # Ensure dict-like behavior
    if not isinstance(config, dict):
        config = {}

    # ---------------------------------------------------------
    # LM STUDIO ENDPOINT OVERRIDE
    # ---------------------------------------------------------
    lmstudio_env = _get_lmstudio_env()
    if lmstudio_env:
        config["lmstudio.base_url"] = lmstudio_env

    # ---------------------------------------------------------
    # PROVIDER API KEYS (ENV OVERRIDES)
    # ---------------------------------------------------------
    config.setdefault("openai.api_key", os.getenv("OPENAI_API_KEY", ""))
    config.setdefault("groq.api_key", os.getenv("GROQ_API_KEY", ""))
    config.setdefault("anthropic.api_key", os.getenv("ANTHROPIC_API_KEY", ""))

    return config


# ============================================================
# LM STUDIO ENDPOINT HELPERS
# ============================================================
def _get_lmstudio_env():
    """
    Returns LM Studio endpoint from environment variables,
    falling back to None.
    """
    candidates = [
        "LMSTUDIO_ENDPOINT",
        "LM_STUDIO_URL",
        "LMSTUDIO_URL",
    ]

    for var in candidates:
        value = os.getenv(var)
        if value:
            return _normalize_url(value)

    return None


def _normalize_url(url: str) -> str:
    """
    Ensures the LM Studio URL is properly formatted.
    """
    url = url.strip()

    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url

    if url.endswith("/"):
        url = url[:-1]

    return url
