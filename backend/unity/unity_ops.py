import os
import sys
import time
import string
from pathlib import Path

from utils.theme_tags import TAG_UNITY, TAG_SCAN, TAG_FOUND, TAG_ERROR

from logger import get_logger

logger = get_logger(__name__)

UNITY_SIGNATURE_DIRS = ["Assets", "ProjectSettings", "Packages"]
PROJECT_VERSION_FILE = "ProjectSettings/ProjectVersion.txt"
EDITOR_BUILD_SETTINGS_FILE = "ProjectSettings/EditorBuildSettings.asset"

ASSET_EXTENSIONS = {
    "prefab": ".prefab",
    "material": ".mat",
    "shader": ".shader",
    "script": ".cs",
    "texture": [".png", ".jpg", ".jpeg", ".tga", ".psd"],
    "audio": [".wav", ".mp3", ".ogg"],
    "animation": [".anim", ".controller"],
    "scriptable_object": ".asset",
}

UNITY_TEMPLATES = {
    "MonoBehaviour": """using UnityEngine;

public class {class_name} : MonoBehaviour
{{
    void Start() {{ }}
    void Update() {{ }}
}}
""",
    "ScriptableObject": """using UnityEngine;

[CreateAssetMenu(fileName = "{class_name}", menuName = "ScriptableObjects/{class_name}", order = 1)]
public class {class_name} : ScriptableObject
{{
}}
""",
    "Editor": """using UnityEditor;
using UnityEngine;

[CustomEditor(typeof({target_type}))]
public class {class_name} : Editor
{{
    public override void OnInspectorGUI()
    {{
        base.OnInspectorGUI();
    }}
}}
"""
}

UNITY_SCENE_TEMPLATE = """%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!29 &1
SceneSettings:
  m_ObjectHideFlags: 0
"""

UNITY_PREFAB_TEMPLATE = """%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!1 &1
GameObject:
  m_Name: {name}
  m_Component: []
  m_IsActive: 1
"""

UNITY_SO_TEMPLATE = """%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!114 &1
MonoBehaviour:
  m_Name: {name}
"""


# =========================================================
# DRIVE + PROJECT DETECTION
# =========================================================
def list_drives():
    drives = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:/"
        if os.path.exists(root):
            drives.append(root)
    return drives


def is_unity_project(path: Path) -> bool:
    if not path.is_dir():
        return False
    return all((path / d).exists() for d in UNITY_SIGNATURE_DIRS)


def find_unity_projects(search_entire_pc=True, search_roots=None, progress_callback=None):
    """
    Scan for Unity projects.
    Returns a list of { "name": <project name>, "path": <absolute path> }.
    Optional progress_callback(dirpath: str) for UI/CLI feedback.
    """

    projects = []
    roots = list_drives() if search_entire_pc or not search_roots else search_roots

    start_time = time.time()
    file_count = 0
    byte_count = 0
    max_depth = 0
    enable_scanner = False
    last_update = 0
    UPDATE_INTERVAL = 0.05

    skip_dirs = [
        "windows",
        "program files",
        "appdata",
        "$recycle.bin",
        "system volume information",
    ]

    for root in roots:
        root_path = Path(root)

        for dirpath, dirnames, filenames in os.walk(root_path, errors="ignore"):
            lower = dirpath.lower()
            if any(skip in lower for skip in skip_dirs):
                continue

            depth = lower.count(os.sep) - str(root_path).lower().count(os.sep)
            max_depth = max(max_depth, depth)

            file_count += len(filenames)
            for f in filenames:
                try:
                    byte_count += os.path.getsize(os.path.join(dirpath, f))
                except Exception:
                    pass

            if not enable_scanner:
                if (
                    time.time() - start_time > 0.5
                    or file_count > 200
                    or byte_count > 50 * 1024 * 1024
                    or max_depth > 5
                ):
                    enable_scanner = True

            if enable_scanner:
                now = time.time()
                if now - last_update > UPDATE_INTERVAL:
                    if progress_callback:
                        progress_callback(dirpath)
                    else:
                        sys.stdout.write(f"\r{TAG_SCAN} Scanning: {dirpath}      ")
                        sys.stdout.flush()
                    last_update = now

            p = Path(dirpath)
            if is_unity_project(p):
                if progress_callback is None:
                    logger.info(f"{TAG_FOUND} Unity project: {p.name} ({p})")
                projects.append({"name": p.name, "path": str(p)})

    if progress_callback is None:
        sys.stdout.write("\r" + " " * 140 + "\r")
        sys.stdout.flush()

    return projects


