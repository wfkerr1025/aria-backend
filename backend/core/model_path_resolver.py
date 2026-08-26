import os
import json
from pathlib import Path
from .model_registry import get_model

from logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------
# Path to models.json
# ---------------------------------------------------------
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "config",
    "models.json"
)

# ---------------------------------------------------------
# Load config
# ---------------------------------------------------------
def _load_config():
    logger.debug(f"Loading config from {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
        logger.debug("Config loaded successfully")
        return cfg

def _save_config(cfg):
    logger.debug(f"Saving updated config to {CONFIG_PATH}")
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4)
    logger.debug("Config saved successfully")

# ---------------------------------------------------------
# Model Path Resolver
# ---------------------------------------------------------
class ModelPathResolver:
    def __init__(self):
        logger.debug("Initializing ModelPathResolver")
        self.config = _load_config()

    # -----------------------------------------------------
    # Detect default model directory based on OS
    # -----------------------------------------------------
    def get_default_model_dir(self) -> Path:
        home = Path.home()
        default_dir = home / ".aria-lite" / "models"
        logger.debug(f"Default model directory resolved → {default_dir}")
        return default_dir

    # -----------------------------------------------------
    # Check if a model file exists
    # -----------------------------------------------------
    def model_exists(self, path: str) -> bool:
        exists = Path(path).expanduser().resolve().exists()
        logger.debug(f"Checking model path '{path}' → exists={exists}")
        return exists

    # -----------------------------------------------------
    # Resolve model path (main entry point)
    # -----------------------------------------------------
    def resolve_model_path(self, model_id: str) -> str:
        logger.debug(f"resolve_model_path() called → model_id={model_id}")

        model_cfg = get_model(model_id)
        if model_cfg is None:
            logger.debug(f"ERROR: Unknown model ID '{model_id}'")
            raise ValueError(f"Unknown model ID: {model_id}")

        # 1. If path exists → return it
        existing_path = model_cfg.get("path")
        logger.debug(f"Existing path in config → {existing_path}")

        if existing_path and self.model_exists(existing_path):
            logger.debug(f"Using existing model path → {existing_path}")
            return existing_path

        # 2. Try default directory + default filename
        default_dir = self.get_default_model_dir()
        default_path = default_dir / model_cfg["defaultFilename"]
        logger.debug(f"Trying default path → {default_path}")

        if self.model_exists(default_path):
            logger.debug(f"Default path valid → updating config to {default_path}")
            model_cfg["path"] = str(default_path)
            self._update_model_path(model_id, str(default_path))
            return str(default_path)

        # 3. If missing → return None for now
        logger.debug("Model path not found → returning None")
        return None

    # -----------------------------------------------------
    # Update models.json with new path
    # -----------------------------------------------------
    def _update_model_path(self, model_id: str, new_path: str):
        logger.debug(f"Updating model path in config → {model_id} = {new_path}")
        cfg = self.config
        cfg["models"][model_id]["path"] = new_path
        _save_config(cfg)
        logger.debug("Model path updated successfully")
