import os
import time
import string
from pathlib import Path

UNITY_SIGNATURE_DIRS = ["Assets", "ProjectSettings", "Packages"]
PROJECT_VERSION_FILE = "ProjectSettings/ProjectVersion.txt"


def list_drives():
    """Return available drive roots (Windows-style)."""
    return [
        f"{letter}:/"
        for letter in string.ascii_uppercase
        if os.path.exists(f"{letter}:/")
    ]


def is_unity_project(path: Path) -> bool:
    """Check if a directory looks like a Unity project."""
    return path.is_dir() and all((path / d).exists() for d in UNITY_SIGNATURE_DIRS)


def find_unity_projects(search_entire_pc=True, search_roots=None, progress_callback=None):
    """
    Find Unity projects.
    Returns: [{ "name": <project name>, "path": <absolute path> }, ...]
    Optional progress_callback(dirpath: str) for UI/CLI integration.
    """
    roots = list_drives() if search_entire_pc or not search_roots else search_roots
    projects = []

    for root in roots:
        root_path = Path(root)
        for dirpath, _, _ in os.walk(root_path, errors="ignore"):
            if progress_callback:
                progress_callback(dirpath)

            p = Path(dirpath)
            if is_unity_project(p):
                projects.append({"name": p.name, "path": str(p)})

    return projects


def read_unity_version(project_path: str):
    """
    Read Unity editor version from ProjectVersion.txt.
    Returns: {"raw": <full version>, "major_minor": <version before 'f'>} or None.
    """
    version_file = Path(project_path) / PROJECT_VERSION_FILE
    if not version_file.exists():
        return None

    text = version_file.read_text(encoding="utf-8", errors="ignore")
    for line in text.splitlines():
        if "m_EditorVersion" in line:
            version_str = line.split(":", 1)[1].strip()
            return {
                "raw": version_str,
                "major_minor": version_str.split("f")[0],
            }
    return None


def find_unity_scenes(project_path: str):
    """
    Find all Unity scenes under Assets/.
    Returns: [{ "name": <scene name>, "path": <absolute path> }, ...]
    """
    scenes = []
    assets_root = Path(project_path) / "Assets"
    if not assets_root.exists():
        return scenes

    for dirpath, _, filenames in os.walk(assets_root, errors="ignore"):
        for fname in filenames:
            if fname.lower().endswith(".unity"):
                full = Path(dirpath) / fname
                scenes.append({"name": full.stem, "path": str(full)})
    return scenes
