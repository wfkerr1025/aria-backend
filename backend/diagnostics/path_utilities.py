# path_utilities.py
from pathlib import Path


def ensure_directory(path):
    """
    Ensure the directory for a given file path exists.
    If `path` is a file, its parent directory is created.
    If `path` is a directory, that directory is created.
    """
    p = Path(path)

    # If the path is a file, ensure its parent exists
    target = p if p.suffix == "" else p.parent

    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Silent fail is acceptable for diagnostics utilities
        pass


def validate_path(path):
    """
    Validate that the directory for a given path exists.
    Returns True if the directory exists, False otherwise.
    """
    p = Path(path)
    target = p if p.suffix == "" else p.parent
    return target.exists()
