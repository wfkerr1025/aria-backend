from pathlib import Path

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
"""
}


# =========================================================
# SCRIPT CREATION
# =========================================================
def create_script(project_path: str, class_name: str, kind: str = "MonoBehaviour"):
    """
    Create a Unity C# script inside Assets/Scripts.
    Returns a message describing the result.
    """

    # Normalize class name
    class_name = class_name.strip()
    if not class_name:
        return "Invalid class name."

    scripts_dir = Path(project_path) / "Assets" / "Scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    file_path = scripts_dir / f"{class_name}.cs"

    if file_path.exists():
        return f"Script already exists: {file_path}"

    template = UNITY_TEMPLATES.get(kind)
    if not template:
        return f"Unknown script type: {kind}"

    try:
        file_path.write_text(template.format(class_name=class_name), encoding="utf-8")
        return f"Created script: {file_path}"
    except Exception as e:
        return f"Failed to create script: {e}"


# =========================================================
# SCENE CREATION
# =========================================================
def create_scene(project_path: str, scene_name: str):
    """
    Create a Unity scene file inside Assets/Scenes.
    Returns a message describing the result.
    """

    scene_name = scene_name.strip()
    if not scene_name:
        return "Invalid scene name."

    scenes_dir = Path(project_path) / "Assets" / "Scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)

    file_path = scenes_dir / f"{scene_name}.unity"

    if file_path.exists():
        return f"Scene already exists: {file_path}"

    try:
        file_path.write_text("%YAML 1.1\nSceneSettings:\n", encoding="utf-8")
        return f"Created scene: {file_path}"
    except Exception as e:
        return f"Failed to create scene: {e}"