# =========================================================
# UNITY VERSION DETECTION
# =========================================================
def read_unity_version(project_path: str):
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


# =========================================================
# SCENE INDEXING
# =========================================================
def find_unity_scenes(project_path: str):
    scenes = []
    assets_root = Path(project_path) / "Assets"

    if not assets_root.exists():
        return scenes

    for dirpath, dirnames, filenames in os.walk(assets_root, errors="ignore"):
        for fname in filenames:
            if fname.lower().endswith(".unity"):
                full = Path(dirpath) / fname
                scenes.append({"name": full.stem, "path": str(full)})
    return scenes


# =========================================================
# ASSET INDEXING
# =========================================================
def match_ext(fname: str, exts):
    if isinstance(exts, str):
        return fname.lower().endswith(exts)
    return any(fname.lower().endswith(e) for e in exts)


def index_unity_assets(project_path: str):
    assets_root = Path(project_path) / "Assets"
    index = {k: [] for k in ASSET_EXTENSIONS.keys()}

    if not assets_root.exists():
        return index

    for dirpath, dirnames, filenames in os.walk(assets_root, errors="ignore"):
        for fname in filenames:
            full = Path(dirpath) / fname
            for kind, exts in ASSET_EXTENSIONS.items():
                if match_ext(fname, exts):
                    index[kind].append({"name": full.stem, "path": str(full)})
                    break
    return index


# =========================================================
# BUILD PIPELINE PARSING
# =========================================================
def read_build_settings(project_path: str):
    build_file = Path(project_path) / EDITOR_BUILD_SETTINGS_FILE
    if not build_file.exists():
        return []

    text = build_file.read_text(encoding="utf-8", errors="ignore")
    scenes = []
    current_path = None
    enabled = True

    for line in text.splitlines():
        line = line.strip()

        if line.startswith("path:"):
            current_path = line.split("path:", 1)[-1].strip()
        elif line.startswith("enabled:"):
            enabled = line.split("enabled:", 1)[-1].strip() in ["1", "true", "True"]

        if current_path and line == "":
            scenes.append({"path": current_path, "enabled": enabled})
            current_path = None
            enabled = True

    return scenes


# =========================================================
# GENERATORS (SCRIPT / SCENE / PREFAB / SCRIPTABLEOBJECT)
# =========================================================
def unity_create_script(project_path, class_name, kind="MonoBehaviour", target_type="GameObject"):
    class_name = class_name.strip()
    if not class_name:
        return f"{TAG_ERROR} Invalid class name."

    scripts_dir = Path(project_path) / "Assets" / "Scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    file_path = scripts_dir / f"{class_name}.cs"
    if file_path.exists():
        return f"{TAG_ERROR} Script already exists: {file_path}"

    template = UNITY_TEMPLATES.get(kind)
    if not template:
        return f"{TAG_ERROR} Unknown script type: {kind}"

    file_path.write_text(
        template.format(class_name=class_name, target_type=target_type),
        encoding="utf-8",
    )
    return f"{TAG_UNITY} Created script: {file_path}"


def unity_create_scene(project_path, scene_name):
    scene_name = scene_name.strip()
    if not scene_name:
        return f"{TAG_ERROR} Invalid scene name."

    scenes_dir = Path(project_path) / "Assets" / "Scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)

    file_path = scenes_dir / f"{scene_name}.unity"
    if file_path.exists():
        return f"{TAG_ERROR} Scene already exists: {file_path}"

    file_path.write_text(UNITY_SCENE_TEMPLATE, encoding="utf-8")
    return f"{TAG_UNITY} Created scene: {file_path}"


