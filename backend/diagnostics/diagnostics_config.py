import json
from pathlib import Path

CONFIG_PATH = Path("config/ARIAConfig.json")

DEFAULT = {
    "window_size": {"width": 1280, "height": 820},
    "window_position": {"x": 240, "y": 160},
    "default_tab": "health",
    "theme_mode": "match_aria",
}


def _merge_defaults(data, defaults):
    """
    Merge missing keys from DEFAULT into loaded config.
    Handles nested dictionaries safely.
    """
    merged = {}

    for key, default_value in defaults.items():
        if key not in data:
            merged[key] = default_value
            continue

        # Nested dict → merge recursively
        if isinstance(default_value, dict) and isinstance(data[key], dict):
            nested = default_value.copy()
            nested.update({k: v for k, v in data[key].items()})
            merged[key] = nested
        else:
            merged[key] = data[key]

    return merged


def load_config():
    """
    Load diagnostic window configuration safely.
    Ensures missing keys are filled with DEFAULT values.
    """
    if not CONFIG_PATH.exists():
        return DEFAULT.copy()

    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return DEFAULT.copy()

    return _merge_defaults(data, DEFAULT)


def save_config(diag_cfg):
    """
    Save diagnostic window configuration safely.
    Creates parent directory if needed.
    """
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(diag_cfg, indent=2), encoding="utf-8")
    except Exception:
        # Silent fail is acceptable for diagnostics
        pass