def unity_create_prefab(project_path, prefab_name):
    prefab_name = prefab_name.strip()
    if not prefab_name:
        return f"{TAG_ERROR} Invalid prefab name."

    prefabs_dir = Path(project_path) / "Assets" / "Prefabs"
    prefabs_dir.mkdir(parents=True, exist_ok=True)

    file_path = prefabs_dir / f"{prefab_name}.prefab"
    if file_path.exists():
        return f"{TAG_ERROR} Prefab already exists: {file_path}"

    file_path.write_text(UNITY_PREFAB_TEMPLATE.format(name=prefab_name), encoding="utf-8")
    return f"{TAG_UNITY} Created prefab: {file_path}"


def unity_create_scriptableobject(project_path, name):
    name = name.strip()
    if not name:
        return f"{TAG_ERROR} Invalid ScriptableObject name."

    so_dir = Path(project_path) / "Assets" / "ScriptableObjects"
    so_dir.mkdir(parents=True, exist_ok=True)

    file_path = so_dir / f"{name}.asset"
    if file_path.exists():
        return f"{TAG_ERROR} ScriptableObject already exists: {file_path}"

    file_path.write_text(UNITY_SO_TEMPLATE.format(name=name), encoding="utf-8")
    return f"{TAG_UNITY} Created ScriptableObject: {file_path}"


# =========================================================
# PROJECT SUMMARY
# =========================================================
def summarize_unity_project(project_path: str, progress_callback=None):
    """
    Summarize a Unity project:
      - scenes
      - assets (by extension)
      - build_scenes
    Returns a dict with keys: scenes, assets, build_scenes.
    """

    summary = {
        "scenes": [],
        "assets": {},
        "build_scenes": [],
    }

    assets_root = os.path.join(project_path, "Assets")
    last_update = 0
    UPDATE_INTERVAL = 0.05

    # Scenes
    for root, dirs, files in os.walk(assets_root, errors="ignore"):
        for f in files:
            full = os.path.join(root, f)
            now = time.time()
            if now - last_update > UPDATE_INTERVAL:
                if progress_callback:
                    progress_callback(full)
                else:
                    sys.stdout.write(f"\r{TAG_SCAN} Scanning: {full}      ")
                    sys.stdout.flush()
                last_update = now

            if f.lower().endswith(".unity"):
                rel = os.path.relpath(full, project_path).replace("\\", "/")
                summary["scenes"].append({
                    "name": f,
                    "path": rel,
                })

    # Assets
    for root, dirs, files in os.walk(assets_root, errors="ignore"):
        for f in files:
            full = os.path.join(root, f)
            now = time.time()
            if now - last_update > UPDATE_INTERVAL:
                if progress_callback:
                    progress_callback(full)
                else:
                    sys.stdout.write(f"\r{TAG_SCAN} Scanning: {full}      ")
                    sys.stdout.flush()
                last_update = now

            ext = os.path.splitext(f)[1].lower()
            rel = os.path.relpath(full, project_path).replace("\\", "/")
            summary["assets"].setdefault(ext, []).append({
                "name": f,
                "path": rel,
            })

    if progress_callback is None:
        sys.stdout.write("\r" + " " * 120 + "\r")
        sys.stdout.flush()

    # Build scenes
    build_file = Path(project_path) / EDITOR_BUILD_SETTINGS_FILE
    if build_file.exists():
        try:
            text = build_file.read_text(encoding="utf-8", errors="ignore")
            for line in text.splitlines():
                if "path:" in line:
                    path = line.split("path:", 1)[-1].strip()
                    summary["build_scenes"].append({
                        "path": path,
                        "enabled": True,
                    })
        except Exception:
            pass

    return summary


# =========================================================
# PLUGIN WRAPPER CLASS
# =========================================================
class UnityTools:
    name = "Unity"

    def __init__(self):
        self.project_path = None

    def open(self):
        """
        Called when user clicks Unity in the Plugins menu.
        Hook this into your UI or ChatManager as needed.
        """
        logger.info(f"{TAG_UNITY} Unity plugin activated.")
